#!/usr/bin/env bash
# Recomputes the audit hash chain and reports the first break (concept 8).
#   bash scripts/verify-audit.sh                          offline database
#   bash scripts/verify-audit.sh --azure [--base-url URL] Azure deployment, through the API
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

mode="local"
base_url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --azure) mode="azure" ;;
    --base-url)
      [ $# -ge 2 ] || pms_fail "--base-url needs a value." "Run: bash scripts/verify-audit.sh --azure --base-url https://<web-app-fqdn>"
      base_url="$2"
      mode="azure"
      shift
      ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) pms_fail "Unknown argument '$1'." "Run: bash scripts/verify-audit.sh [--azure] [--base-url URL]" ;;
  esac
  shift
done

PY="$(pms_python)"
if [ "$mode" = "local" ]; then
  pms_local_env
  exec "$PY" -m pms verify-audit
fi

[ -n "$base_url" ] || base_url="$(pms_value WEB_URL)"
[ -n "$base_url" ] || pms_fail "The web app URL is unknown." "Run 'azd up' first, or pass --base-url https://<web-app-fqdn>"
pms_api_token
export PYTHONPATH="$PMS_REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m pms verify-audit --base-url "$base_url"
