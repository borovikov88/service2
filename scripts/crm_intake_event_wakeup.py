"""Private same-host wake coalescing for the existing guarded intake wrapper.

No credentials, persisted source payloads, or replacement delivery implementation.
A missed wake/crash/budget exhaustion is recovered by the independent periodic job.
"""
import argparse
import fcntl
import os
from pathlib import Path
import stat
import signal
import subprocess
import time
import uuid


def _open(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("unsafe_wakeup_file")
    return fd


def _state(app):
    app = Path(app).absolute()
    if app.resolve() != app or not (app / '.git').is_dir():
        raise ValueError('not_canonical_deployment')
    parent = app.parent / 'tmp'
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError('missing_private_host_runtime')
    path = parent / 'crm-intake-wakeup'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_wakeup_directory')
    return path


def _generation(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 64)


def _lock(fd, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)


def _wrapper(args, app, seconds):
    process = subprocess.Popen(args, cwd=app, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        env={**os.environ, "CRM_INTAKE_EVENT_PROCESS_GROUP": "1"})
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return 124
    finally:
        # Even an inner timeout or early parent exit can leave descendants.
        # Always clean the owned group, not only on our outer timeout.
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=2)


def launch(app, python):
    state = _state(app)
    dispatch = _open(state / 'dispatch.lock')
    worker = marker = None
    try:
        # A short bounded wait closes the normal exit-handoff window. If the
        # producer is stalled, signal failure honestly and retain SQL recovery.
        if not _lock(dispatch, 0.1):
            raise TimeoutError('wakeup_dispatch_busy')
        marker = _open(state / 'generation')
        os.ftruncate(marker, 0)
        os.write(marker, uuid.uuid4().hex.encode('ascii'))
        worker = _open(state / 'worker.lock')
        try:
            fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return subprocess.Popen([python, str(Path(app) / 'scripts/crm_intake_event_wakeup.py'),
            '--app', str(app), '--worker-fd', str(worker)], cwd=app,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, pass_fds=(worker,))
    finally:
        for fd in (marker, worker, dispatch):
            if fd is not None:
                os.close(fd)


def run(app, worker_fd):
    state = _state(app)
    expected = (state / 'worker.lock').stat(follow_symlinks=False)
    actual = os.fstat(worker_fd)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('invalid_worker_descriptor')
    fcntl.flock(worker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    marker = _open(state / 'generation')
    dispatch = _open(state / 'dispatch.lock')
    deadline = time.monotonic() + 120
    try:
        while True:
            generation = _generation(marker)
            # The wrapper independently verifies origin/main, clean exact HEAD,
            # deployment/intake locks, activation, permissions, cutoff and leases.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=app,
                stderr=subprocess.DEVNULL, timeout=min(5, remaining), text=True).strip()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            result = _wrapper(['bash', str(Path(app) / '.github/scripts/crm_intake_worker_remote.sh'), sha, str(app)],
                app, min(90, remaining))
            if not _lock(dispatch, min(2, max(0, deadline - time.monotonic()))):
                return
            if result or time.monotonic() >= deadline or _generation(marker) == generation:
                # Release while holding dispatch: a later producer must either
                # be observed above or acquire the free lock and start a worker.
                fcntl.flock(worker_fd, fcntl.LOCK_UN)
                return
            fcntl.flock(dispatch, fcntl.LOCK_UN)
    finally:
        for fd in (marker, dispatch, worker_fd):
            os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True)
    parser.add_argument('--worker-fd', type=int, required=True)
    options = parser.parse_args()
    run(Path(options.app), options.worker_fd)
