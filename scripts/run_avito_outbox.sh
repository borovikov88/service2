#!/usr/bin/env bash
# One fixed Services outbox, independent of account checks and owner quiet hours.
set -Eeuo pipefail
umask 077
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$APP_DIR/../venv/bin/python"
TMP_DIR="$APP_DIR/../tmp"
MODE="${1:-run}"
if (( $# > 1 )) || [[ "$MODE" != run && "$MODE" != --status ]]; then
    echo "AVITO_OUTBOX status=invalid_arguments" >&2
    exit 64
fi
if [[ ! -x "$PYTHON_BIN" || ! -d "$TMP_DIR" || ! -w "$TMP_DIR" ||
      ! -f "$APP_DIR/pool_service/management/commands/send_avito_outbox.py" ]]; then
    echo "AVITO_OUTBOX status=environment_unavailable" >&2
    exit 67
fi
if ! command -v flock >/dev/null || ! command -v timeout >/dev/null; then
    echo "AVITO_OUTBOX status=required_tools_unavailable" >&2
    exit 69
fi
cd "$APP_DIR"
if [[ "$MODE" == run ]]; then
    exec 7>"$TMP_DIR/service2-avito-outbox-4.lock"
    flock -n 7 || exit 75
fi
exec 9>"$TMP_DIR/service2-deploy.lock"
flock -s -n 9 || exit 75
TIMEOUT_SUPERVISION=()
if [[ "${SERVICE2_AVITO_CRON_SUPERVISED:-}" == 1 ]]; then
    TIMEOUT_SUPERVISION=(--foreground)
fi
if [[ "$MODE" == --status ]]; then
    exec timeout "${TIMEOUT_SUPERVISION[@]}" --signal=TERM --kill-after=5s 30s \
        "$PYTHON_BIN" manage.py send_avito_outbox --connection-id 4 --status
fi
# Any interrupted in-flight POST remains unknown, never silently requeued.
exec timeout "${TIMEOUT_SUPERVISION[@]}" --signal=TERM --kill-after=10s 120s \
    "$PYTHON_BIN" manage.py send_avito_outbox --connection-id 4 --limit 10 --periodic
