"""One reviewed post-deploy operation; no checkout, migration or worker replay.

The control branch MUST NOT be merged: installed HEAD and origin/main remain
BASE. Only the existing production environment/identity may run this program.
Raw child stdout/stderr stay in bounded memory and are never published.
"""
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
import types
import urllib.request

BASE = '5590e16a00134a2cf20e0fe47f90e08b0b0c2115'
CONTROL_REF = 'refs/heads/codex/service2-post-deploy-5590'
PREFIX = 'SERVICE2_POST_DEPLOY_V1 '
SITE = 'https://service2.aqualine22.ru'
OPERATIONS = {'status', 'mcp', 'avito-outbox', 'avito-services'}
ALLOW = {'.github/workflows/ci-deploy.yml', '.github/scripts/service2_post_deploy.py',
         '.github/tests/test_service2_post_deploy.py', 'docs/ai/post-deploy-5590.md'}
LIMIT = 65536
REMOTE_SECONDS = 450
PHASES = {'request', 'source', 'transport', 'layout', 'locks', 'checkout', 'main',
          'status', 'activation', 'restart', 'health', 'metadata', 'finalize',
          'outbox', 'services', 'final'}
SETUP_COMPONENTS = {'manual_photo_evidence', 'daily_statistics', 'period_statistics',
    'water_experiment_baseline', 'crm_source_links', 'improvement_projects', 'owner_review',
    'comparable_subsets', 'tariff_subscription', 'assistant_wallet', 'placement_calendar_check',
    'assistant_read_tools', 'owner_dashboard'}
CONFIRMED_STATES = {'ready', 'available', 'configured', 'preserved', 'observed', 'prepared',
    'already_available', 'read_confirmed', 'already_verified', 'verified', 'readers_checked', 'recorded'}
OP_RESULTS = {'status': {'runtime_read'}, 'avito-services': {'applied', 'already_applied'},
    'avito-outbox': {'installed', 'present', 'disabled', 'panel_setup_required'},
    'mcp': {'configured', 'already_configured', 'already_marked', 'restart_requested',
            'health_verified', 'metadata_verified', 'finalized', 'already_finalized'}}


class Failure(Exception):
    def __init__(self, category='guard'):
        self.category = category


@contextlib.contextmanager
def remote_budget(seconds=REMOTE_SECONDS):
    """The host owns cleanup even if SSH disconnects or the local runner dies."""
    def expired(*_):
        raise Failure('deadline')
    signals = (signal.SIGALRM, signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.getsignal(number) for number in signals}
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
    try:
        for number in signals:
            signal.signal(number, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for number, handler in previous.items():
            signal.signal(number, handler)


def require(condition):
    if not condition:
        raise Failure()


def capture(args, *, cwd=None, source=b'', timeout=30, env=None, limit=LIMIT):
    """Own and reap the command group; never print raw errors, including startup."""
    child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    data = {'out': bytearray(), 'err': bytearray()}
    pending, deadline = memoryview(source), time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            for stream, event, role in ((child.stdin, selectors.EVENT_WRITE, 'in'),
                    (child.stdout, selectors.EVENT_READ, 'out'), (child.stderr, selectors.EVENT_READ, 'err')):
                os.set_blocking(stream.fileno(), False)
                poll.register(stream, event, role)
            while poll.get_map():
                if time.monotonic() >= deadline:
                    raise Failure('deadline')
                for key, _ in poll.select(min(1, max(0, deadline - time.monotonic()))):
                    if key.data == 'in':
                        try:
                            if pending:
                                pending = pending[os.write(key.fd, pending[:65536]):]
                        except BrokenPipeError:
                            pending = memoryview(b'')
                        if not pending:
                            poll.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        data[key.data].extend(chunk)
                        if sum(map(len, data.values())) > limit:
                            raise Failure('output_limit')
                        if not chunk:
                            poll.unregister(key.fileobj)
        code = child.wait(timeout=max(.01, deadline - time.monotonic()))
        return code, bytes(data['out']), bytes(data['err'])
    finally:
        # Include descendants which closed their pipes and outlived the leader.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def git(root, *args):
    code, out, _ = capture(['git', *args], cwd=root, timeout=25,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}, limit=4*1024*1024)
    require(code == 0)
    return out


