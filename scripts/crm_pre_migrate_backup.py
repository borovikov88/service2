#!/usr/bin/env python3
"""Fail-closed pre-migration backup under deployment's already-owned locks."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys

import crm_pilot_db_backup as backup
import crm_pilot_db_backup_receipt as receipt

RESERVE_BYTES = 64 * 1024 * 1024
CODES = frozenset(('arguments', 'lock_path', 'lock_owner', 'lock_mode', 'lock_identity',
                  'lock_not_exclusive', 'disk_space', 'receipt', 'freshness', 'file'))


class GateBlocked(Exception):
    pass


def require(condition, code):
    if not condition:
        raise GateBlocked(code)


def inherited_lock(app, fd, filename):
    """Inspect the inherited open description. Never acquire, convert or unlock."""
    temporary = app.parent / 'tmp'
    require(temporary.resolve() == temporary, 'lock_path')
    directory = temporary.stat()
    require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid()
            and not directory.st_mode & 0o022, 'lock_owner')
    path = temporary / filename
    named, opened = path.lstat(), os.fstat(fd)
    require(stat.S_ISREG(named.st_mode) and stat.S_ISREG(opened.st_mode)
            and opened.st_uid == os.getuid() and opened.st_nlink == 1, 'lock_owner')
    require(not opened.st_mode & 0o022, 'lock_mode')
    require((named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino), 'lock_identity')
    with Path(f'/proc/self/fdinfo/{fd}').open(encoding='ascii') as stream:
        info = stream.read(8193)
    require(len(info) <= 8192, 'lock_not_exclusive')
    locks = [line.split() for line in info.splitlines() if line.startswith('lock:')]
    require(len(locks) == 1, 'lock_not_exclusive')
    parts = locks[0]
    require(len(parts) == 9 and parts[2:5] == ['FLOCK', 'ADVISORY', 'WRITE']
            and parts[7:] == ['0', 'EOF'], 'lock_not_exclusive')
    major, minor, inode = parts[6].split(':')
    require((int(major, 16), int(minor, 16), int(inode)) ==
            (os.major(opened.st_dev), os.minor(opened.st_dev), opened.st_ino), 'lock_identity')


def locks(app):
    inherited_lock(app, 8, 'service2-call-ai.lock')
    inherited_lock(app, 9, 'service2-deploy.lock')


def verified_receipt(app, expected, value, started):
    value = receipt.normalize(receipt.PREFIX + json.dumps(value), expected)
    finished = dt.datetime.now(dt.timezone.utc)
    first = dt.datetime.fromisoformat(value['started_at'])
    last = dt.datetime.fromisoformat(value['completed_at'])
    require(started <= first <= last <= finished and
            (finished - started).total_seconds() <= backup.MAX_SECONDS, 'freshness')
    destination = app.parent / 'tmp' / 'service2-crm-backups'
    backup.private_dir(destination)
    archive = destination / value['backup_file']
    proof = destination / (archive.name[:-len('.sql.gz')] + '.receipt.json')
    for path in (archive, proof):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, 'file')
    require(archive.stat().st_size == value['size_bytes'], 'file')
    with proof.open(encoding='utf-8') as stream:
        text = stream.read(receipt.CAPTURE_LIMIT + 1)
    require(len(text) <= receipt.CAPTURE_LIMIT and json.loads(text) == value, 'receipt')
    # Recheck the published file, not an old filename or just a successful exit.
    require(backup.verify_gzip(archive, value['uncompressed_bytes']) == value['sha256'], 'file')
    return value


def run(app, expected):
    require(re.fullmatch(r'[0-9a-f]{40}', expected) is not None and app.resolve() == app, 'arguments')
    locks(app)
    backup.check_checkout(app, expected)
    destination = app.parent / 'tmp' / 'service2-crm-backups'
    backup.private_dir(destination, create=True)
    require(shutil.disk_usage(destination).free >= backup.MAX_GZIP_BYTES + RESERVE_BYTES, 'disk_space')
    started = dt.datetime.now(dt.timezone.utc)
    value = backup.backup(app, expected)
    value = verified_receipt(app, expected, value, started)
    locks(app)
    backup.check_checkout(app, expected)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    args = parser.parse_args()
    previous_umask = os.umask(0o077)
    def interrupted(signum, frame):
        raise backup.Blocked()
    original = {signum: signal.getsignal(signum) for signum in
                (signal.SIGALRM, signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    for signum in original:
        signal.signal(signum, interrupted)
    try:
        signal.setitimer(signal.ITIMER_REAL, backup.MAX_SECONDS)
        value = run(Path.cwd(), args.expected_sha)
        signal.setitimer(signal.ITIMER_REAL, 0)
        print(receipt.PREFIX + json.dumps(value, sort_keys=True))
        print('CRM_PRE_MIGRATE_BACKUP_VERIFIED_V1 scope=all-organizations-default-mysql')
        return 0
    except BaseException as error:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if isinstance(error, GateBlocked):
            code = str(error) if str(error) in CODES else 'arguments'
            print('CRM_PRE_MIGRATE_BACKUP_BLOCKED code=' + code, file=sys.stderr)
        else:
            print('CRM_PRE_MIGRATE_BACKUP_BLOCKED ' + backup.safe_failure(error), file=sys.stderr)
        return 2
    finally:
        os.umask(previous_umask)
        signal.setitimer(signal.ITIMER_REAL, 0)
        for signum, handler in original.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
