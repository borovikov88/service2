#!/usr/bin/env bash
# Diagnostic reads only; shared deployment lock and current-main proof remain.
set -Eeuo pipefail
umask 077
export GIT_OPTIONAL_LOCKS=0
fail() { printf 'AVITO_READONLY_REMOTE_BLOCKED\n' >&2; exit 2; }
[[ $# == 2 ]] || fail
avito_sha="$1"
avito_app="$2"
[[ "$avito_sha" =~ ^[0-9a-f]{40}$ && "$avito_app" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail
[[ -d "$avito_app" && ! -L "$avito_app" ]] || fail
cd -- "$avito_app"
[[ "$(pwd -P)" == "$avito_app" && -d .git && ! -L .git ]] || fail
[[ "$(git rev-parse --show-toplevel)" == "$avito_app" ]] || fail
[[ -d ../tmp && ! -L ../tmp && -x ../venv/bin/python && -f manage.py ]] || fail
avito_uid="$(id -u)"
for avito_dir in .git ../tmp; do
    [[ "$(stat -c %u -- "$avito_dir")" == "$avito_uid" ]] || fail
    avito_mode="$(stat -c %a -- "$avito_dir")"
    (( (8#$avito_mode & 0022) == 0 )) || fail
done
[[ ! -L ../tmp/service2-deploy.lock ]] || fail
exec 9>../tmp/service2-deploy.lock
flock -n 9 || fail
[[ ! -L ../tmp/service2-deploy.lock ]] || fail
[[ "$(stat -c '%d:%i' -- ../tmp/service2-deploy.lock)" == "$(stat -Lc '%d:%i' -- /proc/$$/fd/9)" ]] || fail
[[ "$(git rev-parse HEAD)" == "$avito_sha" ]] || fail
git diff --quiet HEAD -- .github pool_service service_site manage.py scripts update.sh || fail
avito_origin="$(git config --get remote.origin.url)" || fail
printf '%s' "$avito_origin" | ../venv/bin/python -c '
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
avito_remote="$(git ls-remote --exit-code origin refs/heads/main)" || fail
read -r avito_remote_sha avito_remote_ref <<< "$avito_remote"
[[ "$avito_remote_sha" == "$avito_sha" && "$avito_remote_ref" == refs/heads/main ]] || fail
exec timeout 360 ../venv/bin/python manage.py audit_avito_readonly --item-ids "7234733937,3842842492"
