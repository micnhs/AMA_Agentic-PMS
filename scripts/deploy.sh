#!/usr/bin/env bash
# Deploys the demo to Azure: preflight, then 'azd up' (provision + deploy). Nothing is sent to any
# authority; e-mails go only to the configured demo recipients.
# Usage: bash scripts/deploy.sh [-e ENV_NAME] [-l LOCATION] [-s SUBSCRIPTION_ID] [-- extra 'azd up' args]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

env_name=""
location=""
subscription=""
while [ $# -gt 0 ]; do
  case "$1" in
    -e|--environment) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: bash scripts/deploy.sh -e pms-demo"; env_name="$2"; shift ;;
    -l|--location) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: bash scripts/deploy.sh -l swedencentral"; location="$2"; shift ;;
    -s|--subscription) [ $# -ge 2 ] || pms_fail "$1 needs a value." "Run: bash scripts/deploy.sh -s <subscription-id>"; subscription="$2"; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    --) shift; break ;;
    *) pms_fail "Unknown argument '$1'." "Run: bash scripts/deploy.sh [-e ENV] [-l LOCATION] [-s SUBSCRIPTION] [-- azd args]" ;;
  esac
  shift
done

pms_require_cmd azd "Install the Azure Developer CLI: https://aka.ms/azd-install"
pms_require_cmd az "Install the Azure CLI: https://aka.ms/installazurecli"

PY="$(pms_python)"
if [ -n "$env_name" ]; then
  if azd env list --output json 2>/dev/null | "$PY" -c 'import json, sys; names = {str(e.get("Name") or e.get("name")) for e in json.load(sys.stdin)}; sys.exit(0 if sys.argv[1] in names else 1)' "$env_name"; then
    azd env select "$env_name"
  else
    new_args=(env new "$env_name")
    [ -z "$location" ] || new_args+=(--location "$location")
    [ -z "$subscription" ] || new_args+=(--subscription "$subscription")
    azd "${new_args[@]}"
  fi
fi
current_env="$(azd env get-value AZURE_ENV_NAME 2>/dev/null || true)"
case "$current_env" in
  ""|*ERROR*) pms_fail "No azd environment is selected." "Run: bash scripts/deploy.sh -e pms-demo -l swedencentral" ;;
esac
[ -z "$location" ] || azd env set AZURE_LOCATION "$location"
[ -z "$subscription" ] || azd env set AZURE_SUBSCRIPTION_ID "$subscription"

pms_info "Environment: $current_env"
bash "$SCRIPT_DIR/preflight.sh"

export PREFLIGHT_DONE=1
azd up "$@"

pms_info "Deployed. Web app: $(pms_value WEB_URL)"
pms_info "Next: assign app roles to demo users (README), then bash scripts/seed.sh --azure and bash scripts/smoke-test.sh --part 1"
pms_info "Remove everything when done: bash scripts/teardown.sh"