def current_refs(root, control):
    origin = git(root, 'config', '--get', 'remote.origin.url').decode().strip()
    require(origin in ('https://github.com/borovikov88/service2.git', 'git@github.com:borovikov88/service2.git'))
    rows = git(root, 'ls-remote', '--exit-code', 'origin', 'refs/heads/main', CONTROL_REF).decode().splitlines()
    require(set(rows) == {BASE + '\trefs/heads/main', control + '\t' + CONTROL_REF})


def clean(root, expected):
    require(git(root, 'rev-parse', 'HEAD').decode().strip() == expected)
    require(git(root, 'rev-parse', '--show-toplevel').decode().strip() == str(root))
    require(all(not row or row[:1] == b'H' for row in git(root, 'ls-files', '-v', '-z').split(b'\0')))
    require(not git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
    # Read bytes, not just index stat cache; refuse tracked symlinks/submodules.
    for row in git(root, 'ls-tree', '-rz', '--full-tree', expected).split(b'\0'):
        if not row:
            continue
        meta, name = row.split(b'\t', 1)
        mode, kind, digest = meta.split()
        require(kind == b'blob' and mode in (b'100644', b'100755'))
        path = root / os.fsdecode(name)
        regular(path)
        value = path.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(value)).encode() + b'\0' + value).hexdigest()
        require(actual == digest.decode())


def regular(path, private=False):
    info = path.lstat()
    require(path.resolve() == path and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and not info.st_mode & (0o077 if private else 0o022))


def directory(path):
    info = path.lstat()
    require(path.resolve() == path and stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid() and not info.st_mode & 0o022)


def request(env):
    control, operation = env.get('GITHUB_SHA', ''), env.get('POST_OPERATION', '')
    run = env.get('GITHUB_RUN_ID', '') + '.' + env.get('GITHUB_RUN_ATTEMPT', '')
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and env.get('GITHUB_REF') == CONTROL_REF
            and env.get('GITHUB_REPOSITORY') == 'borovikov88/service2'
            and env.get('RELEASE_MODE') == 'post-deploy'
            and re.fullmatch('[0-9a-f]{40}', control)
            and env.get('CONFIRM_CONTROL_SHA') == control != BASE
            and env.get('CONFIRM_MAIN_SHA') == BASE and operation in OPERATIONS
            and re.fullmatch(r'[1-9][0-9]{0,19}\.[1-9][0-9]{0,5}', run))
    return dict(control_sha=control, installed_sha=BASE, operation=operation, request=run)


def source_guard(root, expected):
    clean(root, expected['control_sha'])
    git(root, 'merge-base', '--is-ancestor', BASE, expected['control_sha'])
    changed = set(filter(None, git(root, 'diff', '--name-only', '-z', BASE, expected['control_sha']).decode().split('\0')))
    require(changed <= ALLOW and {'.github/workflows/ci-deploy.yml', '.github/scripts/service2_post_deploy.py'} <= changed)
    current_refs(root, expected['control_sha'])


