#!/usr/bin/env bash
# Creates the demo's Microsoft Entra app registration outside the deployment (bring your own).
# Run it yourself if the preflight says that you may create app registrations, or ask someone who may
# (for example an Entra admin) to run it in your tenant with --assign-to and --owner set to your account.
# Same settings as infra/modules/entra-app.bicep (five app roles, scope user_impersonation with Azure CLI
# pre-authorised, ID tokens, access tokens v2, enterprise application with assignment required); assigns
# the app roles and stores the client ID in the selected azd environment (ENTRA_EXISTING_APP_CLIENT_ID).
# Running it again completes the same registration. The postprovision hook adds the Web redirect URI.
# Usage: bash scripts/create-app-registration.sh [-e ENV_NAME] [--assign-to UPN] [--owner UPN] [--roles CSV]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

USAGE="bash scripts/create-app-registration.sh [-e ENV] [--assign-to UPN] [--owner UPN] [--roles CSV]"
env_name=""
assign_to="me"
owner=""
roles=""
while [ $# -gt 0 ]; do
  case "$1" in
    -e|--environment) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: $USAGE"; env_name="$2"; shift ;;
    --assign-to) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: $USAGE"; assign_to="$2"; shift ;;
    --owner) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: $USAGE"; owner="$2"; shift ;;
    --roles) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: $USAGE"; roles="$2"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) pms_fail "Unknown argument '$1'." "Run: $USAGE" ;;
  esac
  shift
done

pms_require_cmd az "Install the Azure CLI: https://aka.ms/installazurecli"
PY="$(pms_python)"
selected="$(pms_value AZURE_ENV_NAME)"
env_name="${env_name:-$selected}"
[ -n "$env_name" ] || pms_fail "No azd environment is selected." "Run: azd env select pms-demo, or pass -e pms-demo"
[ -n "$roles" ] || roles="$(pms_value DEPLOYER_APP_ROLES)"
roles="${roles:-PMS.Operator}"

args=(create --shell sh --environment "$env_name" --roles "$roles" --assign-to "$assign_to")
in_selected=0
if [ "$env_name" = "$selected" ]; then
  # Same environment again: complete the registration of the first run instead of creating a second one.
  in_selected=1
  existing="$(pms_value ENTRA_EXISTING_APP_CLIENT_ID)"
  [ -z "$existing" ] || args+=(--client-id "$existing")
  redirect_uri="$(pms_value WEB_REDIRECT_URI)"
  [ -z "$redirect_uri" ] || args+=(--redirect-uri "$redirect_uri")
fi
[ -z "$owner" ] || args+=(--owner "$owner")

token="$(az account get-access-token --resource-type ms-graph --query accessToken -o tsv 2>/dev/null || true)"
[ -n "$token" ] || pms_fail "Could not get a Microsoft Graph token from the Azure CLI." "Run: az login"
result_file="$(mktemp)"
trap 'rm -f "$result_file"' EXIT
status=0
PMS_GRAPH_TOKEN="$token" "$PY" "$SCRIPT_DIR/lib/entra_app.py" "${args[@]}" --result "$result_file" || status=$?
[ "$status" -eq 0 ] || exit "$status"

client_id="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["clientId"])' "$result_file")"
if [ "$in_selected" = "1" ] && pms_azd_set ENTRA_EXISTING_APP_CLIENT_ID "$client_id"; then
  pms_info "Stored ENTRA_EXISTING_APP_CLIENT_ID=$client_id in the azd environment '$env_name'."
  pms_info "Next: bash scripts/deploy.sh -e $env_name (it reuses a resource group left by an earlier attempt)."
else
  pms_info "Give the deployer this command for the azd environment '$env_name':"
  pms_info "  azd env set ENTRA_EXISTING_APP_CLIENT_ID $client_id"
fi
