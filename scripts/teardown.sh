#!/usr/bin/env bash
# Deletes every Azure resource of the selected azd environment ('azd down --purge') after an explicit
# confirmation, then removes what lives outside the resource group: the custom e-mail sender role
# and the Entra app registration created by the deployment (never a bring-your-own registration).
# Usage: bash scripts/teardown.sh [--confirm ENV_NAME]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

confirmation=""
case "${1:-}" in
  --confirm)
    [ $# -ge 2 ] || pms_fail "--confirm needs the environment name." "Run: bash scripts/teardown.sh --confirm <env-name>"
    confirmation="$2"
    ;;
  -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  "") ;;
  *) pms_fail "Unknown argument '$1'." "Run: bash scripts/teardown.sh [--confirm ENV_NAME]" ;;
esac

pms_require_cmd azd "Install the Azure Developer CLI: https://aka.ms/azd-install"
pms_require_cmd az "Install the Azure CLI: https://aka.ms/installazurecli"

env_name="$(pms_value AZURE_ENV_NAME)"
[ -n "$env_name" ] || pms_fail "No azd environment is selected." "Run: azd env select <env-name>"
resource_group="$(pms_value AZURE_RESOURCE_GROUP)"
subscription="$(pms_value AZURE_SUBSCRIPTION_ID)"
app_created="$(pms_value ENTRA_APP_CREATED)"
app_client_id="$(pms_value ENTRA_APP_CLIENT_ID)"
role_id="$(pms_value AZURE_ACS_EMAIL_ROLE_ID)"
vault="$(pms_value AZURE_KEY_VAULT_NAME)"

pms_info "This permanently deletes environment '$env_name':"
pms_info "  - resource group '${resource_group:-rg-$env_name}' with all resources and data (azd down --purge)"
[ -z "$role_id" ] || pms_info "  - the custom role 'PMS demo ACS e-mail sender' ($role_id)"
if [ "$app_created" = "true" ] && [ -n "$app_client_id" ]; then
  pms_info "  - the Entra app registration $app_client_id created by this deployment"
fi
if [ -z "$confirmation" ]; then
  if [ ! -t 0 ]; then
    pms_fail "No confirmation given in a non-interactive shell; nothing was deleted." "Run: bash scripts/teardown.sh --confirm $env_name"
  fi
  read -r -p "[pms] Type the environment name '$env_name' to confirm: " confirmation
fi
[ "$confirmation" = "$env_name" ] || pms_fail "The confirmation did not match '$env_name'; nothing was deleted." "Re-run and type the exact environment name."

down_status=0
azd down --purge --force || down_status=$?
if [ -n "$resource_group" ] && [ "$(az group exists --name "$resource_group" 2>/dev/null || echo true)" = "true" ]; then
  pms_fail "Resource group $resource_group still exists (azd down exit code $down_status)." "Check the error above and re-run bash scripts/teardown.sh"
fi
[ "$down_status" = "0" ] || pms_warn "azd down reported exit code $down_status, but the resource group is gone (a purge step may have been refused)."

if [ -n "$role_id" ] && [ -n "$subscription" ]; then
  if az role definition delete --name "$role_id" --scope "/subscriptions/$subscription" -o none 2>/dev/null; then
    pms_info "Deleted the custom e-mail sender role."
  else
    pms_warn "Could not delete custom role $role_id; delete it in the portal (Subscription > Access control > Roles)."
  fi
fi

if [ "$app_created" = "true" ] && [ -n "$app_client_id" ]; then
  app_object_id="$(az ad app show --id "$app_client_id" --query id -o tsv 2>/dev/null || true)"
  if [ -n "$app_object_id" ] && az ad app delete --id "$app_client_id" 2>/dev/null; then
    az rest --method DELETE --url "https://graph.microsoft.com/v1.0/directory/deletedItems/$app_object_id" -o none 2>/dev/null \
      || pms_warn "The app registration is in the Entra recycle bin for 30 days (could not delete it permanently)."
    pms_info "Deleted the Entra app registration $app_client_id."
  else
    pms_warn "Could not delete the Entra app registration $app_client_id; delete it in the Entra admin center."
  fi
fi

if [ -n "$vault" ]; then
  pms_info "Note: Key Vault $vault has purge protection and stays soft-deleted for 7 days; redeploy within that time with a new environment name."
fi
pms_info "Teardown finished."
