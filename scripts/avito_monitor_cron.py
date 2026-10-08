#!/usr/bin/env python3
"""Manage one hosting-user cron entry; never disclose other cron entries.

Preflight is sent through SSH on stdin from the exact reviewed commit. Install
and removal run only in protected deployment, with the existing account/key.
"""
import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


CRON_PATH = "/usr/local/bin:/usr/bin:/bin"
BEGIN = b"# BEGIN SERVICE2 AVITO STATUS MONITOR v1\n"
END = b"# END SERVICE2 AVITO STATUS MONITOR v1\n"
SCHEDULE = "7,22,37,52 * * * *"
READY = re.compile(r"AVITO_STATUS_MONITOR_READY(?: (?:configured|enabled|baselined|due|running|failed)=[0-9]+){6}")


class CronError(Exception):
    """Only fixed, non-sensitive codes may reach deployment output."""


def checked_path(value):
    if not value or not value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 or c == "%" for c in value):
        raise CronError("invalid_path")
    path = Path(value)
    if path != path.resolve():
        raise CronError("noncanonical_path")
    return path


def bounded(command, *, timeout=20, **kwargs):
    """Kill the whole child group on a hard deadline, not just its shell."""
    supervised = kwargs.get("env", {}).get("SERVICE2_AVITO_CRON_SUPERVISED") == "1"
    with subprocess.Popen(command, start_new_session=True, **kwargs) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise CronError("process_timeout") from None
        finally:
            if supervised:
                # --foreground keeps the inner timeout and Django in this
                # group. Stop any remaining descendants even when the inner
                # timeout/worker exits before our own deadline.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


