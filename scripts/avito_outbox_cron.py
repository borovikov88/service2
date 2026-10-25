#!/usr/bin/env python3
"""A separate minute signal for explicitly authorised Services replies.

Reuse the existing private, compare-before-write cron manager. This never
changes account-monitor jobs, fetches XML, or invokes the unscoped sender.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

_spec = importlib.util.spec_from_file_location(
    "service2_shared_monitor_cron", Path(__file__).with_name("avito_monitor_cron.py"))
_shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shared)
CronError = _shared.CronError
bounded = _shared.bounded

BEGIN = b"# BEGIN SERVICE2 AVITO SERVICES OUTBOX v1\n"
END = b"# END SERVICE2 AVITO SERVICES OUTBOX v1\n"
SCHEDULE = "* * * * *"
MAX_STATUS_BYTES = 16384


class OutboxCronManager(_shared.CronManager):
    begin = BEGIN
    end = END
    schedule = SCHEDULE
    unmanaged_tokens = (
        b"SERVICE2 AVITO SERVICES OUTBOX", b"run_avito_outbox",
        b"send_avito_outbox", b"avito_outbox_cron.py",
    )
    worker_lock_name = "service2-avito-outbox-4.lock"

    def __init__(self, app, expected_sha=None):
        super().__init__(app, expected_sha)
        self.private = self.tmp / "service2-avito-outbox-cron-4"
        self.wrapper = self.app / "scripts/run_avito_outbox.sh"
        self.script = self.app / "scripts/avito_outbox_cron.py"

    def verify_release(self):
        if not self.expected_sha or not re.fullmatch("[0-9a-f]{40}", self.expected_sha):
            raise CronError("exact_sha_required")
        if os.environ.get("SERVICE2_AVITO_OUTBOX_SETUP") != "1":
            raise CronError("protected_setup_required")
        for command in (
            ["git", "rev-parse", "HEAD"],
            ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"],
        ):
            result = bounded(command, timeout=20, cwd=self.app, env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fields = result.stdout.decode(errors="replace").split()
            if result.returncode or not fields or fields[0] != self.expected_sha:
                raise CronError("deployed_main_sha_mismatch")

    def status(self):
        result = bounded(["/bin/bash", str(self.wrapper), "--status"], timeout=40,
                         cwd=self.home, env=self.supervised_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self.checked_status(result)

    @staticmethod
    def checked_status(result):
        if result.returncode or len(result.stdout) > MAX_STATUS_BYTES:
            raise CronError("outbox_readiness_unavailable")
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise CronError("outbox_readiness_invalid") from None
        if (not isinstance(value, dict) or type(value.get("connection_id")) is not int
                or value["connection_id"] != 4 or value.get("account_id") != "446312402"
                or type(value.get("enabled")) is not bool
                or type(value.get("binding_valid")) is not bool):
            raise CronError("outbox_readiness_invalid")
        return value

    def readiness(self):
        value = self.status()
        if not value["binding_valid"] or not value["enabled"]:
            raise CronError("outbox_binding_disabled_or_invalid")

    def configure(self):
        # Only reads Avito's account identity and saves the initial cutoff.
        # It does not process the queue, invoke POST, or relabel old messages.
        result = bounded([
            str(self.python), str(self.app / "manage.py"), "send_avito_outbox",
            "--connection-id", "4", "--configure", "--account-id", "446312402",
        ], timeout=100, cwd=self.app, env=self.supervised_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self.checked_status(result)

    def setup(self):
        # Keep code/schema unchanged during configuration; change() later
        # rechecks release/readiness and acquires its own narrow locks.
        with self.lock(self.tmp / "service2-deploy.lock", shared=True):
            self.verify_release()
            value = self.configure()
        if not value["binding_valid"]:
            raise CronError("outbox_binding_invalid")
        if not value["enabled"]:
            return "disabled"
        self.readiness()
        if not self.crontab:
            # Beget may only expose CronTab in its authenticated panel.
            # Missing CLI is not a missing table and not an installed job.
            return "panel_setup_required"
        return self.change()

    def tick(self):
        self.private_directory()
        try:
            with self.lock(self.private / "tick.lock"), self.lock(
                    self.tmp / "service2-deploy.lock", shared=True):
                started = datetime.now(timezone.utc).isoformat()
                self.state({"started_at": started, "state": "running"})
                try:
                    result = bounded(["/bin/bash", str(self.wrapper)], timeout=145,
                                     cwd=self.home, env=self.supervised_env(),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    code = result.returncode
                except CronError:
                    code = 124
                except (OSError, ValueError, subprocess.SubprocessError):
                    code = 1
                self.state({"started_at": started,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "state": "finished", "exit_code": code})
                # The sender's own DB worker record is the authoritative
                # outgoing heartbeat. This private tick is only supervision.
                return code
        except CronError as error:
            if str(error) == "lock_busy":
                return 75
            raise


def panel_command(app_dir):
    """Only show the returned private runtime paths in authenticated settings."""
    return OutboxCronManager(app_dir).command()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "install", "remove", "tick", "setup"))
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--expected-sha")
    args = parser.parse_args(argv)
    try:
        manager = OutboxCronManager(args.app_dir, args.expected_sha)
        if args.mode == "preflight":
            manager.preflight()
            result = "preflight_passed"
        elif args.mode == "tick":
            return manager.tick()
        elif args.mode == "setup":
            result = manager.setup()
        else:
            result = manager.change(remove=args.mode == "remove")
        print("AVITO_OUTBOX_CRON status=" + result)
        return 0
    except CronError as error:
        print("AVITO_OUTBOX_CRON status=error code=" + str(error), file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("AVITO_OUTBOX_CRON status=error code=operation_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
