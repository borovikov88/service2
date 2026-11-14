"""One bounded private failure snapshot; independent of wakeup state directory.

Not a queue or success heartbeat. Best effort: contention/storage failure drops a
sample, never delays for a lock and never changes launcher security policy.
"""
import datetime
import fcntl
import json
import os
from pathlib import Path
import stat

NAME = 'crm-intake-wakeup-diagnostic.json'
LIMIT = 512
CODES = frozenset({'unexpected_failure', 'not_canonical_deployment',
    'missing_private_host_runtime', 'unsafe_wakeup_directory_type',
    'unsafe_wakeup_directory_owner', 'unsafe_wakeup_directory_mode',
    'unsafe_wakeup_file_type', 'unsafe_wakeup_file_owner', 'unsafe_wakeup_file_mode',
    'wakeup_dispatch_busy', 'invalid_worker_descriptor'})
ERRNOS = frozenset({'NONE', 'OTHER', 'EACCES', 'EPERM', 'ENOSPC', 'EDQUOT',
    'EMFILE', 'ENFILE', 'ELOOP', 'EROFS', 'ENOENT'})


def _open(app, write=False):
    # Existing hosting directory; no mkdir/chmod/chown and no shared-log lookup.
    parent = Path(app).absolute().parent
    directory = os.open(parent / 'tmp', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('unsafe_diagnostic_parent')
        flags = (os.O_RDWR | os.O_CREAT) if write else os.O_RDONLY
        fd = os.open(NAME, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError('unsafe_diagnostic_file')
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def record_failure(app, metadata):
    fd = None
    try:
        code, error, equal = metadata
        sample = {'schema_version': 1,
            'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'code': code if type(code) is str and code in CODES else 'unexpected_failure',
            'errno': error if type(error) is str and error in ERRNOS else 'OTHER',
            'real_effective_uid_equal': equal if type(equal) is bool else None}
        data = (json.dumps(sample, sort_keys=True) + '\n').encode('ascii')
        if len(data) > LIMIT:
            return
        fd = _open(app, write=True)
        # A crash may leave an invalid sample; readers report unavailable, never
        # interpret a partial record. No append, archives or unbounded growth.
        os.ftruncate(fd, 0)
        os.write(fd, data)
    except Exception:
        pass
    finally:
        if fd is not None:
            os.close(fd)


def read_failure(app):
    fd = None
    try:
        fd = _open(app)
        data = os.read(fd, LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError
        sample = json.loads(data)
        if (type(sample) is not dict or set(sample) != {'schema_version', 'observed_at',
                'code', 'errno', 'real_effective_uid_equal'} or type(sample['schema_version']) is not int
                or sample['schema_version'] != 1 or type(sample['code']) is not str
                or sample['code'] not in CODES or type(sample['errno']) is not str
                or sample['errno'] not in ERRNOS or type(sample['observed_at']) is not str
                or (sample['real_effective_uid_equal'] is not None
                    and type(sample['real_effective_uid_equal']) is not bool)):
            raise ValueError
        stamp = datetime.datetime.fromisoformat(sample['observed_at'])
        if stamp.tzinfo is None or stamp.utcoffset() != datetime.timedelta(0):
            raise ValueError
        # Reformat the timestamp; do not echo raw file text.
        sample['observed_at'] = stamp.isoformat()
        return {'status': 'last_failure', 'sample': sample}
    except FileNotFoundError:
        return {'status': 'absent'}
    except Exception:
        return {'status': 'unavailable'}
    finally:
        if fd is not None:
            os.close(fd)