class CronManager:
    def __init__(self, app, expected_sha=None):
        self.app = checked_path(str(app))
        self.tmp = checked_path(str(self.app.parent / "tmp"))
        self.private = self.tmp / "service2-avito-cron"
        self.python = self.app.parent / "venv/bin/python"
        self.wrapper = self.app / "scripts/run_avito_status_monitor.sh"
        self.script = self.app / "scripts/avito_monitor_cron.py"
        self.home = checked_path(pwd.getpwuid(os.getuid()).pw_dir)
        self.expected_sha = expected_sha
        self.crontab = shutil.which("crontab", path=CRON_PATH)
        self.env = {"HOME": str(self.home), "PATH": CRON_PATH, "TZ": "UTC", "LC_ALL": "C"}

    def runtime(self):
        if not self.app.is_dir() or not self.tmp.is_dir() or not os.access(self.tmp, os.W_OK):
            raise CronError("hosting_layout_unavailable")
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise CronError("python_unavailable")
        if not all(os.path.isfile(tool) and os.access(tool, os.X_OK) for tool in ("/usr/bin/env", "/bin/bash")):
            raise CronError("cron_launcher_unavailable")
        if not self.crontab:
            raise CronError("crontab_unavailable")
        if not all(shutil.which(tool, path=CRON_PATH) for tool in ("flock", "timeout")):
            raise CronError("required_tools_unavailable")

    def read_table(self):
        result = bounded([self.crontab, "-l"], env=self.env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            # Do not turn permission errors or an unavailable daemon/CLI into
            # an empty table, which could replace another owner's schedules.
            user = pwd.getpwuid(os.getuid()).pw_name.encode()
            if result.returncode == 1 and not result.stdout and result.stderr.strip() == b"no crontab for " + user:
                return b""
            raise CronError("crontab_read_denied_or_failed")
        table = result.stdout
        if len(table) > 1024 * 1024 or b"\0" in table or (table and not table.endswith(b"\n")):
            raise CronError("unsupported_crontab_format")
        return table

    def block(self):
        command = ["/usr/bin/env", "-i", "HOME=" + str(self.home), "PATH=" + CRON_PATH,
                   "TZ=UTC", "LC_ALL=C", str(self.python), str(self.script),
                   "tick", "--app-dir", str(self.app)]
        line = SCHEDULE + " " + shlex.join(command) + " >/dev/null 2>&1\n"
        return BEGIN + line.encode() + END

    def without_block(self, table):
        if BEGIN in table or END in table:
            if table.count(BEGIN) != 1 or table.count(END) != 1:
                raise CronError("ambiguous_managed_block")
            start, end = table.index(BEGIN), table.index(END) + len(END)
            if end <= start or table[start:end] != self.block():
                raise CronError("unexpected_managed_block")
            if start and table[start - 1:start] != b"\n":
                raise CronError("invalid_managed_boundary")
            other = table[:start] + table[end:]
        else:
            other = table
        if any(token in other for token in (b"SERVICE2 AVITO STATUS MONITOR", b"run_avito_status_monitor", b"monitor_avito_statuses", b"avito_monitor_cron.py")):
            raise CronError("unmanaged_monitor_entry")
        return other

    def preflight(self):
        self.runtime()
        table = self.read_table()
        self.without_block(table)
        return table

    def private_directory(self):
        self.private.mkdir(mode=0o700, exist_ok=True)
        info = self.private.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CronError("unsafe_private_directory")

    @contextmanager
    def lock(self, path, *, shared=False):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise CronError("unsafe_lock_file")
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CronError("lock_busy") from None
            yield
        finally:
            os.close(fd)

    def verify_release(self):
        if not self.expected_sha or not re.fullmatch("[0-9a-f]{40}", self.expected_sha):
            raise CronError("exact_sha_required")
        result = bounded(["git", "rev-parse", "HEAD"], cwd=self.app, env=self.env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode or result.stdout.decode().strip() != self.expected_sha:
            raise CronError("deployed_sha_mismatch")
        workflow = (self.app / ".github/workflows/avito-status-monitor.yml").read_text()
        if re.search(r"^\s+schedule\s*:", workflow, re.MULTILINE) or "workflow_dispatch:" not in workflow:
            raise CronError("github_schedule_not_removed")

    def readiness(self):
        result = bounded(["/bin/bash", str(self.wrapper), "--status"], timeout=80,
                         cwd=self.home, env=self.supervised_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode or not READY.fullmatch(result.stdout.decode(errors="replace").strip()):
            raise CronError("cron_environment_readiness_failed")

    def supervised_env(self):
        # Never inherited by cron-table/git commands or manual Actions runs.
        return {**self.env, "SERVICE2_AVITO_CRON_SUPERVISED": "1"}

    def backup(self, table):
        target = self.private / ("before-" + hashlib.sha256(table).hexdigest() + ".crontab")
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or target.read_bytes() != table:
                raise CronError("unsafe_backup") from None
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(table)

    def change(self, *, remove=False):
        self.preflight()
        self.private_directory()
        with ExitStack() as stack:
            stack.enter_context(self.lock(self.private / "installer.lock"))
            stack.enter_context(self.lock(self.tmp / "service2-deploy.lock", shared=True))
            stack.enter_context(self.lock(self.tmp / "service2-avito-status-monitor.lock"))
            self.verify_release()
            if not remove:
                self.readiness()
            before = self.read_table()
            other = self.without_block(before)
            desired = other if remove else (before if self.block() in before else other + self.block())
            if desired == before:
                return "absent" if remove else "present"
            self.backup(before)
            if self.read_table() != before:
                raise CronError("crontab_changed_before_write")
            # stdin avoids exposing any table contents via argv or logs.
            result = subprocess.run([self.crontab, "-"], input=desired, env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            if result.returncode:
                raise CronError("crontab_install_denied_or_failed")
            if self.read_table() != desired:
                # Never restore a full old table over a concurrent panel edit.
                raise CronError("readback_mismatch_review_required")
            return "removed" if remove else "installed"

    def state(self, data):
        encoded = json.dumps(data, sort_keys=True).encode() + b"\n"
        if len(encoded) > 1024:
            raise CronError("oversized_status")
        fd, name = tempfile.mkstemp(prefix=".tick-", dir=self.private)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
            os.replace(name, self.private / "last-tick.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def tick(self):
        self.private_directory()
        try:
            with self.lock(self.private / "tick.lock"):
                started = datetime.now(timezone.utc).isoformat()
                self.state({"started_at": started, "state": "running"})
                try:
                    result = bounded(["/bin/bash", str(self.wrapper)], timeout=1870,
                                     cwd=self.home, env=self.supervised_env(),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    code = result.returncode
                except CronError:
                    code = 124
                self.state({"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                            "state": "finished", "exit_code": code})
                return code
        except CronError as error:
            if str(error) == "lock_busy":
                return 0
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "install", "remove", "tick"))
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--expected-sha")
    args = parser.parse_args(argv)
    try:
        manager = CronManager(args.app_dir, args.expected_sha)
        if args.mode == "preflight":
            manager.preflight()
            print("AVITO_CRON status=preflight_passed")
        elif args.mode == "tick":
            return manager.tick()
        else:
            result = manager.change(remove=args.mode == "remove")
            print("AVITO_CRON status=" + result)
        return 0
    except CronError as error:
        print("AVITO_CRON status=error code=" + str(error), file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Exceptions can contain usernames, paths, or provider output.
        print("AVITO_CRON status=error code=operation_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
