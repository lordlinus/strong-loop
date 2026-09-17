#!/usr/bin/env bash
#
# deploy.sh — one command to hand the strong-loop agent over to someone else.
#
# It checks the machine before it touches Azure (tooling, sign-in, permissions, model
# gateway), then provisions the Foundry project, wires Application Insights so traces are
# visible, publishes the skills and toolbox, deploys the agent, and grants the agent's
# managed identity the one role it cannot work without.
#
# Every step is idempotent: re-running it on an already-deployed environment is a no-op
# plus a fresh agent version.
#
#   ./scripts/deploy.sh                 # full deployment into the `strong-loop` azd env
#   ./scripts/deploy.sh --smoke         # ...and invoke the deployed agent once at the end
#   ./scripts/deploy.sh --check-only    # preflight only; changes nothing
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# ── defaults, all overridable by flag or environment ──────────────────────────────────
azd_env_name="${AZD_ENV_NAME:-strong-loop}"
subscription_id="${AZURE_SUBSCRIPTION_ID:-}"
location="${AZURE_LOCATION:-southeastasia}"
apim_store="${APIM_STORE:-$HOME/.config/azure-apim/apim-ssattiraju-01.env}"
azd_env_file=".azure/$azd_env_name/.env"
toolbox_name="${TOOLBOX_NAME:-strong-loop-toolbox}"
agent_name="strong-loop"

loop_model="${LOOP_MODEL:-gpt-5.6-luna}"
loop_charter="${LOOP_CHARTER:-charters/claims_analyst.yaml}"
loop_data="${LOOP_DATA:-data/claims.csv}"
loop_max_iterations="${LOOP_MAX_ITERATIONS:-4}"

check_only=0
skip_provision=0
skip_toolbox=0
skip_appinsights=0
smoke=0
assume_yes=0

usage() {
  sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Flags
  -e, --env NAME            azd environment name            (default: strong-loop)
  -s, --subscription ID     Azure subscription id           (default: the az CLI default)
  -l, --location REGION     Azure region for new resources  (default: southeastasia)
      --check-only          Run the preflight checks and stop.
      --skip-provision      Skip `azd provision` (the project already exists).
      --skip-toolbox        Do not publish skills or the toolbox.
      --skip-app-insights   Do not create or attach Application Insights.
      --smoke               Invoke the deployed agent once when finished.
  -y, --yes                 Never prompt; fail instead of asking.
  -h, --help                Show this help.

Environment it reads
  APIM_GATEWAY_URL / APIM_SUBSCRIPTION_KEY   the model gateway (else read from the selected azd env, then APIM_STORE)
  APIM_STORE                                 default: ~/.config/azure-apim/apim-ssattiraju-01.env
  LOOP_MODEL / LOOP_CHARTER / LOOP_DATA / LOOP_MAX_ITERATIONS   the agent's run defaults
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--env)           azd_env_name="$2"; shift 2 ;;
    -s|--subscription)  subscription_id="$2"; shift 2 ;;
    -l|--location)      location="$2"; shift 2 ;;
    --check-only)       check_only=1; shift ;;
    --skip-provision)   skip_provision=1; shift ;;
    --skip-toolbox)     skip_toolbox=1; shift ;;
    --skip-app-insights) skip_appinsights=1; shift ;;
    --smoke)            smoke=1; shift ;;
    -y|--yes)           assume_yes=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

# ── output helpers ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then bold=$'\e[1m'; dim=$'\e[2m'; red=$'\e[31m'; green=$'\e[32m'; yellow=$'\e[33m'; reset=$'\e[0m'
else bold=; dim=; red=; green=; yellow=; reset=; fi

step()  { printf '\n%s▸ %s%s\n' "$bold" "$*" "$reset"; }
ok()    { printf '  %s✓%s %s\n' "$green" "$reset" "$*"; }
warn()  { printf '  %s!%s %s\n' "$yellow" "$reset" "$*"; }
info()  { printf '  %s%s%s\n' "$dim" "$*" "$reset"; }
die()   { printf '\n  %s✗ %s%s\n\n' "$red" "$*" "$reset" >&2; exit 1; }

warnings=0
soft_warn() { warnings=$((warnings + 1)); warn "$@"; }

version_at_least() {  # version_at_least HAVE WANT
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]
}

# The Foundry extensions print a resolution log line before their JSON; drop everything
# before the first brace so the rest can be parsed.
json_tail() { sed -n '/^[[{]/,$p'; }

