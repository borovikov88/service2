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
CONCURRENCY="${4:-3}"

[[ "$LIMIT" =~ ^[0-9]+$ ]]
[[ "$WAIT_FOR_AI" == "0" || "$WAIT_FOR_AI" == "1" ]]
[[ "$IDLE_GRACE_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]
[[ "$CONCURRENCY" =~ ^[1-4]$ ]]

# Only durable manual launchers participate in the successor mutex.
# Scheduled fallback is deliberately non-waiting and must never consume this slot.
if [[ "$WAIT_FOR_AI" == "1" ]]; then
    exec 7>"$TMP_DIR/service2-call-ai-successor.lock"
    if ! flock -n 7; then
        exit 0
    fi
fi

# Keep the lock order identical to update.sh: AI -> deploy.
# Manual successor waits durably; scheduled fallback passes 0 and never waits.
exec 8>"$TMP_DIR/service2-call-ai.lock"
if [[ "$WAIT_FOR_AI" == "1" ]]; then
    flock 8
else
    if ! flock -n 8; then
        exit 0
    fi
fi

# This process is now the active worker. Free the manual successor slot so one
# later request may wait behind this worker while it drains the durable queue.
if [[ "$WAIT_FOR_AI" == "1" ]]; then
    flock -u 7
    exec 7>&-
fi

# Background readers share the deployment guard. A manual request waits through
# an exclusive deploy/identity-sync holder rather than abandoning its queue row.
# Scheduled fallback remains non-blocking so its GitHub job stays bounded.
exec 9>"$TMP_DIR/service2-deploy.lock"
if [[ "$WAIT_FOR_AI" == "1" ]]; then
    flock -s 9
else
    if ! flock -s -n 9; then
        exit 0
    fi
fi

COMMAND_ARGS=(
    manage.py
    process_requested_call_analyses
    --limit "$LIMIT"
    --idle-grace-seconds "$IDLE_GRACE_SECONDS"
    --concurrency "$CONCURRENCY"
)
if [[ "$WAIT_FOR_AI" == "1" ]]; then
    COMMAND_ARGS+=(--drain)
fi

exec "$PYTHON_BIN" "${COMMAND_ARGS[@]}"
