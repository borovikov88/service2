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
WAIT_FOR_AI="${2:-1}"
IDLE_GRACE_SECONDS="${3:-0}"

[[ "$LIMIT" =~ ^[0-9]+$ ]]
[[ "$WAIT_FOR_AI" == "0" || "$WAIT_FOR_AI" == "1" ]]
[[ "$IDLE_GRACE_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]

# Allow at most one durable successor to wait behind the active AI worker.
# Extra button clicks only persist database requests; their launchers exit here.
exec 7>"$TMP_DIR/service2-call-ai-successor.lock"
if ! flock -n 7; then
    exit 0
fi

# Keep the lock order identical to update.sh: AI -> deploy.
# Manual successor waits durably; scheduled fallback passes 0 and never waits.
exec 8>"$TMP_DIR/service2-call-ai.lock"
if [[ "$WAIT_FOR_AI" == "1" ]]; then
    flock 8
elif ! flock -n 8; then
    exit 0
fi

# This process is now the active worker. Release the successor slot immediately
# so at most one new launcher may wait behind it while it drains the queue.
flock -u 7
exec 7>&-

# Background readers share the deployment guard. update.sh keeps its exclusive
# lock, so checkout/venv/migrations can never change underneath this process.
exec 9>"$TMP_DIR/service2-deploy.lock"
if ! flock -s -n 9; then
    exit 0
fi

exec "$PYTHON_BIN" manage.py process_requested_call_analyses \
    --limit "$LIMIT" \
    --idle-grace-seconds "$IDLE_GRACE_SECONDS"