# ══════════════════════════════════════════════════════════════════════════════════════
# 1. Tooling
# ══════════════════════════════════════════════════════════════════════════════════════
step "Checking tooling"

command -v az  >/dev/null 2>&1 || die "The Azure CLI is not installed. See https://aka.ms/azure-cli"
command -v azd >/dev/null 2>&1 || die "The Azure Developer CLI is not installed. See https://aka.ms/azd-install"
command -v python3 >/dev/null 2>&1 || die "python3 is required (this script uses it to read JSON)."

az_version="$(az version --query '"azure-cli"' -o tsv 2>/dev/null || echo 0)"
version_at_least "$az_version" "2.60.0" \
  || soft_warn "az $az_version is older than the 2.60.0 this was verified against; upgrade if a command fails."
ok "az $az_version"

# azure.yaml pins the minimum azd; read it rather than repeating the number here.
azd_required="$(python3 - <<'PY'
import re, pathlib
text = pathlib.Path("azure.yaml").read_text()
m = re.search(r"azd:\s*'?>=\s*([0-9][^'\s]*)'?", text)
print(m.group(1) if m else "1.27.1")
PY
)"
azd_version="$(azd version --output json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["azd"]["version"].split()[0])' 2>/dev/null || echo 0)"
version_at_least "$azd_version" "$azd_required" \
  || die "azd $azd_version is older than the $azd_required azure.yaml requires. Run: azd version upgrade"
ok "azd $azd_version (azure.yaml requires >= $azd_required)"

# The Foundry work is done by azd extensions; a missing one fails much later and obscurely.
for ext in azure.ai.agents azure.ai.skills azure.ai.toolboxes; do
  if azd extension list --output json 2>/dev/null \
      | python3 -c 'import json,sys; want=sys.argv[1]; print(any(e.get("id")==want and e.get("installedVersion") for e in json.load(sys.stdin)))' "$ext" \
      | grep -q True; then
    ok "azd extension $ext"
  elif [[ $check_only -eq 1 ]]; then
    soft_warn "azd extension $ext is not installed (the script would install it)."
  else
    info "installing azd extension $ext"
    azd extension install "$ext" >/dev/null || die "Could not install the azd extension $ext."
    ok "azd extension $ext (installed)"
  fi
done

# ══════════════════════════════════════════════════════════════════════════════════════
# 2. Sign-in
# ══════════════════════════════════════════════════════════════════════════════════════
step "Checking Azure sign-in"

if ! az account show >/dev/null 2>&1; then
  [[ $assume_yes -eq 1 || $check_only -eq 1 ]] && die "Not signed in to the Azure CLI. Run: az login"
  info "az login"
  az login --only-show-errors >/dev/null
fi

if [[ -n "$subscription_id" ]]; then
  az account set --subscription "$subscription_id" \
    || die "Cannot select subscription $subscription_id with the signed-in account."
fi
subscription_id="$(az account show --query id -o tsv)"
subscription_name="$(az account show --query name -o tsv)"
tenant_id="$(az account show --query tenantId -o tsv)"
signed_in_as="$(az account show --query 'user.name' -o tsv)"
ok "az: $signed_in_as → $subscription_name ($subscription_id)"

if ! azd auth login --check-status >/dev/null 2>&1; then
  [[ $assume_yes -eq 1 || $check_only -eq 1 ]] && die "Not signed in to azd. Run: azd auth login"
  info "azd auth login"
  azd auth login >/dev/null
fi
ok "azd: signed in"

# ══════════════════════════════════════════════════════════════════════════════════════
# 3. Permissions
# ══════════════════════════════════════════════════════════════════════════════════════
# Asked of Azure, not inferred from role names: a custom role can grant the same actions.
# Deployment actions are fatal; roleAssignments/write is a warning because a second person
# with Owner or User Access Administrator can make that one grant afterwards.
step "Checking permissions on the subscription"

permissions_file="$(mktemp)"
trap 'rm -f "$permissions_file"' EXIT
az rest --method get \
  --url "https://management.azure.com/subscriptions/$subscription_id/providers/Microsoft.Authorization/permissions?api-version=2022-04-01" \
  -o json >"$permissions_file" 2>/dev/null || printf '{"value":[]}' >"$permissions_file"

