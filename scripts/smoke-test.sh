#!/usr/bin/env bash
# Live smoke test against the Azure deployment (Gate C): python -m pms smoke-test --part 1|2.
# Usage: bash scripts/smoke-test.sh --part 1|2 [--out smoke-test-result.json] [--base-url URL]
# Uses your Microsoft Entra token for the demo API (az login; app role needed).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

part=""
out="smoke-test-result.json"
base_url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --part)
      [ $# -ge 2 ] || pms_fail "--part needs a value." "Run: bash scripts/smoke-test.sh --part 1"
      part="$2"
      shift
      ;;
    --out)
      [ $# -ge 2 ] || pms_fail "--out needs a file name." "Run: bash scripts/smoke-test.sh --part 1 --out smoke-test-result.json"
      out="$2"
      shift
      ;;
    --base-url)
      [ $# -ge 2 ] || pms_fail "--base-url needs a value." "Run: bash scripts/smoke-test.sh --part 1 --base-url https://<web-app-fqdn>"
      base_url="$2"
      shift
      ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) pms_fail "Unknown argument '$1'." "Run: bash scripts/smoke-test.sh --part 1|2 [--out FILE] [--base-url URL]" ;;
  esac
  shift
done

case "$part" in
  1|2) ;;
  *) pms_fail "Choose the smoke-test part (1 or 2)." "Run: bash scripts/smoke-test.sh --part 1" ;;
esac

PY="$(pms_python)"
[ -n "$base_url" ] || base_url="$(pms_value WEB_URL)"
[ -n "$base_url" ] || pms_fail "The web app URL is unknown." "Run 'azd up' first, or pass --base-url https://<web-app-fqdn>"
pms_api_token
export PYTHONPATH="$PMS_REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
pms_info "Smoke test part $part against $base_url -> $out"
exec "$PY" -m pms smoke-test --base-url "$base_url" --part "$part" --out "$out"
