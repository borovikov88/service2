#!/usr/bin/env bash
# Bounded SQL delivery for the specifically enabled Aqualine organization.
set -Eeuo pipefail
umask 077
export GIT_OPTIONAL_LOCKS=0
exec 3>&2 2>/dev/null
fail() { printf 'CRM_INTAKE_WORKER_BLOCKED\n' >&3; exit 2; }
trap fail ERR
[[ $# == 2 ]] || fail
# Event launcher owns the process group and its outer timeout. Keep nested
# timeout commands in that group; scheduled invocations retain existing behavior.
intake_timeout_options=()
if [[ "${CRM_INTAKE_EVENT_PROCESS_GROUP:-0}" == 1 ]]; then
    intake_timeout_options=(--foreground)
fi
intake_sha="$1"
intake_app="$2"
[[ "$intake_sha" =~ ^[0-9a-f]{40}$ && "$intake_app" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail
[[ -d "$intake_app" && ! -L "$intake_app" ]] || fail
cd -- "$intake_app"
[[ "$(pwd -P)" == "$intake_app" && -d .git && ! -L .git ]] || fail
[[ -d ../tmp && ! -L ../tmp && -x ../venv/bin/python ]] || fail
for intake_lock in ../tmp/service2-deploy.lock ../tmp/service2-crm-intake.lock; do
    [[ ! -L "$intake_lock" ]] || fail
    if [[ -e "$intake_lock" ]]; then
        [[ -f "$intake_lock" && -O "$intake_lock" ]] || fail
    fi
done
exec 8>../tmp/service2-crm-intake.lock
if ! flock -n 8; then printf '{"status":"busy","processed":0}\n'; exit 0; fi
exec 9>../tmp/service2-deploy.lock
if ! flock -s -n 9; then printf '{"status":"deploying","processed":0}\n'; exit 0; fi
if [[ "$(git rev-parse HEAD)" != "$intake_sha" ]]; then
    printf '{"status":"release_pending","processed":0}\n'; exit 0
fi
git diff --quiet HEAD -- .github pool_service service_site manage.py scripts update.sh requirements.txt || fail
[[ -z "$(git ls-files --others --exclude-standard -- .github pool_service service_site manage.py scripts update.sh requirements.txt)" ]] || fail
intake_main="$(timeout "${intake_timeout_options[@]}" 20 git ls-remote --exit-code origin refs/heads/main)" || fail
read -r intake_main_sha intake_main_ref <<< "$intake_main"
if [[ "$intake_main_sha" != "$intake_sha" || "$intake_main_ref" != refs/heads/main ]]; then
    printf '{"status":"release_pending","processed":0}\n'; exit 0
fi
timeout "${intake_timeout_options[@]}" --signal=TERM --kill-after=10 55 ../venv/bin/python manage.py run_crm_intake \
    --organization 1 --limit 100 --seconds 40
