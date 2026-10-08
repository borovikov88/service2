#!/usr/bin/env bash
# Run only the deployed monitor. Hosting cron supplies the schedule, while
# database due times keep successful checks hourly and failed checks bounded.
set -Eeuo pipefail
umask 077

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SERVICE2_PYTHON:-$APP_DIR/../venv/bin/python}"
TMP_DIR="$APP_DIR/../tmp"
MODE="${1:-run}"

if (( $# > 1 )) || [[ "$MODE" != "run" && "$MODE" != "--status" ]]; then
    echo "AVITO_MONITOR status=invalid_arguments" >&2
    exit 64
fi
if [[ ! -x "$PYTHON_BIN" || ! -d "$TMP_DIR" || ! -w "$TMP_DIR" ||
      ! -f "$APP_DIR/pool_service/management/commands/monitor_avito_statuses.py" ]]; then
    echo "AVITO_MONITOR status=environment_unavailable" >&2
    exit 67
fi
if ! command -v flock >/dev/null || ! command -v timeout >/dev/null; then
    echo "AVITO_MONITOR status=required_tools_unavailable" >&2
    exit 69
fi

cd "$APP_DIR"

if [[ "$MODE" == "run" ]]; then
    # This lock also survives an interrupted SSH client until the bounded
    # server-side process exits. A later tick must not start a duplicate scan.
    exec 7>"$TMP_DIR/service2-avito-status-monitor.lock"
    if flock -n 7; then
        :
    else
        status=$?
        if [[ "$status" == "1" ]]; then
            echo "AVITO_MONITOR status=skipped reason=worker_busy"
            # A supervised skip must not become successful worker-tick evidence.
            if [[ "${SERVICE2_AVITO_CRON_SUPERVISED:-}" == "1" ]]; then exit 75; fi
            exit 0
        fi
        echo "AVITO_MONITOR status=lock_failed exit_code=$status" >&2
        exit "$status"
    fi
fi

# The existing update.sh owns this lock exclusively while replacing code and
# applying migrations. Do not wait and occupy the scheduler during deployment.
exec 9>"$TMP_DIR/service2-deploy.lock"
if flock -s -n 9; then
    :
else
    status=$?
    if [[ "$status" == "1" ]]; then
        echo "AVITO_MONITOR status=skipped reason=deployment_busy"
        # A scheduled tick may retry later; deployment readiness must be proven.
        if [[ "$MODE" == "--status" || "${SERVICE2_AVITO_CRON_SUPERVISED:-}" == "1" ]]; then exit 75; fi
        exit 0
    fi
    echo "AVITO_MONITOR status=lock_failed exit_code=$status" >&2
    exit "$status"
fi

# The cron manager owns a process-group watchdog. Keep GNU timeout in that
# group so the outer watchdog can kill the worker and its descendants. Manual
# calls retain timeout's independent process-group supervision.
TIMEOUT_SUPERVISION=()
if [[ "${SERVICE2_AVITO_CRON_SUPERVISED:-}" == "1" ]]; then
    TIMEOUT_SUPERVISION=(--foreground)
fi

if [[ "$MODE" == "--status" ]]; then
    exec timeout "${TIMEOUT_SUPERVISION[@]}" --signal=TERM --kill-after=10s 60s \
        "$PYTHON_BIN" manage.py monitor_avito_statuses --status
fi

# The command has a 25-minute graceful budget and bounded individual scans.
# The independent host timeout also protects against a lost SSH session or a
# hung dependency. It expires before the 35-minute GitHub job limit.
if timeout "${TIMEOUT_SUPERVISION[@]}" --signal=TERM --kill-after=30s 30m \
    "$PYTHON_BIN" manage.py monitor_avito_statuses --limit 10 --budget-seconds 1500; then
    exit 0
else
    status=$?
    echo "AVITO_MONITOR status=command_failed exit_code=$status" >&2
    exit "$status"
fi
