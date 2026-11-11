#!/usr/bin/env bash
# azd postprovision hook. Best effort: it never fails the deployment and prints the fix instead.
#   1. App registration created by Bicep: pre-authorise Azure CLI for the API scope, so
#      'az account get-access-token' works for scripts/seed.sh and scripts/smoke-test.sh.
#   2. Bring-your-own app registration: print the settings it must have.
#   3. Assign the signed-in user the app roles in DEPLOYER_APP_ROLES (default PMS.Operator).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

GRAPH="https://graph.microsoft.com/v1.0"
PY="$(pms_python)"
client_id="$(pms_value ENTRA_APP_CLIENT_ID)"
created="$(pms_value ENTRA_APP_CREATED)"
redirect_uri="$(pms_value WEB_REDIRECT_URI)"
api_scope="$(pms_value ENTRA_API_SCOPE)"
user_type="$(az account show --query user.type -o tsv 2>/dev/null || true)"

if [ -z "$client_id" ]; then
  pms_warn "ENTRA_APP_CLIENT_ID is empty; skipping the Entra follow-up."
  exit 0
fi

if [ "$created" = "true" ]; then
  app_object_id="$(az ad app show --id "$client_id" --query id -o tsv 2>/dev/null || true)"
  api_json="$(az ad app show --id "$client_id" --query api -o json 2>/dev/null || true)"
  if [ -z "$app_object_id" ] || [ -z "$api_json" ]; then
    pms_warn "Could not read app registration $client_id. Fix: pre-authorise Azure CLI (04b07795-8ddb-461a-bbee-02f9e1bf7b46) for user_impersonation under 'Expose an API'."
  else
    patch_body="$(printf '%s' "$api_json" | "$PY" "$SCRIPT_DIR/lib/entra_api_patch.py" 2>/dev/null || true)"
    if [ -z "$patch_body" ]; then
      pms_info "Azure CLI is already pre-authorised for $api_scope."
    elif az rest --method PATCH --url "$GRAPH/applications/$app_object_id" --headers "Content-Type=application/json" --body "$patch_body" -o none 2>/dev/null; then
      pms_info "Pre-authorised Azure CLI for $api_scope (tokens for seed and smoke test)."
    else
      pms_warn "Could not pre-authorise Azure CLI. Fix: Entra admin center > App registrations > $client_id > Expose an API > add client application 04b07795-8ddb-461a-bbee-02f9e1bf7b46 for user_impersonation."
    fi
  fi
else
  pms_info "Bring-your-own app registration $client_id: make sure it has"
  pms_info "  - Web redirect URI: $redirect_uri"
  pms_info "  - ID tokens enabled (Authentication > Implicit grant and hybrid flows), single tenant"
  pms_info "  - app roles PMS.Viewer, PMS.Analyst, PMS.Regulatory, PMS.MedicalReviewer, PMS.Operator (allowed member type User)"
  pms_info "  - Expose an API: scope user_impersonation, with Azure CLI (04b07795-8ddb-461a-bbee-02f9e1bf7b46) as authorised client"
  pms_info "  - access token version 2 (manifest: requestedAccessTokenVersion = 2)"
fi

if [ "$user_type" != "user" ]; then
  pms_info "Signed in as a service principal: assign demo users their app roles in the Entra admin center."
  exit 0
fi

roles="$(pms_value DEPLOYER_APP_ROLES)"
roles="${roles:-PMS.Operator}"
service_principal_id="$(az ad sp show --id "$client_id" --query id -o tsv 2>/dev/null || true)"
me="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
if [ -z "$service_principal_id" ] || [ -z "$me" ]; then
  pms_warn "Could not read the enterprise application or your user. Fix: Entra admin center > Enterprise applications > $client_id > Users and groups > assign yourself $roles."
  exit 0
fi
IFS=',' read -r -a role_list <<< "$roles"
for role in "${role_list[@]}"; do
  role="${role// /}"
  [ -n "$role" ] || continue
  role_id="$(az ad sp show --id "$client_id" --query "appRoles[?value=='$role'].id | [0]" -o tsv 2>/dev/null || true)"
  if [ -z "$role_id" ] || [ "$role_id" = "None" ]; then
    pms_warn "App role $role does not exist on $client_id."
    continue
  fi
  existing="$(az rest --method GET --url "$GRAPH/servicePrincipals/$service_principal_id/appRoleAssignedTo" --query "length(value[?principalId=='$me' && appRoleId=='$role_id'])" -o tsv 2>/dev/null || true)"
  if [ -n "$existing" ] && [ "$existing" != "0" ]; then
    pms_info "You already have the app role $role."
    continue
  fi
  body="{\"principalId\":\"$me\",\"resourceId\":\"$service_principal_id\",\"appRoleId\":\"$role_id\"}"
  if az rest --method POST --url "$GRAPH/servicePrincipals/$service_principal_id/appRoleAssignedTo" --headers "Content-Type=application/json" --body "$body" -o none 2>/dev/null; then
    pms_info "Assigned you the app role $role (sign in again to get it in your token)."
  else
    pms_warn "Could not assign $role to you. Fix: Entra admin center > Enterprise applications > $client_id > Users and groups > Add user/group ($role)."
  fi
done
exit 0
