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

LIMIT="${1:-10}"
LOCK_WAIT_SECONDS="${2:-5}"
IDLE_GRACE_SECONDS="${3:-0}"

[[ "$LIMIT" =~ ^[0-9]+$ ]]
[[ "$LOCK_WAIT_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]
[[ "$IDLE_GRACE_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]

# Keep the lock order identical to update.sh: AI -> deploy.
# Manual successors wait briefly for the current worker. This closes the tiny
# race where a request is queued just before the previous worker releases lock 8.
exec 8>"$TMP_DIR/service2-call-ai.lock"
if ! flock -w "$LOCK_WAIT_SECONDS" 8; then
    exit 0
fi

exec 9>"$TMP_DIR/service2-deploy.lock"
if ! flock -n 9; then
    exit 0
fi

exec "$PYTHON_BIN" manage.py process_requested_call_analyses \
    --limit "$LIMIT" \
    --idle-grace-seconds "$IDLE_GRACE_SECONDS"
