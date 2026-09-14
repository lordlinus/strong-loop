#!/usr/bin/env bash
set -euo pipefail

subscription_id="${AZURE_SUBSCRIPTION_ID:-75f2a33a-540e-4d0f-bd91-5681b79baa70}"
location="${AZURE_WEB_LOCATION:-eastasia}"
environment_name="${AZURE_WEB_ENVIRONMENT:-production}"
resource_group="${AZURE_WEB_RESOURCE_GROUP:-rg-strong-loop-web-production}"
azd_environment="${AZD_ENV_NAME:-strong-loop}"

az account set --subscription "$subscription_id"

foundry_endpoint="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AGENT_STRONG_LOOP_RESPONSES_ENDPOINT -e "$azd_environment")"
foundry_project_id="$(AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env get-value AZURE_AI_PROJECT_ID -e "$azd_environment")"

if [[ -z "$foundry_endpoint" || -z "$foundry_project_id" ]]; then
  echo "The $azd_environment azd environment is missing the Foundry agent endpoint or project ID." >&2
  exit 1
fi

deployment_name="strong-loop-web-$(date -u +%Y%m%d%H%M%S)"
az deployment sub create \
  --name "$deployment_name" \
  --location "$location" \
  --template-file infra/web/main.bicep \
  --parameters \
    environmentName="$environment_name" \
    location="$location" \
    resourceGroupName="$resource_group" \
    foundryAgentEndpoint="$foundry_endpoint" \
  --only-show-errors \
  --output none

deployment_json="$(az deployment sub show --name "$deployment_name" --query properties.outputs -o json)"
deployment_output() {
  python3 -c 'import json,sys; outputs=json.load(sys.stdin); wanted=sys.argv[1].lower(); print(next(v["value"] for k,v in outputs.items() if k.lower() == wanted))' "$1" <<<"$deployment_json"
}

api_principal_id="$(deployment_output SERVICE_API_IDENTITY_PRINCIPAL_ID)"
web_name="$(deployment_output SERVICE_WEB_NAME)"
api_name="$(deployment_output SERVICE_API_NAME)"
stream_api_name="$(deployment_output SERVICE_STREAM_API_NAME)"
stream_api_uri="$(deployment_output SERVICE_STREAM_API_URI)"
web_uri="$(deployment_output SERVICE_WEB_URI)"

az webapp config appsettings set \
  --resource-group "$resource_group" \
  --name "$api_name" \
  --settings APP_MODE=control PUBLIC_API_ORIGIN="$stream_api_uri" \
  --only-show-errors \
  --output none
az webapp config appsettings set \
  --resource-group "$resource_group" \
  --name "$stream_api_name" \
  --settings APP_MODE=stream PUBLIC_WEB_ORIGIN="$web_uri" \
  --only-show-errors \
  --output none
az webapp restart --resource-group "$resource_group" --name "$api_name"
az webapp restart --resource-group "$resource_group" --name "$stream_api_name"

az role assignment create \
  --assignee-object-id "$api_principal_id" \
  --assignee-principal-type ServicePrincipal \
  --role "Foundry User" \
  --scope "$foundry_project_id" \
  --only-show-errors \
  --output none

printf 'Provisioned %s and linked API %s.\n' "$web_name" "$api_name"
printf 'Site: %s\n' "$web_uri"
printf 'Wait for RBAC propagation before the first live invocation.\n'
