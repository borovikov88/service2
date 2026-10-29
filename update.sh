#!/usr/bin/env bash

# Deploy one already-tested main commit into the existing Passenger checkout.
# This script intentionally does not enable the advisor MCP probe.
set -Eeuo pipefail
# Read-only Git inspection must not refresh index metadata.
export GIT_OPTIONAL_LOCKS=0

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
# Keep contention diagnostics inline: the reviewed script runs via SSH stdin.
# This only reads the exact failed lock and bounded /proc metadata; it never
# removes a lock, retries acquisition or reads process arguments/environment.
lock_diagnostic() {
    ../venv/bin/python - "$1" "$2" 2>/dev/null <<'PYTHON_LOCK_DIAGNOSTIC' || true
import json
import math
import os
from pathlib import Path
import re
import sys

PREFIX = "SERVICE2_DEPLOY_LOCK_V1 "
MAX_LOCK_BYTES = 262144


def parse_holders(text, identity):
    holders = set()
    for line in text.splitlines():
        parts = line.split()
        # "->" records are waiting requests, not holders. Ignore other lock types.
        if len(parts) != 8 or parts[1:3] != ["FLOCK", "ADVISORY"] or parts[3] not in {"READ", "WRITE"}:
            continue
        try:
            major, minor, inode = parts[5].split(":")
            key = (int(major, 16), int(minor, 16), int(inode, 10))
            pid = int(parts[4], 10)
        except ValueError:
            continue
        if key == identity and pid > 0:
            holders.add((pid, "shared" if parts[3] == "READ" else "exclusive"))
    return sorted(holders)


def bounded_text(path, size):
    with path.open("r", encoding="ascii") as stream:
        value = stream.read(size + 1)
    if len(value) > size:
        raise ValueError("bounded read exceeded")
    return value


def start_ticks(path):
    value = bounded_text(path, 4096)
    tail = value[value.rindex(")") + 1:].split()
    return int(tail[19], 10)


def process_metadata(pid, uptime, ticks, proc=Path("/proc")):
    try:
        path = proc / str(pid)
        start = start_ticks(path / "stat")
        executable = Path(os.readlink(path / "exe")).name
        # Arbitrary comm/argv names can contain personal data. Only known runtime
        # executable basenames are eligible; unknown executable paths stay hidden.
        if not re.fullmatch(r"(?:python(?:[23](?:\.[0-9]{1,2})?)?|bash|dash|sh|timeout|flock|sleep)", executable):
            executable = None
        age = uptime - start / ticks
        if start != start_ticks(path / "stat") or start < 0 or not math.isfinite(age) or age < 0:
            return None, None
        return executable, int(age)
    except (OSError, ValueError, IndexError, OverflowError):
        return None, None


def diagnose(role, fd, proc=Path("/proc")):
    receipt = {"role": role, "state": "unavailable"}
    try:
        opened = os.fstat(fd)
        identity = (os.major(opened.st_dev), os.minor(opened.st_dev), opened.st_ino)
        holders = parse_holders(bounded_text(proc / "locks", MAX_LOCK_BYTES), identity)
        if not holders or len(holders) > 16:
            return receipt
        uptime = float(bounded_text(proc / "uptime", 256).split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
        if not math.isfinite(uptime) or uptime < 0 or ticks <= 0:
            return receipt
        observed = []
        for pid, mode in holders:
            basename, age = process_metadata(pid, uptime, ticks, proc)
            observed.append({"mode": mode, "pid": pid,
                "process_basename": basename, "process_age_seconds": age})
        # A disappearing lock is not evidence about the current blocker.
        current = parse_holders(bounded_text(proc / "locks", MAX_LOCK_BYTES), identity)
        if current != holders:
            return receipt
        return {"role": role, "state": "observed", "holders": observed}
    except (OSError, ValueError, IndexError, OverflowError):
        return receipt


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) == 3 and sys.argv[1] in {"deployment", "call_analysis"} else "unknown"
    try:
        receipt = diagnose(role, int(sys.argv[2]))
    except (ValueError, IndexError):
        receipt = {"role": role, "state": "unavailable"}
    print(PREFIX + json.dumps(receipt, sort_keys=True, separators=(",", ":")))
PYTHON_LOCK_DIAGNOSTIC
}

