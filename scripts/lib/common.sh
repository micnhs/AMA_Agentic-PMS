#!/usr/bin/env bash
# Shared helpers for scripts/*.sh. Sourced, not executed.
# shellcheck shell=bash

PMS_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PMS_REPO_ROOT

# Python writes UTF-8 also when its output is piped (for example in Git Bash on Windows).
export PYTHONUTF8=1

pms_info() { printf '[pms] %s\n' "$*"; }
pms_warn() { printf '[pms] WARNING: %s\n' "$*" >&2; }

# Stops with one plain line: what is wrong and how to fix it.
pms_fail() {
  printf '[pms] FAILED: %s Fix: %s\n' "$1" "$2" >&2
  exit 1
}

pms_has() { command -v "$1" >/dev/null 2>&1; }

pms_require_cmd() {
  pms_has "$1" || pms_fail "'$1' was not found on PATH." "$2"
}

# Prints the first Python >= 3.12 interpreter. Use as: PY="$(pms_python)"
pms_python() {
  local candidate
  for candidate in python3.12 python3 python; do
    if pms_has "$candidate" && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  pms_fail "Python 3.12 or newer was not found." "Install Python 3.12+ (https://www.python.org/downloads/) and open a new shell."
}

# True if version $1 >= version $2 (numeric major.minor.patch; suffixes are ignored).
pms_version_ge() {
  "${PY:-python3}" - "$1" "$2" <<'EOF'
import re
import sys


def parse(text):
    numbers = [int(n) for n in re.findall(r"\d+", text)[:3]]
    return numbers + [0] * (3 - len(numbers))


sys.exit(0 if parse(sys.argv[1]) >= parse(sys.argv[2]) else 1)
EOF
}

# Value of an environment variable, else of the selected azd environment, else empty.
pms_value() {
  local key="$1" value
  value="$(env | awk -v k="$key" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }')"
  if [ -z "$value" ] && pms_has azd; then
    value="$(azd env get-value "$key" 2>/dev/null || true)"
    case "$value" in
      *ERROR*|*"not found"*) value="" ;;
    esac
  fi
  printf '%s' "$value"
}

# Stores a value in the selected azd environment (warns if there is none).
pms_azd_set() {
  if azd env set "$1" "$2" >/dev/null 2>&1; then
    return 0
  fi
  pms_warn "Could not store $1 in the azd environment (is one selected? azd env select <name>)."
  return 1
}

# Offline mode: no Azure endpoints, deterministic agents, local test users, in-process worker.
pms_local_env() {
  unset FOUNDRY_PROJECT_ENDPOINT AZURE_SEARCH_ENDPOINT AZURE_SEARCH_INDEX COSMOS_ENDPOINT COSMOS_DATABASE \
    STORAGE_ACCOUNT_URL ACS_ENDPOINT ACS_SENDER_ADDRESS ALERT_RECIPIENTS APPLICATIONINSIGHTS_CONNECTION_STRING \
    COST_SCOPE AZURE_CLIENT_ID ENTRA_TENANT_ID ENTRA_APP_CLIENT_ID MODEL_DEPLOYMENT_ANALYSIS MODEL_DEPLOYMENT_GENERAL \
    PMS_API_TOKEN
  export APP_ENV=local AGENT_BACKEND=offline LOCAL_TEST_USERS=true WORKER_MODE=inprocess PREVIEW_MONITORING=false
  export DATA_DIR="${DATA_DIR:-./var}"
  export PYTHONPATH="$PMS_REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
}

# Exports PMS_API_TOKEN: a Microsoft Entra access token for the demo API (never printed).
pms_api_token() {
  local scope client_id
  pms_require_cmd az "Install the Azure CLI: https://aka.ms/installazurecli"
  scope="$(pms_value ENTRA_API_SCOPE)"
  if [ -z "$scope" ]; then
    client_id="$(pms_value ENTRA_APP_CLIENT_ID)"
    [ -n "$client_id" ] || pms_fail "The Entra app of the demo is unknown (ENTRA_APP_CLIENT_ID)." "Run 'azd provision' first or set ENTRA_API_SCOPE."
    scope="api://${client_id}/user_impersonation"
  fi
  PMS_API_TOKEN="$(az account get-access-token --scope "$scope" --query accessToken -o tsv 2>/dev/null || true)"
  [ -n "$PMS_API_TOKEN" ] || pms_fail "Could not get a Microsoft Entra token for $scope." "Run: az login --scope $scope (and make sure you have a PMS app role, e.g. PMS.Operator)."
  export PMS_API_TOKEN
}
