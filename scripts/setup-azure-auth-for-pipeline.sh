#!/usr/bin/env bash
set -euo pipefail

repo="${1:-${GITHUB_REPOSITORY:-}}"
if [[ -z "$repo" || "$repo" != */* ]]; then
  echo "Usage: $0 <github-owner/repository>" >&2
  exit 2
fi

command -v gh >/dev/null || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
gh auth status --hostname github.com >/dev/null

subscription_id="${AZURE_SUBSCRIPTION_ID:-75f2a33a-540e-4d0f-bd91-5681b79baa70}"
location="${AZURE_WEB_LOCATION:-eastasia}"
environment_name="${GITHUB_ENVIRONMENT:-production}"
azd_environment="${AZD_ENV_NAME:-strong-loop}"
identity_group="${AZURE_CICD_RESOURCE_GROUP:-rg-strong-loop-cicd}"
identity_name="${AZURE_CICD_IDENTITY_NAME:-id-strong-loop-github}"
web_group="${AZURE_WEB_RESOURCE_GROUP:-rg-strong-loop-web-production}"

az account set --subscription "$subscription_id"
tenant_id="$(az account show --query tenantId -o tsv)"
gh api --method PUT "repos/$repo/environments/$environment_name" >/dev/null

az group create --name "$identity_group" --location "$location" --tags app=strong-loop purpose=cicd --output none
az identity create --name "$identity_name" --resource-group "$identity_group" --location "$location" --output none

client_id="$(az identity show --name "$identity_name" --resource-group "$identity_group" --query clientId -o tsv)"
principal_id="$(az identity show --name "$identity_name" --resource-group "$identity_group" --query principalId -o tsv)"
subject_prefix="$(gh api "repos/$repo/actions/oidc/customization/sub" --jq '.sub_claim_prefix // empty' 2>/dev/null || true)"
subject_prefix="${subject_prefix:-repo:${repo}}"
subject="${subject_prefix}:environment:${environment_name}"
credential_suffix="$(printf '%s' "$subject_prefix" | sha256sum | cut -c1-8)"
credential_name="github-${environment_name}-${credential_suffix}"

if ! az identity federated-credential show --name "$credential_name" --identity-name "$identity_name" --resource-group "$identity_group" >/dev/null 2>&1; then
  az identity federated-credential create \
    --name "$credential_name" \
    --identity-name "$identity_name" \
    --resource-group "$identity_group" \
    --issuer "https://token.actions.githubusercontent.com" \
    --subject "$subject" \
    --audiences "api://AzureADTokenExchange" \
    --output none
fi

foundry_project_id="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AZURE_AI_PROJECT_ID -e "$azd_environment")"
foundry_project_endpoint="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AZURE_AI_PROJECT_ENDPOINT -e "$azd_environment")"
foundry_resource_group="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AZURE_RESOURCE_GROUP -e "$azd_environment")"
foundry_account_name="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AZURE_AI_ACCOUNT_NAME -e "$azd_environment")"
foundry_project_name="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AZURE_AI_PROJECT_NAME -e "$azd_environment")"
api_name="$(az webapp list --resource-group "$web_group" --query "[?starts_with(name, 'app-strong-loop-')].name | [0]" -o tsv)"
web_name="$(az staticwebapp list --resource-group "$web_group" --query '[0].name' -o tsv)"
web_hostname="$(az staticwebapp show --name "$web_name" --resource-group "$web_group" --query defaultHostname -o tsv)"

az role assignment create \
  --assignee-object-id "$principal_id" \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope "/subscriptions/$subscription_id/resourceGroups/$foundry_resource_group" \
  --only-show-errors \
  --output none
az role assignment create \
  --assignee-object-id "$principal_id" \
  --assignee-principal-type ServicePrincipal \
  --role "Foundry User" \
  --scope "$foundry_project_id" \
  --only-show-errors \
  --output none
az role assignment create \
  --assignee-object-id "$principal_id" \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope "/subscriptions/$subscription_id/resourceGroups/$web_group" \
  --only-show-errors \
  --output none

for pair in \
  "AZURE_CLIENT_ID=$client_id" \
  "AZURE_TENANT_ID=$tenant_id" \
  "AZURE_SUBSCRIPTION_ID=$subscription_id" \
  "AZURE_LOCATION=southeastasia" \
  "AZURE_WEB_LOCATION=$location" \
  "AZURE_WEB_RESOURCE_GROUP=$web_group" \
  "AZURE_API_APP_NAME=$api_name" \
  "AZURE_STATIC_WEB_APP_NAME=$web_name" \
  "AZURE_STATIC_WEB_APP_URL=https://$web_hostname" \
  "AZURE_RESOURCE_GROUP=$foundry_resource_group" \
  "AZURE_FOUNDRY_RESOURCE_GROUP=$foundry_resource_group" \
  "AZURE_AI_ACCOUNT_NAME=$foundry_account_name" \
  "AZURE_AI_PROJECT_NAME=$foundry_project_name" \
  "AZURE_AI_PROJECT_ENDPOINT=$foundry_project_endpoint" \
  "FOUNDRY_PROJECT_ENDPOINT=$foundry_project_endpoint" \
  "AZURE_AI_PROJECT_ID=$foundry_project_id"; do
  gh variable set "${pair%%=*}" --body "${pair#*=}" --repo "$repo" --env "$environment_name"
done

for name in APIM_GATEWAY_URL LOOP_MODEL LOOP_CHARTER LOOP_DATA LOOP_MAX_ITERATIONS TOOLBOX_STRONG_LOOP_TOOLBOX_MCP_ENDPOINT; do
  value="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value "$name" -e "$azd_environment")"
  gh variable set "$name" --body "$value" --repo "$repo" --env "$environment_name"
done

apim_key="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value APIM_SUBSCRIPTION_KEY -e "$azd_environment")"
printf '%s' "$apim_key" | gh secret set APIM_SUBSCRIPTION_KEY --repo "$repo" --env "$environment_name"
unset apim_key

swa_token="$(az staticwebapp secrets list --name "$web_name" --resource-group "$web_group" --query properties.apiKey -o tsv)"
printf '%s' "$swa_token" | gh secret set AZURE_STATIC_WEB_APPS_API_TOKEN --repo "$repo" --env "$environment_name"
unset swa_token

printf 'Configured GitHub environment %s for %s with OIDC identity %s.\n' "$environment_name" "$repo" "$identity_name"
