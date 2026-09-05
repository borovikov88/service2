#!/usr/bin/env bash

# Deploy one already-tested main commit into the existing Passenger checkout.
# This script intentionally does not enable the advisor MCP probe.
set -Eeuo pipefail

EXPECTED_SHA="${1:-}"
APP_DIR="${2:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
MODE="${3:-deploy}"
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Usage: $0 <40-character-main-commit-sha> [application-directory]" >&2
    exit 64
fi
if [[ "$APP_DIR" != /* || ! -d "$APP_DIR/.git" ]]; then
    echo "Application directory must be an absolute Git checkout path" >&2
    exit 64
fi
if [[ "$MODE" != "deploy" && "$MODE" != "preflight" ]]; then
    echo "Mode must be deploy or preflight" >&2
    exit 64
fi
cd "$APP_DIR"

if [[ ! -d ../venv || ! -x ../venv/bin/python || ! -d ../tmp || ! -w ../tmp ]]; then
    echo "Hosting layout is incompatible: ../venv and writable ../tmp are required" >&2
    exit 67
fi
exec 9>../tmp/service2-deploy.lock
if ! flock -n 9; then
    echo "Another service2 deployment is already running" >&2
    exit 75
fi

echo "===== Verifying tested main commit $EXPECTED_SHA ====="
REMOTE_MAIN="$(git ls-remote --exit-code origin refs/heads/main | awk 'NR == 1 {print $1}')"
if [[ "$REMOTE_MAIN" != "$EXPECTED_SHA" ]]; then
    echo "Refusing deploy: origin/main is $REMOTE_MAIN, expected $EXPECTED_SHA" >&2
    exit 65
fi
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "Refusing deploy: production checkout contains local changes; they were not removed" >&2
    exit 66
fi
if [[ "$MODE" == "preflight" ]]; then
    test -f manage.py
    test -f requirements.txt
    echo "===== Hosting preflight passed without changing the checkout ====="
    exit 0
fi

echo "===== Fetching tested main commit $EXPECTED_SHA ====="
git fetch --prune origin main

git checkout --detach "$EXPECTED_SHA"

echo "===== Activating virtual environment ====="
source ../venv/bin/activate

echo "===== Installing pinned application dependencies ====="
python -m pip install --disable-pip-version-check -r requirements.txt --quiet

echo "===== Running Django deployment checks ====="
python manage.py check --deploy

echo "===== Applying migrations ====="
python manage.py migrate --noinput

echo "===== Collecting static files ====="
python manage.py collectstatic --noinput

echo "===== Restarting Passenger ====="
touch ../tmp/restart.txt

echo "===== Deployed $EXPECTED_SHA ====="