if [[ "$MODE" == "deploy" ]]; then
    # Keep deployment mutually exclusive with the call-AI worker.
    # Deployment owns both locks for the full mutating phase.
    exec 8>../tmp/service2-call-ai.lock
    if ! flock -n 8; then
        echo "Active call analysis is running; retry deployment after it finishes" >&2
        lock_diagnostic call_analysis 8 >&2
        exit 75
    fi
    exec 9>../tmp/service2-deploy.lock
    if ! flock -n 9; then
        echo "Another service2 deployment is already running" >&2
        lock_diagnostic deployment 9 >&2
        exit 75
    fi
fi

# Keep this helper inline: SSH executes the reviewed script via stdin, while
# files in the existing checkout may still belong to an older release.
static_guard() {
    ../venv/bin/python - "$@" <<'PYTHON'
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root = Path.cwd().resolve()
action = sys.argv[1]

def git(*args):
    return subprocess.check_output(["git", *args], cwd=str(root))

def reject(message):
    print("Refusing deploy: " + message, file=sys.stderr)
    sys.exit(66)

def regular(path):
    return path.is_file() and not path.is_symlink() and path.resolve() == path

if action == "restore":
    backup = Path(sys.argv[2])
    for relative in json.loads((backup / "manifest.json").read_text()):
        destination = root / relative
        if destination.is_symlink() or (destination.exists() and not regular(destination)):
            reject("generated output changed type; preserved copy is at " + str(backup))
        if destination.parent.resolve() != destination.parent:
            reject("generated output parent became a symlink; preserved copy is at " + str(backup))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(backup / "files" / relative), str(destination))
    sys.exit(0)

expected = sys.argv[2]
records = git("status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
tracked = []
all_generated = []
for record in records:
    if not record:
        continue
    status = record[:2]
    # Staged edits, renames, deletions and conflicts are never generated output.
    if status not in (b" M", b"??"):
        reject("checkout contains staged, deleted or otherwise unsupported local changes")
    relative = os.fsdecode(record[3:])
    if not relative.startswith("public_static/"):
        reject("checkout contains local changes outside verified generated static files")
    source_relative = "pool_service/static/" + relative[len("public_static/"):]
    output, source = root / relative, root / source_relative
    if not regular(output) or not regular(source):
        reject("static output or source is missing, non-regular or uses a symlink")
    try:
        entry = git("ls-tree", "HEAD", "--", source_relative).split(None, 3)
        if not entry or entry[0] not in (b"100644", b"100755"):
            reject("static source is not a regular file tracked in HEAD")
        original = git("show", "HEAD:" + source_relative)
    except subprocess.CalledProcessError:
        reject("static source is not tracked in HEAD")
    if output.read_bytes() != original or source.read_bytes() != original:
        reject("static output differs from its unchanged tracked source")
    all_generated.append((relative, source_relative, status))
    if status == b" M":
        tracked.append(relative)

# Preflight does not fetch. Deployment always repeats this check after fetch,
# before restoring any files; it can then inspect the exact target commit.
target_available = subprocess.run(
    ["git", "cat-file", "-e", expected + "^{commit}"],
    cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
).returncode == 0
if action == "preserve" and not target_available:
    reject("target commit is not available for collision checks")
if target_available:
    for relative, source_relative, status in all_generated:
        source_entry = git("ls-tree", expected, "--", source_relative).split(None, 3)
        if not source_entry or source_entry[0] not in (b"100644", b"100755"):
            reject("target removes or changes type of a generated file's source")
        for parent in Path(relative).parents:
            if str(parent) == ".":
                continue
            parent_entry = git("ls-tree", expected, "--", str(parent)).split(None, 3)
            if parent_entry and parent_entry[0] != b"040000":
                reject("target replaces a generated output directory with a non-directory")
        target_entry = git("ls-tree", expected, "--", relative).split(None, 3)
        if status == b"??" and target_entry:
            reject("target tracks a currently untracked generated path")
        if status == b" M" and (not target_entry or target_entry[0] not in (b"100644", b"100755")):
            reject("target removes or changes type of tracked generated output")

if action == "preserve" and tracked:
    backup_parent = root.parent / "tmp"
    resolved_parent = backup_parent.resolve()
    if resolved_parent == root or root in resolved_parent.parents:
        reject("static backup directory must resolve outside the application checkout")
    backup = Path(tempfile.mkdtemp(prefix="service2-static-", dir=str(backup_parent)))
    for relative in tracked:
        saved = backup / "files" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(root / relative), str(saved))
        if saved.read_bytes() != (root / relative).read_bytes():
            reject("generated static backup verification failed")
    (backup / "manifest.json").write_text(json.dumps(tracked))
    print(str(backup))
