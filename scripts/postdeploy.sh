#!/usr/bin/env bash
# azd postdeploy hook: waits until GET /healthz answers, builds the Azure AI Search knowledge index and publishes
# changed Foundry prompt agents (both idempotent), then prints the next steps.
# AUTO_SEED=true also loads the demo data through the API (needs the PMS.Operator role).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
cd "$PMS_REPO_ROOT"

PY="$(pms_python)"
web_url="$(pms_value WEB_URL)"
if [ -z "$web_url" ]; then
  pms_warn "WEB_URL is unknown; run 'azd provision' first."
  exit 0
fi

pms_info "Waiting for $web_url/healthz (up to 5 minutes) ..."
if "$PY" - "$web_url/healthz" <<'EOF'
import sys
import time
import urllib.request

deadline = time.time() + 300
while time.time() < deadline:
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=10) as response:
            if response.status == 200:
                sys.exit(0)
    except OSError:
        pass
    time.sleep(10)
sys.exit(1)
EOF
then
  pms_info "Web app is healthy: $web_url"
else
  pms_warn "No healthy answer from $web_url/healthz yet. Check: az containerapp logs show --name $(pms_value WEB_APP_NAME) --resource-group $(pms_value AZURE_RESOURCE_GROUP)"
fi

# Knowledge index and Foundry prompt agents. Both steps are idempotent: only changed index documents are uploaded
# and an agent gets a new version only when its prompt/specification changed (versions are kept in Cosmos DB).
# The worker container runs the same steps on every start with its managed identity; here they run with your
# sign-in when the Azure Python packages are installed (pip install -r requirements.txt).
if "$PY" -c "import azure.identity, azure.cosmos, azure.search.documents, agent_framework" >/dev/null 2>&1; then
  for name in APP_ENV AGENT_BACKEND LOCAL_TEST_USERS WORKER_MODE FOUNDRY_PROJECT_ENDPOINT MODEL_DEPLOYMENT_ANALYSIS \
      MODEL_DEPLOYMENT_GENERAL AZURE_SEARCH_ENDPOINT AZURE_SEARCH_INDEX COSMOS_ENDPOINT COSMOS_DATABASE; do
    value="$(pms_value "$name")"
    if [ -n "$value" ]; then
      export "$name=$value"
    fi
  done
  export PYTHONPATH="$PMS_REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  worker_logs="az containerapp logs show --name $(pms_value WORKER_APP_NAME) --resource-group $(pms_value AZURE_RESOURCE_GROUP)"
  pms_info "Knowledge index: python -m pms seed --index-only"
  "$PY" -m pms seed --index-only || pms_warn "Index not updated from here (role 'Search Index Data Contributor' missing?). The worker container does it on start: $worker_logs"
  pms_info "Foundry prompt agents: python -m pms publish-agents"
  "$PY" -m pms publish-agents || pms_warn "Agents not published from here. The worker container publishes changed agents on start: $worker_logs"
else
  pms_info "Knowledge index and Foundry agents: done by the worker container on start (managed identity). To run them here: pip install -r requirements.txt"
fi

if [ "$(pms_value AUTO_SEED)" = "true" ]; then
  bash "$SCRIPT_DIR/seed.sh" --azure || pms_warn "Seeding failed; run bash scripts/seed.sh --azure after checking your app role."
fi

pms_info "Next steps:"
pms_info "  1. Assign demo users their app roles (Entra admin center > Enterprise applications > PMS agent demo)."
pms_info "  2. Load demo data: bash scripts/seed.sh --azure"
pms_info "  3. Smoke test: bash scripts/smoke-test.sh --part 1"
pms_info "  4. When done: bash scripts/teardown.sh"
exit 0
