#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SERVICE2_PYTHON:-$APP_DIR/../venv/bin/python}"
TMP_DIR="$APP_DIR/../tmp"

if [[ ! -x "$PYTHON_BIN" || ! -d "$TMP_DIR" || ! -w "$TMP_DIR" ]]; then
    echo "Call recording sync environment is unavailable" >&2
    exit 67
fi

LIMIT="${1:-100}"
[[ "$LIMIT" =~ ^[0-9]+$ ]]
if (( LIMIT < 1 || LIMIT > 500 )); then
    echo "Call recording sync limit must be between 1 and 500" >&2
    exit 64
fi

cd "$APP_DIR"

# Only one provider/recording sync may run at a time. The command is idempotent,
# but a dedicated mutex avoids duplicate MegaFon API requests and download races.
exec 7>"$TMP_DIR/service2-call-recordings.lock"
if ! flock -n 7; then
    echo "Call recording sync already running; skipping."
    exit 0
fi

# The sync is a background reader/writer of application data and must not use a
# checkout while deployment is replacing it. Share the deploy guard.
exec 9>"$TMP_DIR/service2-deploy.lock"
flock -s -w 600 9

exec "$PYTHON_BIN" manage.py sync_call_recordings --limit "$LIMIT"
