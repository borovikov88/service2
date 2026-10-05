#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SERVICE2_PYTHON:-$APP_DIR/../venv/bin/python}"
TMP_DIR="$APP_DIR/../tmp"
RUN_ID="${1:-}"

if [[ ! -x "$PYTHON_BIN" || ! -d "$TMP_DIR" || ! -w "$TMP_DIR" ]]; then
    echo "Client import worker environment is unavailable" >&2
    exit 67
fi
if [[ ! "$RUN_ID" =~ ^[0-9]+$ ]]; then
    echo "Invalid client import run id" >&2
    exit 64
fi

cd "$APP_DIR"

exec 8>"$TMP_DIR/service2-client-import.lock"
flock 8

exec 9>"$TMP_DIR/service2-deploy.lock"
flock -s 9

exec "$PYTHON_BIN" manage.py process_client_import_run "$RUN_ID"