check_action() {  # check_action ACTION -> prints "allow" or "deny"
  python3 - "$permissions_file" "$1" <<'PYEOF'
import json, pathlib, re, sys
perms = json.loads(pathlib.Path(sys.argv[1]).read_text() or '{"value":[]}').get("value", [])
action = sys.argv[2].lower()
def matches(patterns):
    return any(re.fullmatch(re.escape(p.lower()).replace("\\*", ".*"), action) for p in patterns)
print("allow" if any(matches(p.get("actions") or []) and not matches(p.get("notActions") or [])
                     for p in perms) else "deny")
PYEOF
}

fatal_actions=(
  "Microsoft.Resources/deployments/write"
  "Microsoft.Resources/subscriptions/resourceGroups/write"
  "Microsoft.CognitiveServices/accounts/write"
  "Microsoft.ContainerRegistry/registries/write"
)
missing_fatal=()
for action in "${fatal_actions[@]}"; do
  if [[ "$(check_action "$action")" == "allow" ]]; then ok "$action"; else missing_fatal+=("$action"); warn "$action — missing"; fi
done

can_assign_roles=0
if [[ "$(check_action "Microsoft.Authorization/roleAssignments/write")" == "allow" ]]; then
  can_assign_roles=1
  ok "Microsoft.Authorization/roleAssignments/write"
else
  soft_warn "Microsoft.Authorization/roleAssignments/write — missing. The agent's managed identity
    cannot be granted Foundry User by this account, and without it the agent cannot read a
    skill body from the toolbox. Ask an Owner or User Access Administrator to run the
    command this script prints at the end."
fi

if [[ "$(check_action "Microsoft.Insights/components/write")" == "allow" ]]; then
  ok "Microsoft.Insights/components/write"
elif [[ $skip_appinsights -eq 0 ]]; then
  soft_warn "Microsoft.Insights/components/write — missing. Application Insights will be skipped."
  skip_appinsights=1
fi

