#!/bin/sh
# Container entrypoint for pms-agent-demo. PMS_ROLE selects the process (set per container app in
# infra/modules/container-apps.bicep); extra arguments are passed through.
#   web    -> python -m pms serve --host 0.0.0.0 --port $PORT
#   worker -> python -m pms worker
set -eu

role="${PMS_ROLE:-web}"
case "$role" in
  web)
    exec python -m pms serve --host 0.0.0.0 --port "${PORT:-8000}" "$@"
    ;;
  worker)
    exec python -m pms worker "$@"
    ;;
  *)
    echo "pms entrypoint: unknown PMS_ROLE '$role' (expected web or worker)" >&2
    exit 64
    ;;
esac