PYTHON
}


echo "===== Verifying tested main commit $EXPECTED_SHA ====="
REMOTE_MAIN="$(git ls-remote --exit-code origin refs/heads/main | awk 'NR == 1 {print $1}')"
if [[ "$REMOTE_MAIN" != "$EXPECTED_SHA" ]]; then
    echo "Refusing deploy: origin/main is $REMOTE_MAIN, expected $EXPECTED_SHA" >&2
    exit 65
fi
static_guard check "$EXPECTED_SHA"
if [[ "$MODE" == "preflight" ]]; then
    test -f manage.py
    test -f requirements.txt
    echo "===== Hosting preflight passed without changing the checkout ====="
    exit 0
fi

echo "===== Fetching tested main commit $EXPECTED_SHA ====="
git fetch --prune origin main

# Recheck after fetch, retain matched tracked output, then restore only those
# generated paths to HEAD so Git can switch commits. Unknown files stay blocked.
STATIC_BACKUP="$(static_guard preserve "$EXPECTED_SHA")"
restore_generated() {
    if [[ -n "$STATIC_BACKUP" ]]; then
        static_guard restore "$STATIC_BACKUP"
    fi
}
trap restore_generated EXIT
if [[ -n "$STATIC_BACKUP" ]]; then
    ../venv/bin/python - "$STATIC_BACKUP" <<'PYTHON'
import json
from pathlib import Path
import subprocess
import sys
paths = json.loads((Path(sys.argv[1]) / "manifest.json").read_text())
subprocess.check_call(["git", "restore", "--source=HEAD", "--worktree", "--", *paths])
PYTHON
fi
git checkout --detach "$EXPECTED_SHA"
restore_generated
trap - EXIT
if [[ -n "$STATIC_BACKUP" ]]; then
    echo "Verified generated static copies retained at $STATIC_BACKUP"
fi

echo "===== Activating virtual environment ====="
source ../venv/bin/activate

echo "===== Installing pinned application dependencies ====="
python -m pip install --disable-pip-version-check -r requirements.txt --quiet

echo "===== Running Django deployment checks ====="
python manage.py check --deploy

echo "===== Verifying private database backup before migrations ====="
# Borrow the existing EX locks 8/9; never open a second deployment lock here.
# The inner 300s guard stops native children; the outer watchdog fails closed.
timeout --signal=TERM --kill-after=10 330 python scripts/crm_pre_migrate_backup.py --expected-sha "$EXPECTED_SHA"

echo "===== Applying migrations ====="
python manage.py migrate --noinput

PHONE_NORMALIZATION_MARKER="../tmp/client-phone-normalization-v1"
if [[ ! -f "$PHONE_NORMALIZATION_MARKER" ]]; then
    echo "===== Auditing client phones ====="
    python manage.py normalize_client_phones

    echo "===== Normalizing client phones ====="
    python manage.py normalize_client_phones --apply
    touch "$PHONE_NORMALIZATION_MARKER"
else
    echo "===== Client phone normalization already applied ====="
fi

echo "===== Collecting static files ====="
python manage.py collectstatic --noinput

echo "===== Restarting Passenger ====="
touch ../tmp/restart.txt

# Deployment is complete. Release both locks before starting detached
# background workers so they can safely use the freshly deployed checkout.
flock -u 9 || true
flock -u 8 || true
exec 9>&-
exec 8>&-

if [[ -f scripts/run_call_ai_worker.sh ]]; then
    nohup /bin/bash scripts/run_call_ai_worker.sh </dev/null >/dev/null 2>&1 &
fi
if [[ -f scripts/run_call_recording_sync.sh ]]; then
    nohup /bin/bash scripts/run_call_recording_sync.sh 100 </dev/null >/dev/null 2>&1 &
fi

echo "===== Deployed $EXPECTED_SHA ====="