@contextlib.contextmanager
def locks(app, write):
    """Never create the existing deploy/AI locks. Serialize addressed writers."""
    paths = [(app.parent / 'tmp' / 'service2-call-ai.lock', False),
             (app.parent / 'tmp' / 'service2-deploy.lock', False)]
    if write:
        paths.append((app.parent / 'tmp' / 'service2-post-deploy.lock', True))
    opened = []
    try:
        for path, exclusive in paths:
            fd = os.open(path, os.O_NOFOLLOW | (os.O_RDWR | os.O_CREAT if exclusive else os.O_RDONLY), 0o600)
            opened.append((path, fd))
            regular(path)
            info = os.fstat(fd)
            require((info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino))
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
        for path, fd in opened:
            require((os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (path.lstat().st_dev, path.lstat().st_ino))
    finally:
        for _, fd in reversed(opened):
            os.close(fd)


def helper(app, name):
    # Compile installed verified source; ignore stale/ignored bytecode caches.
    path = app / 'scripts' / (name + '.py')
    regular(path)
    source = path.read_bytes()
    require(source == git(app, 'show', BASE + ':scripts/' + name + '.py'))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module


def bounded_outbox(module):
    """Keep installed manager policy, replace only its child I/O supervision.

    Both imported modules have their own globals. Never monkeypatch the global
    subprocess module or the business/identity/readiness/compare-write guards.
    """
    def run(args, *, timeout=20, cwd=None, env=None, input=b'',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE):
        require(stdout == subprocess.PIPE and stderr == subprocess.PIPE)
        code, out, err = capture(args, cwd=cwd, env=env, source=input, timeout=timeout)
        return subprocess.CompletedProcess(args, code, out, err)
    facade = types.SimpleNamespace(PIPE=subprocess.PIPE, run=run)
    module.bounded = run
    module._shared.bounded = run
    module.subprocess = facade
    module._shared.subprocess = facade
    return module


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def https_json(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(SITE + path, timeout=15) as response:
        require(response.status == 200 and response.geturl() == SITE + path)
        body = response.read(LIMIT + 1)
    require(len(body) <= LIMIT)
    value = json.loads(body)
    require(isinstance(value, dict))
    return value


def health():
    for path in ('/health/live/', '/health/ready/'):
        require(https_json(path).get('status') == 'ok')


def metadata():
    row = https_json('/.well-known/oauth-protected-resource/mcp/1c')
    require(row.get('resource') == SITE + '/mcp/1c'
            and row.get('authorization_servers') == [SITE + '/onec-diagnostic']
            and isinstance(row.get('scopes_supported'), list)
            and 'onec.diagnostic.read' in row['scopes_supported'])


def restart(app):
    path = app.parent / 'tmp/restart.txt'
    if path.exists() or path.is_symlink():
        regular(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        regular(path)
        require((os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (path.stat().st_dev, path.stat().st_ino))
        os.utime(fd, None)
    finally:
        os.close(fd)


def monitor_status(app):
    code, out, _ = capture([str(app.parent / 'venv/bin/python'), '-B', '-X',
        'pycache_prefix=/dev/null/service2-post-deploy', 'manage.py', 'monitor_avito_statuses', '--status'],
        cwd=app, timeout=65)
    require(code == 0)
    pattern = rb'AVITO_STATUS_MONITOR_READY configured=(\d{1,9}) enabled=(\d{1,9}) baselined=(\d{1,9}) due=(\d{1,9}) running=(\d{1,9}) failed=(\d{1,9})\n'
    match = re.fullmatch(pattern, out)
    require(match is not None)
    return dict(zip(('configured', 'enabled', 'baselined', 'due', 'running', 'failed'), map(int, match.groups())))


def setup_evidence(value):
    """Conservative evidence classification, not a new setup/business policy.

    An attempted read, absent state, or unknown future state is not confirmation.
    Inspect nested evidence/attempt_state too; no stored state/code is emitted.
    """
    if not isinstance(value, dict) or not value:
        return 'unconfirmed'
    states, errors = [], []
    def walk(row):
        if isinstance(row, dict):
            for key, item in row.items():
                if key in {'state', 'attempt_state', 'probe_state'}:
                    # An envelope/attempt alone is not evidence. Its nested
                    # saved result may be useful; absent evidence stays partial.
                    if item not in {'already_checked', 'read_attempted', 'already_attempted', 'attempted'}:
                        states.append(item)
                elif key in {'code', 'error', 'error_code', 'last_error_code'} and item:
                    errors.append(True)
                else:
                    walk(item)
        elif isinstance(row, list):
            for item in row:
                walk(item)
    walk(value)
    if errors or any(isinstance(state, str) and state in {'blocked', 'failed', 'unavailable'} for state in states):
        return 'failed'
    if states and all(isinstance(state, str) and state in CONFIRMED_STATES for state in states):
        return 'confirmed'
    return 'unconfirmed'


def services(app, receipt):
    code, out, _ = capture([str(app.parent / 'venv/bin/python'), '-B', '-X',
        'pycache_prefix=/dev/null/service2-post-deploy', 'manage.py', 'configure_avito_services_workplace',
        '--expected-sha', BASE, '--base-url', SITE], cwd=app, timeout=120,
        env={**os.environ, 'SERVICE2_AVITO_DEPLOY_SETUP': '1'})
    require(code == 0 and out.startswith(b'AVITO_SERVICES_SETUP ') and out.count(b'\n') == 1)
    value = json.loads(out[len(b'AVITO_SERVICES_SETUP '):])
    require(value.get('setup') in ('applied', 'already_applied'))
    receipt['completed'].append(value['setup'])
    evidence = [setup_evidence(value.get(name)) for name in SETUP_COMPONENTS]
    receipt['checks'] = {
        'webhook_registered': value.get('webhook', {}).get('registration_confirmed') is True,
        'storage_verified': value.get('storage_probe', {}).get('checked') is True,
        'additional_setup_failed': 'failed' in evidence,
        'additional_setup_unconfirmed': 'unconfirmed' in evidence,
    }
    require(receipt['checks']['webhook_registered'] and receipt['checks']['storage_verified']
            and not receipt['checks']['additional_setup_failed'])
    if receipt['checks']['additional_setup_unconfirmed']:
        # Native setup includes observations with unrelated evidence contracts.
        # Preserve a useful partial outcome; do not invent their confirmation.
        raise Failure('partial')
    # Setup is not successful processing. Do not expose raw nested diagnostics.
    return value['setup']


def operate(app, expected, receipt, guard):
    operation = expected['operation']
    def step(phase, work):
        receipt['phase'] = phase
        guard()
        receipt['category'] = 'application'
        if phase in {'activation', 'restart', 'finalize', 'outbox', 'services'}:
            receipt['effects_possible'] = True
        return work()
    if operation == 'status':
        receipt['counts'] = step('status', lambda: monitor_status(app))
        receipt['completed'].append('runtime_read')
    elif operation == 'mcp':
        module = helper(app, 'enable_onec_diagnostic_mcp')
        marker = app.parent / 'tmp/onec-diagnostic-mcp-activated'
        result = step('activation', lambda: module.activate_once(app / '.env', marker))
        require(result in {'configured', 'already_configured', 'already_marked'})
        receipt['completed'].append(result)
        if result != 'already_marked':
            step('restart', lambda: restart(app))
            receipt['completed'].append('restart_requested')
        step('health', health)
        receipt['completed'].append('health_verified')
        # Even CURRENT_MARKER must pass live metadata. Never silently clear it.
        step('metadata', metadata)
        receipt['completed'].append('metadata_verified')
        result = step('finalize', lambda: module.finalize_marker(marker))
        require(result in {'finalized', 'already_finalized'})
        receipt['completed'].append(result)
    elif operation == 'avito-outbox':
        def configure():
            module = bounded_outbox(helper(app, 'avito_outbox_cron'))
            manager = module.OutboxCronManager(app, BASE)
            manager.env.update(PYTHONPYCACHEPREFIX='/dev/null/service2-post-deploy', PYTHONDONTWRITEBYTECODE='1')
            os.environ['SERVICE2_AVITO_OUTBOX_SETUP'] = '1'
            return manager.setup()
        result = step('outbox', configure)
        require(result in {'installed', 'present', 'disabled', 'panel_setup_required'})
        receipt['completed'].append(result)
    else:
        step('services', lambda: services(app, receipt))


def remote(app, expected):
    with remote_budget():
        return remote_work(app, expected)


def remote_work(app, expected):
    receipt = {**expected, 'phase': 'layout', 'category': 'guard', 'status': 'blocked',
               'completed': [], 'effects_possible': False}
    sys.dont_write_bytecode = True
    sys.pycache_prefix = '/dev/null/service2-post-deploy'
    try:
        require(app.is_absolute() and app == Path.cwd())
        for path in (app, app / '.git', app.parent / 'tmp', app.parent / 'venv'):
            directory(path)
        regular(app / '.env', private=True)
        require(os.access(app.parent / 'venv/bin/python', os.X_OK))
        receipt['phase'] = 'locks'
        with locks(app, expected['operation'] != 'status'):
            def guard():
                phase = receipt['phase']
                receipt.update(phase='checkout', category='guard')
                clean(app, BASE)
                receipt['phase'] = 'main'
                current_refs(app, expected['control_sha'])
                receipt['phase'] = phase
            guard()
            operate(app, expected, receipt, guard)
            receipt['phase'] = 'final'
            guard()
        receipt.update(status='completed', category='none', phase='final')
        code = 0
    except Exception as error:
        # The phase identifies the failure boundary, never its private exception.
        receipt['reason'] = error.category if isinstance(error, Failure) and error.category in {'deadline', 'output_limit'} else 'unavailable'
        if isinstance(error, Failure) and error.category == 'partial':
            receipt.update(status='partial', reason='private_review_required')
            code = 3
        else:
            code = 2
    print(PREFIX + json.dumps(receipt, sort_keys=True))
    return code


def normalize(code, data, expected):
    require(len(data) <= LIMIT and data.startswith(PREFIX.encode()) and data.count(b'\n') == 1 and data.endswith(b'\n'))
    value = json.loads(data[len(PREFIX):])
    require(isinstance(value, dict) and all(value.get(k) == v for k, v in expected.items()))
    keys = set(expected) | {'phase', 'category', 'status', 'completed', 'effects_possible'}
    if code in (2, 3):
        keys.add('reason')
        require(value.get('reason') in ({'private_review_required'} if code == 3 else {'deadline', 'output_limit', 'unavailable'}))
    require(set(value) in (keys, keys | {'counts'}, keys | {'checks'}) and value['phase'] in PHASES
            and isinstance(value['completed'], list) and len(value['completed']) <= 6
            and all(isinstance(item, str) and item in OP_RESULTS[expected['operation']] for item in value['completed']))
    require(type(value['effects_possible']) is bool
            and (expected['operation'] != 'status' or value['effects_possible'] is False))
    require((code == 0 and value['status'] == 'completed' and value['category'] == 'none' and value['phase'] == 'final')
            or (code == 2 and value['status'] == 'blocked' and value['category'] in ('guard', 'application'))
            or (code == 3 and expected['operation'] == 'avito-services' and value['status'] == 'partial'
                and value['category'] == 'application' and value['phase'] == 'services' and 'checks' in value))
    if code == 0:
        done, operation = value['completed'], expected['operation']
        require((operation == 'status' and done == ['runtime_read'] and 'counts' in value)
                or (operation == 'avito-outbox' and len(done) == 1)
                or (operation == 'avito-services' and len(done) == 1 and 'checks' in value)
                or (operation == 'mcp' and len(done) in (4, 5)
                    and done[-3:-1] == ['health_verified', 'metadata_verified']
                    and done[-1] in {'finalized', 'already_finalized'}))
    if 'counts' in value:
        counts = value['counts']
        require(expected['operation'] == 'status' and isinstance(counts, dict)
                and set(counts) == {'configured', 'enabled', 'baselined', 'due', 'running', 'failed'}
                and all(type(n) is int and 0 <= n < 10**9 for n in counts.values()))
    if 'checks' in value:
        checks = value['checks']
        require(expected['operation'] == 'avito-services' and isinstance(checks, dict)
                and set(checks) == {'webhook_registered', 'storage_verified', 'additional_setup_failed', 'additional_setup_unconfirmed'}
                and all(type(item) is bool for item in checks.values()))
        if code == 3:
            require(checks['webhook_registered'] and checks['storage_verified']
                    and checks['additional_setup_unconfirmed'] and not checks['additional_setup_failed'])
        if code == 0:
            require(checks['webhook_registered'] and checks['storage_verified']
                    and not checks['additional_setup_unconfirmed'] and not checks['additional_setup_failed'])
    return value


def transport(env, expected, root):
    host, user, app = (env.get(k, '') for k in ('DEPLOY_HOST', 'DEPLOY_USER', 'DEPLOY_APP_PATH'))
    port = env.get('DEPLOY_PORT') or '22'
    require(len(host) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', s) for s in host.split('.')))
    require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', user) and re.fullmatch(r'[0-9]{1,5}', port) and 1 <= int(port) <= 65535)
    require(re.fullmatch(r'/[A-Za-z0-9_./-]+/service_site', app) and '/..' not in app and '//' not in app)
    key, hosts = (Path.home() / '.ssh' / s for s in ('service2_post_deploy', 'service2_post_deploy_known_hosts'))
    for path in (key, hosts):
        regular(path, private=True)
    # Literal source only, passed over the existing pinned SSH channel; no host file installation.
    command = 'cd -- ' + shlex.quote(app) + ' && exec ../venv/bin/python -B -I - remote ' + ' '.join(map(shlex.quote,
        (app, expected['control_sha'], expected['operation'], expected['request'])))
    args = ['ssh', '-F', '/dev/null', '-i', str(key), '-p', port, '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=' + str(hosts), '-o', 'ConnectTimeout=20',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', user + '@' + host, command]
    return capture(args, source=(root / '.github/scripts/service2_post_deploy.py').read_bytes(), timeout=540)


def main(argv=None, env=None):
    args, env = sys.argv[1:] if argv is None else argv, os.environ if env is None else env
    os.umask(0o077)
    phase = 'request'
    try:
        if args[:1] == ['remote']:
            require(len(args) == 5 and re.fullmatch('[0-9a-f]{40}', args[2]) and args[3] in OPERATIONS
                    and re.fullmatch(r'[1-9][0-9]{0,19}\.[1-9][0-9]{0,5}', args[4]))
            return remote(Path(args[1]), dict(control_sha=args[2], installed_sha=BASE, operation=args[3], request=args[4]))
        require(args in (['validate'], ['run']))
        expected = request(env)
        phase = 'source'
        root = Path(__file__).resolve().parents[2]
        source_guard(root, expected)
        if args == ['validate']:
            print('SERVICE2_POST_DEPLOY_SOURCE_VERIFIED')
            return 0
        phase = 'transport'
        code, data, _ = transport(env, expected, root)
        # SSH 255 is a transport failure, not a diagnosis of network vs auth.
        # No valid remote receipt means execution may be uncertain: never retry.
        if code == 255:
            print('SERVICE2_POST_DEPLOY_BLOCKED category=transport outcome=uncertain')
            return 2
        try:
            value = normalize(code, data, expected)
        except Exception:
            print('SERVICE2_POST_DEPLOY_BLOCKED category=unclassified phase=receipt outcome=uncertain')
            return 2
        print(PREFIX + json.dumps(value, sort_keys=True))
        return code
    except Exception:
        print('SERVICE2_POST_DEPLOY_BLOCKED category=' + ('transport' if phase == 'transport' else 'guard')
              + ' phase=' + phase + (' outcome=uncertain' if phase == 'transport' else ''))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
