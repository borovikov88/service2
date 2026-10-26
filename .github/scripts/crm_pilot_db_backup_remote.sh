#!/usr/bin/env bash
# Runs only after normal deployment/health; no migrations or recovery actions.
set -Eeuo pipefail
umask 077
export GIT_OPTIONAL_LOCKS=0
fail() { printf 'CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED\n' >&2; exit 2; }
[[ $# == 2 ]] || fail
backup_sha="$1"
backup_app="$2"
[[ "$backup_sha" =~ ^[0-9a-f]{40}$ && "$backup_app" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail
[[ -d "$backup_app" && ! -L "$backup_app" ]] || fail
cd -- "$backup_app"
[[ "$(pwd -P)" == "$backup_app" && -d .git && ! -L .git ]] || fail
[[ "$(git rev-parse --show-toplevel)" == "$backup_app" ]] || fail
[[ -d ../tmp && ! -L ../tmp && -x ../venv/bin/python ]] || fail
[[ "$(git rev-parse HEAD)" == "$backup_sha" ]] || fail
git diff --quiet HEAD -- .github pool_service service_site manage.py scripts update.sh requirements.txt || fail
backup_origin="$(git config --get remote.origin.url)" || fail
printf '%s' "$backup_origin" | ../venv/bin/python -c '
import sys
from urllib.parse import urlsplit
value = sys.stdin.read()
if value == "git@github.com:borovikov88/service2.git":
    raise SystemExit(0)
try:
    url = urlsplit(value)
    ok = (url.scheme == "https" and url.hostname == "github.com"
          and url.port in (None, 443) and url.path == "/borovikov88/service2.git"
          and not url.query and not url.fragment)
except ValueError:
    ok = False
raise SystemExit(0 if ok else 2)
' || fail
backup_remote="$(timeout 20 git ls-remote --exit-code origin refs/heads/main)" || fail
read -r backup_remote_sha backup_remote_ref <<< "$backup_remote"
[[ "$backup_remote_sha" == "$backup_sha" && "$backup_remote_ref" == refs/heads/main ]] || fail
# Helper takes and retains the canonical exclusive deployment lock before settings.
exec timeout --signal=TERM --kill-after=10 330 ../venv/bin/python scripts/crm_pilot_db_backup.py --expected-sha "$backup_sha"
