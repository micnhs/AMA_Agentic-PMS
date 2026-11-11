#!/usr/bin/env bash
# Preflight checks before 'azd provision' (also the azd preprovision hook).
# Every failed check stops with one plain line: the problem and the fix.
# Usage: bash scripts/preflight.sh [--hook]
# Checks: tools, login, region, providers, rbac, entra, models, quota, agents, definitions, email,
#         costapi (warning only), bicep, softdelete. Skip one only if you know why it is a false
#         alarm: PREFLIGHT_SKIP="quota,bicep"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

MIN_AZ_VERSION="2.67.0"
MIN_AZD_VERSION="1.18.0"
EU_REGIONS="swedencentral germanywestcentral francecentral italynorth polandcentral spaincentral westeurope northeurope austriaeast belgiumcentral denmarkeast"
PROVIDERS="Microsoft.App Microsoft.CognitiveServices Microsoft.Search Microsoft.DocumentDB Microsoft.Storage Microsoft.KeyVault Microsoft.ContainerRegistry Microsoft.OperationalInsights Microsoft.Insights Microsoft.AlertsManagement Microsoft.Communication Microsoft.ManagedIdentity Microsoft.Consumption Microsoft.CostManagement Microsoft.PolicyInsights"
# Directory roles that may create app registrations: Global Administrator, Application Administrator,
# Cloud Application Administrator, Application Developer (role template IDs).
APP_CREATOR_ROLES="62e90394-69f5-4237-9190-012177145e10 9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3 158c047a-c907-4556-b7ef-446551a6b5f7 cf1c38e5-3621-4004-a7cb-879624dced7c"

MODE="standalone"
for arg in "$@"; do
  case "$arg" in
    --hook) MODE="azd preprovision hook" ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) pms_fail "Unknown argument '$arg'." "Run: bash scripts/preflight.sh [--hook]" ;;
  esac
done

if [ "${PREFLIGHT_DONE:-}" = "1" ]; then
  pms_info "Preflight already passed in this run (PREFLIGHT_DONE=1); skipping."
  exit 0
fi

skipped() {
  case ",${PREFLIGHT_SKIP:-}," in
    *",$1,"*) pms_warn "Check '$1' skipped (PREFLIGHT_SKIP)."; return 0 ;;
  esac
  return 1
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

pms_info "Preflight ($MODE) for pms-agent-demo"

# --- tools -------------------------------------------------------------------------------------
pms_require_cmd az "Install the Azure CLI: https://aka.ms/installazurecli"
pms_require_cmd azd "Install the Azure Developer CLI: https://aka.ms/azd-install"
PY="$(pms_python)"
if ! skipped tools; then
  az_version="$(az version --query '"azure-cli"' -o tsv 2>/dev/null || true)"
  [ -n "$az_version" ] || pms_fail "Could not read the Azure CLI version." "Reinstall the Azure CLI: https://aka.ms/installazurecli"
  pms_version_ge "$az_version" "$MIN_AZ_VERSION" || pms_fail "Azure CLI $az_version is older than $MIN_AZ_VERSION." "Run: az upgrade"
  azd_version="$(azd version 2>/dev/null | sed -n 's/^azd version \([0-9][0-9.]*\).*/\1/p' | head -n 1)"
  [ -n "$azd_version" ] || pms_fail "Could not read the Azure Developer CLI version." "Reinstall azd: https://aka.ms/azd-install"
  pms_version_ge "$azd_version" "$MIN_AZD_VERSION" || pms_fail "Azure Developer CLI $azd_version is older than $MIN_AZD_VERSION." "Upgrade azd: https://aka.ms/azd-install"
  py_version="$("$PY" -c 'import platform; print(platform.python_version())')"
  pms_info "Tools: az $az_version, azd $azd_version, Python $py_version"
fi

# --- login and subscription --------------------------------------------------------------------
az account show -o none 2>/dev/null || pms_fail "The Azure CLI is not signed in." "Run: az login"
if ! skipped login; then
  azd auth login --check-status >/dev/null 2>&1 || pms_fail "The Azure Developer CLI is not signed in." "Run: azd auth login"
fi
current_sub="$(az account show --query id -o tsv)"
target_sub="$(pms_value AZURE_SUBSCRIPTION_ID)"
if [ -z "$target_sub" ]; then
  target_sub="$current_sub"
elif [ "$(lower "$target_sub")" != "$(lower "$current_sub")" ]; then
  pms_fail "The Azure CLI uses subscription $current_sub but the azd environment targets $target_sub." "Run: az account set --subscription $target_sub"
fi
user_type="$(az account show --query user.type -o tsv)"
pms_info "Subscription: $target_sub (signed in as $user_type)"