if [[ ${#missing_fatal[@]} -gt 0 ]]; then
  die "$signed_in_as cannot deploy into this subscription: ${#missing_fatal[@]} required action(s) missing.
  Contributor on the subscription (or on an existing resource group, with --skip-provision)
  plus User Access Administrator is the smallest role pair that works."
fi

# ══════════════════════════════════════════════════════════════════════════════════════
# 4. Resource providers
# ══════════════════════════════════════════════════════════════════════════════════════
step "Checking resource providers"
for provider in Microsoft.CognitiveServices Microsoft.ContainerRegistry Microsoft.OperationalInsights Microsoft.Insights Microsoft.Storage; do
  state="$(az provider show --namespace "$provider" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
  if [[ "$state" == "Registered" ]]; then
    ok "$provider"
  elif [[ $check_only -eq 1 ]]; then
    soft_warn "$provider is $state (the script would register it)."
  else
    info "registering $provider"
    az provider register --namespace "$provider" --wait --only-show-errors >/dev/null 2>&1 \
      || soft_warn "Could not register $provider; deployment may fail."
    ok "$provider (registered)"
  fi
done

# ══════════════════════════════════════════════════════════════════════════════════════
# 5. The model gateway
# ══════════════════════════════════════════════════════════════════════════════════════
# The agent has no model deployment of its own: every token goes through APIM. Without
# these two values the container starts and fails on the first turn, so check now.
step "Checking the model gateway"

load_gateway_values() {  # load_gateway_values FILE
  local file="$1"
  local supplied_gateway_url="${APIM_GATEWAY_URL:-}"
  local supplied_subscription_key="${APIM_SUBSCRIPTION_KEY:-}"
  [[ -f "$file" ]] || return 0

  info "reading $file"
  set -a
  # shellcheck disable=SC1090
  source "$file"
  set +a
  APIM_GATEWAY_URL="${supplied_gateway_url:-${APIM_GATEWAY_URL:-}}"
  APIM_SUBSCRIPTION_KEY="${supplied_subscription_key:-${APIM_SUBSCRIPTION_KEY:-}}"
}

load_gateway_values "$azd_env_file"
load_gateway_values "$apim_store"

if [[ -z "${APIM_GATEWAY_URL:-}" || -z "${APIM_SUBSCRIPTION_KEY:-}" ]]; then
  if [[ $assume_yes -eq 1 || $check_only -eq 1 ]]; then
    die "APIM_GATEWAY_URL and APIM_SUBSCRIPTION_KEY are not set in the environment, $azd_env_file, or $apim_store.
  Export both, or point APIM_STORE at the env file that does."
  fi
  read -r -p "  APIM gateway URL: " APIM_GATEWAY_URL
  read -r -s -p "  APIM subscription key: " APIM_SUBSCRIPTION_KEY; echo
  [[ -n "$APIM_GATEWAY_URL" && -n "$APIM_SUBSCRIPTION_KEY" ]] || die "Both values are required."
fi
ok "gateway ${APIM_GATEWAY_URL%/} (key ${#APIM_SUBSCRIPTION_KEY} chars)"

# A real POST is the only proof the gateway serves this model; /models lists the catalogue.
probe_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 60 \
  -X POST "${APIM_GATEWAY_URL%/}/openai/v1/responses" \
  -H "Ocp-Apim-Subscription-Key: $APIM_SUBSCRIPTION_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$loop_model\",\"input\":\"ping\",\"max_output_tokens\":16}" 2>/dev/null || echo 000)"
if [[ "$probe_status" == "200" ]]; then
  ok "$loop_model answered through the gateway"
elif [[ "$loop_model" == claude-* ]]; then
  info "$loop_model routes to /anthropic; skipping the OpenAI probe"
else
  soft_warn "the gateway returned HTTP $probe_status for $loop_model — the agent will fail on its first turn."
fi

if [[ $check_only -eq 1 ]]; then
  step "Preflight complete"
  [[ $warnings -eq 0 ]] && ok "no warnings — this machine can deploy strong-loop." \
                        || warn "$warnings warning(s) above."
  exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════════════
# 6. The azd environment
# ══════════════════════════════════════════════════════════════════════════════════════
step "Preparing the azd environment '$azd_env_name'"

if azd env list --output json | python3 -c 'import json,sys; print(any(e["Name"]==sys.argv[1] for e in json.load(sys.stdin)))' "$azd_env_name" | grep -q True; then
  azd env select "$azd_env_name"
  ok "reusing $azd_env_name"
else
  azd env new "$azd_env_name" --subscription "$subscription_id" --location "$location" --no-prompt
  ok "created $azd_env_name"
fi

env_set() { azd env set "$1" "$2" -e "$azd_env_name" >/dev/null; }
env_get() { azd env get-value "$1" -e "$azd_env_name" 2>/dev/null || true; }

env_set AZURE_SUBSCRIPTION_ID "$subscription_id"
env_set AZURE_LOCATION        "$location"
env_set ENABLE_HOSTED_AGENTS  "true"
env_set APIM_GATEWAY_URL      "$APIM_GATEWAY_URL"
env_set APIM_SUBSCRIPTION_KEY "$APIM_SUBSCRIPTION_KEY"
env_set LOOP_MODEL            "$loop_model"
env_set LOOP_CHARTER          "$loop_charter"
env_set LOOP_DATA             "$loop_data"
env_set LOOP_MAX_ITERATIONS   "$loop_max_iterations"
ok "charter $loop_charter · data $loop_data · model $loop_model · $loop_max_iterations iterations"

# ══════════════════════════════════════════════════════════════════════════════════════
# 7. Provision the Foundry project
# ══════════════════════════════════════════════════════════════════════════════════════
# Provision and deploy are separate on purpose: Application Insights and the toolbox have
# to exist before the agent container is built, because the agent reads their values from
# the azd environment at deploy time.
if [[ $skip_provision -eq 1 ]]; then
  step "Skipping provision (--skip-provision)"
else
  step "Provisioning the Foundry project (this takes a few minutes)"
  azd provision --no-prompt -e "$azd_env_name"
  ok "provisioned"
fi

project_id="$(env_get AZURE_AI_PROJECT_ID)"
project_endpoint="$(env_get AZURE_AI_PROJECT_ENDPOINT)"
resource_group="$(env_get AZURE_RESOURCE_GROUP)"
account_name="$(env_get AZURE_AI_ACCOUNT_NAME)"
[[ -n "$project_id" && -n "$project_endpoint" ]] \
  || die "The azd environment has no Foundry project. Run without --skip-provision."
account_id="${project_id%%/projects/*}"
ok "project $project_endpoint"

# The skill and toolbox extensions read *different* project-endpoint variables, so both
# are set and the endpoint is passed explicitly on every call.
env_set FOUNDRY_PROJECT_ENDPOINT "$project_endpoint"

# ══════════════════════════════════════════════════════════════════════════════════════
# 8. Application Insights
# ══════════════════════════════════════════════════════════════════════════════════════
# Traces are a property of the *project*, not of the agent: once an AppInsights connection
# exists, Foundry injects the connection string into the hosted container and main.py's
# OTel setup (enabled whenever FOUNDRY_HOSTING_ENVIRONMENT is set) exports to it.
if [[ $skip_appinsights -eq 1 ]]; then
  step "Skipping Application Insights"
else
  step "Wiring Application Insights"

  existing="$(az rest --method get \
    --url "https://management.azure.com$account_id/connections?api-version=2025-06-01" \
    --query "value[?properties.category=='AppInsights'] | [0].properties.metadata.ResourceId" -o tsv 2>/dev/null || true)"

  if [[ -n "$existing" && "$existing" != "None" ]]; then
    appi_id="$existing"
    ok "already connected to ${appi_id##*/}"
  else
    workspace_name="log-${account_name}"
    appi_name="appi-${account_name}"

    workspace_id="$(az resource show -g "$resource_group" -n "$workspace_name" \
      --resource-type Microsoft.OperationalInsights/workspaces --query id -o tsv 2>/dev/null || true)"
    if [[ -z "$workspace_id" ]]; then
      info "creating Log Analytics workspace $workspace_name"
      workspace_id="$(az resource create -g "$resource_group" -n "$workspace_name" \
        --resource-type Microsoft.OperationalInsights/workspaces --location "$location" \
        --properties '{"sku":{"name":"PerGB2018"},"retentionInDays":30}' \
        --query id -o tsv --only-show-errors)"
    fi
    ok "workspace $workspace_name"

    appi_id="$(az resource show -g "$resource_group" -n "$appi_name" \
      --resource-type Microsoft.Insights/components --query id -o tsv 2>/dev/null || true)"
    if [[ -z "$appi_id" ]]; then
      info "creating Application Insights $appi_name"
      appi_id="$(az resource create -g "$resource_group" -n "$appi_name" \
        --resource-type Microsoft.Insights/components --location "$location" \
        --properties "{\"Application_Type\":\"web\",\"Flow_Type\":\"Bluefield\",\"Request_Source\":\"rest\",\"WorkspaceResourceId\":\"$workspace_id\"}" \
        --query id -o tsv --only-show-errors)"
    fi
    ok "component $appi_name"

    connection_string="$(az resource show --ids "$appi_id" --query properties.ConnectionString -o tsv)"
    az rest --method put \
      --url "https://management.azure.com$account_id/connections/${appi_name}-traces?api-version=2025-06-01" \
      --headers 'Content-Type=application/json' \
      --body "$(python3 - "$appi_id" "$connection_string" <<'PY'
import json, sys
resource_id, connection_string = sys.argv[1], sys.argv[2]
print(json.dumps({"properties": {
    "category": "AppInsights",
    "target": resource_id,
    "authType": "ApiKey",
    "credentials": {"key": connection_string},
    "isSharedToAll": True,
    "metadata": {"ApiType": "Azure", "ResourceId": resource_id},
}}))
PY
)" -o none
    ok "connected $appi_name to the Foundry project"
  fi

  traces_url="https://portal.azure.com/#@$tenant_id/resource${appi_id}/overview"
