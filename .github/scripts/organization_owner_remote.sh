#!/usr/bin/env bash
# Explicit first-owner operation on an already deployed exact main.
set -Eeuo pipefail
umask 077
export GIT_OPTIONAL_LOCKS=0
exec 3>&2 2>/dev/null
fail() { printf 'ORG_OWNER_REMOTE_BLOCKED code=%s\n' "$1" >&3; exit 2; }
trap 'fail command_failed' ERR
[[ $# == 8 ]] || fail invalid_arguments
owner_sha="$1"
owner_app="$2"
owner_org="$3"
owner_user="$4"
owner_mode="$5"
owner_plan="$6"
owner_run="$7"
owner_attempt="$8"
[[ "$owner_sha" =~ ^[a-f0-9]{40}$ ]] || fail invalid_sha
[[ "$owner_app" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail invalid_path
[[ "$owner_org" =~ ^[1-9][0-9]{0,17}$ && "$owner_user" =~ ^[1-9][0-9]{0,17}$ ]] || fail invalid_scope
[[ "$owner_run" =~ ^[1-9][0-9]{0,19}$ && "$owner_attempt" =~ ^[1-9][0-9]{0,5}$ ]] || fail invalid_execution
if [[ "$owner_mode" == preview ]]; then
    [[ "$owner_plan" == - ]] || fail invalid_plan
elif [[ "$owner_mode" == apply ]]; then
    [[ "$owner_plan" =~ ^[a-f0-9]{64}$ ]] || fail invalid_plan
else
    fail invalid_mode
fi
[[ -d "$owner_app" && ! -L "$owner_app" ]] || fail invalid_checkout
cd -- "$owner_app" || fail invalid_checkout
[[ "$(pwd -P)" == "$owner_app" && -d .git && ! -L .git ]] || fail invalid_checkout
[[ "$(git rev-parse --show-toplevel)" == "$owner_app" ]] || fail invalid_checkout
[[ -d ../tmp && ! -L ../tmp && -x ../venv/bin/python ]] || fail runtime_layout
[[ ! -L ../tmp/service2-deploy.lock ]] || fail invalid_lock
if [[ -e ../tmp/service2-deploy.lock ]]; then
    [[ -f ../tmp/service2-deploy.lock && -O ../tmp/service2-deploy.lock ]] || fail invalid_lock
fi
command -v timeout >/dev/null || fail runtime_layout
exec 9>../tmp/service2-deploy.lock
flock -n 9 || fail deployment_busy
[[ "$(git rev-parse HEAD)" == "$owner_sha" ]] || fail head_mismatch
git diff --quiet HEAD -- .github pool_service service_site manage.py scripts update.sh requirements.txt || fail dirty_checkout
[[ -z "$(git ls-files --others --exclude-standard -- .github pool_service service_site manage.py scripts update.sh requirements.txt)" ]] || fail untracked_code
owner_origin="$(git config --get remote.origin.url)"
printf '%s' "$owner_origin" | ../venv/bin/python -c '
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
owner_main="$(timeout 20 git ls-remote --exit-code origin refs/heads/main)" || fail main_unavailable
read -r owner_main_sha owner_main_ref <<< "$owner_main"
[[ "$owner_main_sha" == "$owner_sha" && "$owner_main_ref" == refs/heads/main ]] || fail main_mismatch
owner_args=(--organization-id "$owner_org" --user-id "$owner_user" --expected-sha "$owner_sha"
    --run-id "$owner_run" --run-attempt "$owner_attempt")
if [[ "$owner_mode" == apply ]]; then
    owner_args+=(--apply --expected-plan "$owner_plan")
fi
SERVICE2_OWNER_BOOTSTRAP=1 timeout --signal=TERM --kill-after=10 120 \
    ../venv/bin/python manage.py establish_organization_owner "${owner_args[@]}" 2>&3 3>&-