# --- region ------------------------------------------------------------------------------------
location="$(lower "$(pms_value AZURE_LOCATION)")"
[ -n "$location" ] || pms_fail "AZURE_LOCATION is not set." "Run: azd env set AZURE_LOCATION swedencentral"
if ! skipped region; then
  case " $EU_REGIONS " in
    *" $location "*) ;;
    *) pms_fail "Region '$location' is not an allowed EU region for this demo." "Run: azd env set AZURE_LOCATION swedencentral (fallback: germanywestcentral)" ;;
  esac
  available="$(az account list-locations --query "length([?name=='$location'])" -o tsv 2>/dev/null || true)"
  [ "$available" = "1" ] || pms_fail "Region '$location' is not available to subscription $target_sub." "Choose another EU region: azd env set AZURE_LOCATION germanywestcentral"
  pms_info "Region: $location (EU)"
fi

# --- resource providers ------------------------------------------------------------------------
if ! skipped providers; then
  registered="$(az provider list --query "[?registrationState=='Registered'].namespace" -o tsv 2>/dev/null || true)"
  for namespace in $PROVIDERS; do
    grep -Fxiq -- "$namespace" <<< "$registered" \
      || pms_fail "Resource provider $namespace is not registered in subscription $target_sub." "Run: az provider register --namespace $namespace --wait"
  done
  pms_info "Resource providers: all registered"
fi

# --- Azure RBAC rights of the deploying identity -----------------------------------------------
if [ "$user_type" = "user" ]; then
  principal_id="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
else
  principal_id="$(az ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv 2>/dev/null || true)"
fi
if ! skipped rbac; then
  [ -n "$principal_id" ] || pms_fail "Could not resolve the object ID of the signed-in identity." "Sign in again (az login) or, if you are sure you are Owner, set PREFLIGHT_SKIP=rbac."
  assigned_roles="$(az role assignment list --assignee "$principal_id" --scope "/subscriptions/$target_sub" --include-inherited --include-groups --query "[].roleDefinitionName" -o tsv 2>/dev/null || true)"
  has_role() { grep -Fxq -- "$1" <<< "$assigned_roles"; }
  if has_role "Owner" || { has_role "Contributor" && has_role "User Access Administrator"; }; then
    pms_info "Azure rights: sufficient (Owner, or Contributor plus User Access Administrator)"
  else
    pms_fail "Your identity needs Owner (or Contributor plus User Access Administrator) on subscription $target_sub for role assignments, the policy assignment and the budget." "Ask a subscription owner to assign the role, then re-run."
  fi
fi

# --- Entra app registration right (or bring your own) ------------------------------------------
existing_app="$(pms_value ENTRA_EXISTING_APP_CLIENT_ID)"
if ! skipped entra; then
  if [ -n "$existing_app" ]; then
    az ad app show --id "$existing_app" -o none 2>/dev/null || az ad sp show --id "$existing_app" -o none 2>/dev/null \
      || pms_fail "The app registration $existing_app (ENTRA_EXISTING_APP_CLIENT_ID) was not found in this tenant." "Check the client ID with the admin who created it."
    pms_info "Entra: using your existing app registration $existing_app (bring your own; see README)"
  elif [ "$user_type" != "user" ]; then
    pms_fail "A service principal usually cannot create Entra app registrations." "Create one as described in the README and set ENTRA_EXISTING_APP_CLIENT_ID (repository variable for the pipeline)."
  else
    can_create="$(lower "$(az rest --method GET --url "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" --query "defaultUserRolePermissions.allowedToCreateApps" -o tsv 2>/dev/null || true)")"
    if [ "$can_create" = "true" ]; then
      pms_info "Entra: users may create app registrations in this tenant"
    else
      directory_roles="$(az rest --method GET --url "https://graph.microsoft.com/v1.0/me/memberOf/microsoft.graph.directoryRole?\$select=roleTemplateId" --query "value[].roleTemplateId" -o tsv 2>/dev/null || true)"
      allowed=0
      for role_id in $APP_CREATOR_ROLES; do
        if grep -Fxq -- "$role_id" <<< "$directory_roles"; then
          allowed=1
        fi
      done
      [ "$allowed" = "1" ] || pms_fail "You may not create Entra app registrations in this tenant." "Ask an admin to create one (README: bring your own app registration), then run: azd env set ENTRA_EXISTING_APP_CLIENT_ID <client-id>"
      pms_info "Entra: your directory role may create app registrations"
    fi
  fi
fi

# --- models, deployment type and quota ---------------------------------------------------------
sku="$(pms_value MODEL_DEPLOYMENT_SKU)"
sku="${sku:-DataZoneStandard}"
case "$sku" in
  DataZoneStandard|Standard) ;;
  *) pms_fail "Model deployment type '$sku' is not allowed (EU processing only; never GlobalStandard)." "Run: azd env set MODEL_DEPLOYMENT_SKU DataZoneStandard" ;;
