#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SERVICE2_PYTHON:-$APP_DIR/../venv/bin/python}"
TMP_DIR="$APP_DIR/../tmp"

if [[ ! -x "$PYTHON_BIN" || ! -d "$TMP_DIR" || ! -w "$TMP_DIR" ]]; then
    echo "Call AI worker environment is unavailable" >&2
    exit 67
fi

cd "$APP_DIR"

# Keep the lock order identical to update.sh: AI -> deploy.
# Both acquisitions are non-blocking, so workers yield instead of delaying deploys.
exec 8>"$TMP_DIR/service2-call-ai.lock"
if ! flock -n 8; then
    exit 0
fi

exec 9>"$TMP_DIR/service2-deploy.lock"
if ! flock -n 9; then
    exit 0
fi

exec "$PYTHON_BIN" manage.py process_requested_call_analyses --limit 1
