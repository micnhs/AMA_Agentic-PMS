#!/usr/bin/env bash
# Starts the demo offline: no Azure, no model calls, Python 3.12 standard library only, nothing to install.
# Loads the demo data on start (python -m pms serve --seed) and runs the worker in-process.
# Usage: bash scripts/run-local.sh [--host 127.0.0.1] [--port 8000]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

case "${1:-}" in
  -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
esac

PY="$(pms_python)"
pms_local_env
pms_info "Offline mode: APP_ENV=local, AGENT_BACKEND=offline, data in $DATA_DIR (synthetic data only)."
pms_info "The server prints the URL to open (default http://127.0.0.1:8000; if 8000 is busy, the next free port). Stop with Ctrl+C."
exec "$PY" -m pms serve --seed "$@"