esac
fallback="$(lower "$(pms_value MODEL_FALLBACK_ALL_GENERAL)")"
resource_group="$(pms_value AZURE_RESOURCE_GROUP)"
foundry_account="$(pms_value AZURE_FOUNDRY_ACCOUNT_NAME)"

while IFS=$'\t' read -r role model_name model_version deployment_name; do
  if [ "$role" = "analysis" ] && [ "$fallback" = "true" ]; then
    pms_info "Model: $model_name not deployed (fallback MODEL_FALLBACK_ALL_GENERAL=true: gpt-6.1-sol for all agents)"
    continue
  fi
  if [ "$role" = "analysis" ]; then
    capacity="$(pms_value MODEL_ANALYSIS_CAPACITY)"
    no_model_fix="Use the documented fallback: azd env set MODEL_FALLBACK_ALL_GENERAL true (or another EU region, e.g. germanywestcentral)."
  else
    capacity="$(pms_value MODEL_GENERAL_CAPACITY)"
    no_model_fix="Choose another EU region where it is offered: azd env set AZURE_LOCATION germanywestcentral"
  fi
  capacity="${capacity:-50}"
  if ! skipped models; then
    offered="$(az cognitiveservices model list --location "$location" --query "[?model.name=='$model_name' && model.version=='$model_version'].model.skus[].name" -o tsv 2>/dev/null || true)"
    grep -Fxq -- "$sku" <<< "$offered" || pms_fail "Model $model_name version $model_version is not offered as $sku in $location." "$no_model_fix"
    pms_info "Model: $model_name $model_version offered as $sku in $location"
  fi
  if ! skipped quota; then
    usage_name="$(az cognitiveservices model list --location "$location" --query "[?model.name=='$model_name' && model.version=='$model_version'].model.skus[] | [?name=='$sku'].usageName | [0]" -o tsv 2>/dev/null || true)"
    if [ -z "$usage_name" ] || [ "$usage_name" = "None" ]; then
      usage_name="OpenAI.$sku.$model_name"
    fi
    quota="$(az cognitiveservices usage list --location "$location" --query "[?name.value=='$usage_name'] | [0].[limit, currentValue]" -o tsv 2>/dev/null || true)"
    [ -n "$quota" ] || pms_fail "No $sku quota ($usage_name) exists for $model_name in $location." "Request quota in the Foundry portal (Operate > Quota) or lower the capacity; then re-run."
    limit="$(printf '%s' "$quota" | cut -f1)"
    used="$(printf '%s' "$quota" | cut -f2)"
    own=0
    if [ -n "$resource_group" ] && [ -n "$foundry_account" ]; then
      own="$(az cognitiveservices account deployment show --resource-group "$resource_group" --name "$foundry_account" --deployment-name "$deployment_name" --query sku.capacity -o tsv 2>/dev/null || true)"
      own="${own:-0}"
    fi
    if ! awk -v l="$limit" -v u="$used" -v o="$own" -v c="$capacity" 'BEGIN { exit !((l - u + o) >= c) }'; then
      free="$(awk -v l="$limit" -v u="$used" -v o="$own" 'BEGIN { print l - u + o }')"
      pms_fail "Only $free (thousand tokens per minute) of $sku quota is free for $model_name in $location; $capacity is requested." "Lower the capacity (azd env set MODEL_$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')_CAPACITY 20) or request more quota."
    fi
    pms_info "Quota: $model_name $capacity requested, limit $limit, in use $used"
  fi
done < <("$PY" "$SCRIPT_DIR/lib/preflight_data.py" models)

# --- Foundry Agent Service (Foundry projects) in the region ------------------------------------
if ! skipped agents; then
  project_regions="$(az provider show --namespace Microsoft.CognitiveServices --query "resourceTypes[?resourceType=='accounts/projects'].locations | [0]" -o tsv 2>/dev/null || true)"
  normalized="$(printf '%s' "$project_regions" | tr '\t' '\n' | tr -d ' ' | tr '[:upper:]' '[:lower:]')"
  if [ -z "$normalized" ]; then
    pms_warn "Could not read the regions of Foundry projects; confirm Foundry Agent Service availability for $location in the documentation."
  elif grep -Fxq -- "$location" <<< "$normalized"; then
    pms_info "Foundry Agent Service: Foundry projects are offered in $location"
  else
    pms_fail "Foundry projects (Agent Service) are not offered in $location." "Run: azd env set AZURE_LOCATION swedencentral"
  fi
fi