fi

# ══════════════════════════════════════════════════════════════════════════════════════
# 9. Skills and toolbox
# ══════════════════════════════════════════════════════════════════════════════════════
# Nothing here can mint evidence — see toolbox.yaml. The three certifying tools stay bound
# per run in loop/tools.py and are deliberately not in the toolbox.
if [[ $skip_toolbox -eq 1 ]]; then
  step "Skipping skills and toolbox"
else
  step "Publishing skills and the toolbox"
  for skill_dir in src/strong-loop/skills/*/; do
    skill="$(basename "$skill_dir")"
    azd ai skill create "$skill" --file "$skill_dir/SKILL.md" --force \
      -p "$project_endpoint" -e "$azd_env_name" >/dev/null
    ok "skill $skill"
  done

  # The extension writes the endpoint to TOOLBOX_<NAME>_MCP_ENDPOINT, which azure.yaml
  # hands to the container. `create` has no --force, so an existing toolbox is read rather
  # than recreated.
  toolbox_var="TOOLBOX_$(printf '%s' "$toolbox_name" | tr 'a-z-' 'A-Z_')_MCP_ENDPOINT"
  if azd ai toolbox list -o json --project-endpoint "$project_endpoint" -e "$azd_env_name" 2>/dev/null \
      | json_tail \
      | python3 -c 'import json,sys; print(any(t["name"]==sys.argv[1] for t in json.load(sys.stdin).get("toolboxes",[])))' "$toolbox_name" \
      | grep -q True; then
    toolbox_endpoint="$(azd ai toolbox show "$toolbox_name" -o json \
      --project-endpoint "$project_endpoint" -e "$azd_env_name" 2>/dev/null \
      | json_tail | python3 -c 'import json,sys; print(json.load(sys.stdin).get("endpoint",""))')"
    env_set "$toolbox_var" "$toolbox_endpoint"
    ok "toolbox $toolbox_name (existing)"
  else
    azd ai toolbox create "$toolbox_name" --from-file toolbox.yaml \
      --project-endpoint "$project_endpoint" -e "$azd_env_name" >/dev/null
    toolbox_endpoint="$(env_get "$toolbox_var")"
    ok "toolbox $toolbox_name (created)"
  fi
  [[ -n "$toolbox_endpoint" ]] \
    || soft_warn "the toolbox endpoint is not in the azd environment; the agent will run on its bound tools alone."
fi

# ══════════════════════════════════════════════════════════════════════════════════════
# 10. Deploy the agent
# ══════════════════════════════════════════════════════════════════════════════════════
step "Deploying the agent (remote build; this takes a few minutes)"
azd deploy "$agent_name" --no-prompt -e "$azd_env_name"
agent_endpoint="$(env_get AGENT_STRONG_LOOP_RESPONSES_ENDPOINT)"
agent_version="$(env_get AGENT_STRONG_LOOP_VERSION)"
ok "agent $agent_name version ${agent_version:-?}"

# ══════════════════════════════════════════════════════════════════════════════════════
# 11. The agent's own identity
# ══════════════════════════════════════════════════════════════════════════════════════
# A hosted agent's managed identity is created with no Azure role at all. Model inference
# and session storage work anyway, but reading a skill body from the toolbox fails with
# McpError('Failed to read resource.') until it holds Foundry User on the account.
step "Granting the agent's managed identity its role"
agent_principal_id="$(env_get AGENT_STRONG_LOOP_INSTANCE_IDENTITY_PRINCIPAL_ID)"
grant_command="az role assignment create --assignee-object-id $agent_principal_id \\
    --assignee-principal-type ServicePrincipal --role 'Foundry User' --scope $account_id"

if [[ -z "$agent_principal_id" ]]; then
  soft_warn "the agent has no managed identity in the azd environment; skipping."
elif az role assignment list --assignee "$agent_principal_id" --scope "$account_id" \
      --query "[?roleDefinitionName=='Foundry User'] | length(@)" -o tsv 2>/dev/null | grep -qv '^0$'; then
  ok "Foundry User already held on ${account_id##*/}"
