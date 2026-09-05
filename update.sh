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
if [[ "$MODE" == "deploy" ]]; then
    exec 9>../tmp/service2-deploy.lock
    if ! flock -n 9; then
        echo "Another service2 deployment is already running" >&2
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

echo "===== Applying migrations ====="
python manage.py migrate --noinput

echo "===== Collecting static files ====="
python manage.py collectstatic --noinput

echo "===== Restarting Passenger ====="
touch ../tmp/restart.txt

echo "===== Deployed $EXPECTED_SHA ====="

