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
LOCK_WAIT_SECONDS="${2:-}"
IDLE_GRACE_SECONDS="${3:-0}"

[[ "$LIMIT" =~ ^[0-9]+$ ]]
if [[ -n "$LOCK_WAIT_SECONDS" ]]; then
    [[ "$LOCK_WAIT_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]
fi
[[ "$IDLE_GRACE_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]

# Keep the lock order identical to update.sh: AI -> deploy.
# Manual successors wait until the current worker finishes, providing a durable
# handoff for requests queued during the final item. Scheduled fallback passes
# zero and remains non-blocking.
exec 8>"$TMP_DIR/service2-call-ai.lock"
if [[ -z "$LOCK_WAIT_SECONDS" ]]; then
    flock 8
elif [[ "$LOCK_WAIT_SECONDS" == "0" || "$LOCK_WAIT_SECONDS" == "0.0" ]]; then
    if ! flock -n 8; then
        exit 0
    fi
elif ! flock -w "$LOCK_WAIT_SECONDS" 8; then
    exit 0
fi

# Background readers share the deployment guard. update.sh keeps its exclusive
# lock, so checkout/venv/migrations can never change underneath this process.
exec 9>"$TMP_DIR/service2-deploy.lock"
if ! flock -s -n 9; then
    exit 0
fi

exec "$PYTHON_BIN" manage.py process_requested_call_analyses \
    --limit "$LIMIT" \
    --idle-grace-seconds "$IDLE_GRACE_SECONDS"
