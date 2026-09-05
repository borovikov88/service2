#!/usr/bin/env bash

# Deploy one already-tested main commit into the existing Passenger checkout.
# This script intentionally does not enable the advisor MCP probe.
set -Eeuo pipefail

EXPECTED_SHA="${1:-}"
APP_DIR="${2:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Usage: $0 <40-character-main-commit-sha> [application-directory]" >&2
    exit 64
fi
if [[ "$APP_DIR" != /* || ! -d "$APP_DIR/.git" ]]; then
    echo "Application directory must be an absolute Git checkout path" >&2
    exit 64
fi
cd "$APP_DIR"

exec 9>../tmp/service2-deploy.lock
if ! flock -n 9; then
    echo "Another service2 deployment is already running" >&2
    exit 75
fi

echo "===== Fetching tested main commit $EXPECTED_SHA ====="
git fetch --prune origin main
REMOTE_MAIN="$(git rev-parse refs/remotes/origin/main)"
if [[ "$REMOTE_MAIN" != "$EXPECTED_SHA" ]]; then
    echo "Refusing deploy: origin/main is $REMOTE_MAIN, expected $EXPECTED_SHA" >&2
    exit 65
fi
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "Refusing deploy: production checkout contains local changes" >&2
    exit 66
fi

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
