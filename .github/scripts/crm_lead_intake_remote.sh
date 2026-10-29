#!/usr/bin/env bash
# Fixed owner-approved CRM operation, after protected exact-commit deployment.
set -Eeuo pipefail
umask 077
export GIT_OPTIONAL_LOCKS=0
exec 3>&2 2>/dev/null
fail() { printf 'CRM_LEAD_INTAKE_ACCESS_REMOTE_BLOCKED code=%s\n' "$1" >&3; exit 2; }
trap 'fail command_failed' ERR
[[ $# == 4 ]] || fail invalid_arguments
access_sha="$1"
access_app="$2"
access_run="$3"
access_attempt="$4"
[[ "$access_sha" =~ ^[0-9a-f]{40}$ ]] || fail invalid_sha
[[ "$access_app" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail invalid_path
[[ "$access_run" =~ ^[1-9][0-9]{0,19}$ && "$access_attempt" =~ ^[1-9][0-9]{0,5}$ ]] || fail invalid_execution
[[ -d "$access_app" && ! -L "$access_app" ]] || fail invalid_checkout
cd -- "$access_app" || fail invalid_checkout
[[ "$(pwd -P)" == "$access_app" && -d .git && ! -L .git ]] || fail invalid_checkout
[[ "$(git rev-parse --show-toplevel)" == "$access_app" ]] || fail invalid_checkout
[[ -d ../tmp && ! -L ../tmp && -x ../venv/bin/python ]] || fail runtime_layout
[[ ! -L ../tmp/service2-deploy.lock ]] || fail invalid_lock
if [[ -e ../tmp/service2-deploy.lock ]]; then
    [[ -f ../tmp/service2-deploy.lock && -O ../tmp/service2-deploy.lock ]] || fail invalid_lock
fi
command -v timeout >/dev/null || fail runtime_layout
# Hold the same exclusive lock as update.sh for every checkout check and DB write.
exec 9>../tmp/service2-deploy.lock
flock -n 9 || fail deployment_busy
[[ "$(git rev-parse HEAD)" == "$access_sha" ]] || fail head_mismatch
git diff --quiet HEAD -- .github pool_service service_site manage.py scripts update.sh requirements.txt || fail dirty_checkout
[[ -z "$(git ls-files --others --exclude-standard -- .github pool_service service_site manage.py scripts update.sh requirements.txt)" ]] || fail untracked_code
access_message="$(git show -s --format=%B HEAD)"
[[ "$access_message" == *'[crm-lead-intake-aqualine-20261026]'* ]] || fail approval_marker_missing
access_origin="$(git config --get remote.origin.url)"
printf '%s' "$access_origin" | ../venv/bin/python -c '
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
' || fail origin_mismatch
access_main="$(timeout 20 git ls-remote --exit-code origin refs/heads/main)" || fail main_unavailable
read -r access_main_sha access_main_ref <<< "$access_main"
[[ "$access_main_sha" == "$access_sha" && "$access_main_ref" == refs/heads/main ]] || fail main_mismatch
SERVICE2_CRM_LEAD_INTAKE_ACCESS=1 timeout --signal=TERM --kill-after=10 120 \
    ../venv/bin/python manage.py provision_crm_lead_intake \
    --expected-sha "$access_sha" --apply --approval-id crm-lead-intake-aqualine-20261026 \
    --run-id "$access_run" --run-attempt "$access_attempt" 2>&3 3>&-
