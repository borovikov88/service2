#!/usr/bin/env bash
# Beget CronTab entry. All business-time calculations use Barnaul.
set -Eeuo pipefail
umask 077
export PATH=/usr/bin:/bin LC_ALL=C
[[ $# == 1 && "$1" =~ ^[1-9][0-9]{0,17}$ ]] || exit 2
read -r day hour <<< "$(TZ=Asia/Barnaul date '+%u %H')"
[[ "$day" =~ ^[1-5]$ && "$hour" =~ ^(09|1[0-7])$ ]] || exit 0
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
app_dir="$(pwd -P)"
tmp="$app_dir/../tmp"
[[ -d "$app_dir/.git" && -d "$tmp" && ! -L "$tmp" && -x "$app_dir/../venv/bin/python" ]] || exit 2
deploy_lock="$tmp/service2-deploy.lock"
tick_lock="$tmp/service2-customer-reminder-$1.lock"
for lock in "$deploy_lock" "$tick_lock"; do
    [[ ! -L "$lock" && ( ! -e "$lock" || ( -f "$lock" && -O "$lock" ) ) ]] || exit 2
done
# Shared with the protected deployment. Never run against a migrating checkout.
exec 8>"$deploy_lock"
flock -s -n 8 || exit 0
exec 9>"$tick_lock"
flock -n 9 || exit 0
exec timeout --signal=TERM --kill-after=5 50 "$app_dir/../venv/bin/python" "$app_dir/manage.py" run_customer_reminders --organization "$1"