# --- built-in role and policy definitions used by the templates --------------------------------
if ! skipped definitions; then
  while IFS=$'\t' read -r kind definition_id expected_name; do
    if [ "$kind" = "role" ]; then
      actual="$(az role definition list --name "$definition_id" --query "[0].roleName" -o tsv 2>/dev/null || true)"
    else
      actual="$(az policy definition show --name "$definition_id" --query displayName -o tsv 2>/dev/null || true)"
    fi
    [ -n "$actual" ] || pms_fail "Built-in $kind definition '$expected_name' ($definition_id) was not found." "Update infra/roles.json with the current ID from the Azure built-in definitions list."
    if [ "$actual" != "$expected_name" ]; then
      pms_warn "Built-in $kind $definition_id is now called '$actual' (expected '$expected_name')."
    fi
  done < <("$PY" "$SCRIPT_DIR/lib/preflight_data.py" definitions)
  pms_info "Built-in role and policy definitions: found"
fi

# --- notification addresses ---------------------------------------------------------------------
email_pattern='^[^@[:space:],]+@[^@[:space:],]+\.[^@[:space:],]+$'
deployer_email="$(pms_value DEPLOYER_EMAIL)"
if [ -z "$deployer_email" ] && [ "$user_type" = "user" ]; then
  detected="$(az ad signed-in-user show --query mail -o tsv 2>/dev/null || true)"
  if grep -Eq -- "$email_pattern" <<< "$detected"; then
    deployer_email="$detected"
    pms_azd_set DEPLOYER_EMAIL "$deployer_email" || true
    pms_info "DEPLOYER_EMAIL set to your account address $deployer_email (budget and alert notifications)"
  fi
fi
if ! skipped email; then
  [ -n "$deployer_email" ] || pms_fail "DEPLOYER_EMAIL is not set (budget and alert notifications)." "Run: azd env set DEPLOYER_EMAIL you@example.com"
  grep -Eq -- "$email_pattern" <<< "$deployer_email" || pms_fail "DEPLOYER_EMAIL '$deployer_email' is not an e-mail address." "Run: azd env set DEPLOYER_EMAIL you@example.com"
  recipients="$(pms_value ALERT_RECIPIENTS)"
  if [ -n "$recipients" ]; then
    IFS=',' read -r -a recipient_list <<< "$recipients"
    for recipient in "${recipient_list[@]}"; do
      recipient="${recipient// /}"
      grep -Eq -- "$email_pattern" <<< "$recipient" || pms_fail "ALERT_RECIPIENTS contains '$recipient', which is not an e-mail address." "Run: azd env set ALERT_RECIPIENTS demo1@example.com,demo2@example.com (demo mailboxes only)"
    done
  fi
  pms_info "Notifications: budget and alerts to $deployer_email; critical-case e-mails to ${recipients:-$deployer_email}"
fi

# --- Cost Management API (warning only, OI-030) --------------------------------------------------
if ! skipped costapi; then
  cost_query='{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}}}}'
  if az rest --method POST --url "https://management.azure.com/subscriptions/$target_sub/providers/Microsoft.CostManagement/query?api-version=2023-11-01" --body "$cost_query" -o none 2>/dev/null; then
    pms_info "Cost Management API: available"
  else
    pms_warn "The Cost Management API did not answer for this subscription (offer type or rights). The cost view will show token-based model costs only, with a notice (OI-030)."
  fi
fi

# --- Bicep compiles (also restores the Microsoft Graph extension) --------------------------------
if ! skipped bicep; then
  bicep_errors="$(az bicep build --file infra/main.bicep --stdout 2>&1 >/dev/null)" || {
    first_error="$(printf '%s\n' "$bicep_errors" | grep -i -m 1 'error' || true)"
    pms_fail "infra/main.bicep does not compile: ${first_error:-see 'az bicep build --file infra/main.bicep'}." "Run: az bicep upgrade; if the Graph extension tag in infra/bicepconfig.json is unknown, set the current one (see docs/handover)."
  }
  pms_info "Bicep: infra/main.bicep compiles"
fi

# --- names reserved by soft-deleted resources of an earlier deployment ---------------------------
if ! skipped softdelete && [ -n "$resource_group" ] && [ "$(az group exists --name "$resource_group" 2>/dev/null || echo true)" = "false" ]; then
  vault="$(pms_value AZURE_KEY_VAULT_NAME)"
  if [ -n "$vault" ] && az keyvault show-deleted --name "$vault" -o none 2>/dev/null; then
    pms_fail "Key Vault $vault of an earlier deployment is soft-deleted with purge protection, so its name is reserved for up to 7 days." "Use a new environment (azd env new <name>) or recover it: az keyvault recover --name $vault"
  fi
  if [ -n "$foundry_account" ]; then
    deleted="$(az cognitiveservices account list-deleted --query "length([?name=='$foundry_account'])" -o tsv 2>/dev/null || echo 0)"
    [ "${deleted:-0}" = "0" ] || pms_fail "Foundry account $foundry_account of an earlier deployment is soft-deleted." "Run: az cognitiveservices account purge --name $foundry_account --resource-group $resource_group --location $location"
  fi
fi

pms_info "Preflight passed."
exit 0
