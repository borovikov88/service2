#!/usr/bin/env bash
# Runs only after normal deployment/health; no migrations or recovery actions.
set -Eeuo pipefail
umask 077
export GIT_OPTIONAL_LOCKS=0
# Keep command errors private even before the runner's second allowlist boundary.
exec 3>&2 2>/dev/null
fail() { printf 'CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED phase=%s error=%s\n' "$1" "$2" >&3; exit 2; }
trap 'fail exec command_failed' ERR
[[ $# == 2 ]] || fail arguments invalid_count
backup_sha="$1"
backup_app="$2"
[[ "$backup_sha" =~ ^[0-9a-f]{40}$ ]] || fail arguments invalid_sha
[[ "$backup_app" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail arguments invalid_path
[[ -d "$backup_app" && ! -L "$backup_app" ]] || fail path not_directory
cd -- "$backup_app" || fail path change_failed
[[ "$(pwd -P)" == "$backup_app" && -d .git && ! -L .git ]] || fail path noncanonical_checkout
backup_toplevel="$(git rev-parse --show-toplevel)" || fail git toplevel_failed
[[ "$backup_toplevel" == "$backup_app" ]] || fail git toplevel_mismatch
[[ -d ../tmp && ! -L ../tmp && -x ../venv/bin/python ]] || fail runtime layout
command -v timeout >/dev/null || fail runtime timeout_missing
[[ -f scripts/crm_pilot_db_backup.py && ! -L scripts/crm_pilot_db_backup.py ]] || fail runtime helper_missing
backup_head="$(git rev-parse HEAD)" || fail head read_failed
[[ "$backup_head" == "$backup_sha" ]] || fail head mismatch
git diff --quiet HEAD -- .github pool_service service_site manage.py scripts update.sh requirements.txt || fail git diff_failed
backup_origin="$(git config --get remote.origin.url)" || fail origin read_failed
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
' || fail origin invalid
backup_remote="$(timeout 20 git ls-remote --exit-code origin refs/heads/main)" || fail main lookup_failed
read -r backup_remote_sha backup_remote_ref <<< "$backup_remote"
[[ "$backup_remote_sha" == "$backup_sha" && "$backup_remote_ref" == refs/heads/main ]] || fail main mismatch
# Helper takes and retains the canonical exclusive deployment lock before settings.
if timeout --signal=TERM --kill-after=10 330 ../venv/bin/python scripts/crm_pilot_db_backup.py --expected-sha "$backup_sha" 2>&3 3>&-; then
    exit 0
else
    backup_exit=$?
    # Helper's exit 2 already carries its own fixed record. Do not duplicate it.
    case "$backup_exit" in 126|127) fail exec command_failed ;; esac
    exit "$backup_exit"
fi