elif [[ $can_assign_roles -eq 0 ]]; then
  soft_warn "cannot assign roles with this account. Ask an Owner to run:
    $grant_command"
else
  az role assignment create \
    --assignee-object-id "$agent_principal_id" \
    --assignee-principal-type ServicePrincipal \
    --role "Foundry User" --scope "$account_id" \
    --only-show-errors -o none
  ok "Foundry User granted on ${account_id##*/} (allow a minute to propagate)"
fi

# ══════════════════════════════════════════════════════════════════════════════════════
# 12. Smoke test
# ══════════════════════════════════════════════════════════════════════════════════════
if [[ $smoke -eq 1 ]]; then
  step "Invoking the deployed agent (a full run takes a couple of minutes)"
  azd ai agent invoke --new-session "go" -e "$azd_env_name" || soft_warn "the smoke invocation failed."
fi

# ══════════════════════════════════════════════════════════════════════════════════════
step "Done"
cat <<EOF
  Subscription   $subscription_name ($subscription_id)
  Tenant         $tenant_id
  Resource group $resource_group
  Project        $project_endpoint
  Agent          $agent_name v${agent_version:-?}
  Endpoint       ${agent_endpoint:-unknown}
  Traces         ${traces_url:-Application Insights was skipped}

  Talk to it     azd ai agent invoke --new-session "go" -e $azd_env_name
  Watch it       azd ai agent monitor -e $azd_env_name
  Change a role  azd env set LOOP_CHARTER charters/portfolio_analyst.yaml -e $azd_env_name \\
                 && azd env set LOOP_DATA data/customers.csv -e $azd_env_name \\
                 && azd deploy $agent_name -e $azd_env_name
EOF
[[ $warnings -gt 0 ]] && warn "$warnings warning(s) above — read them before handing this on."
exit 0
