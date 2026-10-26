"""Synthetic offline proof of fail-closed private DB-backup prerequisites."""
import gzip
import errno
import importlib.util
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import secrets
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

B = module('crm_backup', 'scripts/crm_pilot_db_backup.py')
R = module('crm_receipt', 'scripts/crm_pilot_db_backup_receipt.py')
SHA = 'a' * 40
DDL = 'CREATE TABLE `synthetic` (\n  `id` int NOT NULL\n) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4'
SCHEMA = {'synthetic': B.canonical_schema(DDL)}
SQL = (DDL + ';\nINSERT INTO `synthetic` VALUES (1);\n-- Dump completed on 2026-10-26 12:00:00\n').encode()


def config():
    return {'ENGINE': 'django.db.backends.mysql', 'NAME': 'synthetic', 'USER': 'test_user',
            'PASSWORD': 'private-value"\\', 'HOST': 'localhost', 'PORT': '3306',
            'OPTIONS': {'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"}}


def receipt():
    return {'kind': 'crm_pilot_db_backup', 'schema_version': 1, 'expected_sha': SHA,
            'table_count': 1, 'size_bytes': 200, 'uncompressed_bytes': 400, 'sha256': 'b' * 64,
            'schema_sha256': 'c' * 64, 'started_at': '2026-10-26T12:00:00+00:00',
            'completed_at': '2026-10-26T12:01:00+00:00', 'integrity_verified': True,
            'restore_verified': False, 'backup_file': SHA + '-20261026T120000Z-' + 'd' * 16 + '.sql.gz'}


class CoverageTests(unittest.TestCase):
    def test_streaming_schema_normalizes_only_auto_increment_and_requires_footer(self):
        check = B.DumpCoverage(SCHEMA)
        for index in range(0, len(SQL), 7):
            check.feed(SQL[index:index + 7])
        check.finish()
        self.assertEqual(set(check.seen), {'synthetic'})
        for data in (SQL.split(b'-- Dump')[0], SQL.replace(b'`id` int', b'`id` bigint'),
                     SQL + b'PRIVATE\n', SQL.replace(b'`synthetic` (', b'`other` ('),
                     SQL.replace(b'-- Dump completed', DDL.encode() + b';\n-- Dump completed')):
            with self.subTest(data=data[:30]), self.assertRaises(B.Blocked):
                check = B.DumpCoverage(SCHEMA)
                check.feed(data)
                check.finish()

    def test_raw_and_line_caps(self):
        for cap in ('MAX_RAW_BYTES', 'MAX_LINE_BYTES'):
            with patch.object(B, cap, 8), self.assertRaises(B.Blocked):
                B.DumpCoverage(SCHEMA).feed(b'x' * 9)

    def test_nontransactional_or_unsupported_schema_fails_closed(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query): self.query = query
            def fetchall(self): return [('synthetic', self.kind, self.engine)]
            def fetchone(self):
                return ('synthetic', DDL) if 'SHOW CREATE' in self.query else (self.objects,)
        cursor = Cursor()
        connection = types.SimpleNamespace(cursor=lambda: cursor)
        for kind, engine, objects in [('BASE TABLE', 'InnoDB', 0), ('VIEW', None, 0),
                                      ('BASE TABLE', 'MyISAM', 0), ('BASE TABLE', 'InnoDB', 1)]:
            cursor.kind, cursor.engine, cursor.objects = kind, engine, objects
            if kind == 'BASE TABLE' and engine == 'InnoDB' and not objects:
                self.assertEqual(B.schema_snapshot(connection), SCHEMA)
            else:
                with self.assertRaises(B.Blocked): B.schema_snapshot(connection)


class PrivateBackupTests(unittest.TestCase):
    def test_options_escape_credentials_and_reject_ambiguous_config(self):
        text = B.client_options(config())
        self.assertIn('password="private-value\\"\\\\"', text)
        for changes in ({'ENGINE': 'django.db.backends.sqlite3'}, {'NAME': '--foreign'},
                        {'PASSWORD': 'bad\noption=x'}, {'OPTIONS': {'read_default_file': '/elsewhere'}},
                        {'PORT': '0'}):
            with self.subTest(changes=changes), self.assertRaises(B.Blocked):
                B.client_options(config() | changes)

    def test_failure_diagnostics_never_include_private_exception_text(self):
        for error in (B.Blocked('PRIVATE SQL'), OSError('PRIVATE path'), ImportError('PRIVATE secret'),
                      ValueError('PRIVATE payload'), B.NativeFailure(2)):
            with patch.object(B, 'PHASE', 'dump_stream'):
                code = B.safe_failure(error)
                self.assertRegex(code, r'^phase=dump_stream error=[a-z_]+ native_exit=[0-9]+'
                                 r' reason=[a-z_]+ os_errno=[0-9]+ database_code=[0-9]+$')
                self.assertNotIn('PRIVATE', code)
        with patch.object(B, 'PHASE', 'PRIVATE injected'):
            self.assertTrue(B.safe_failure(ValueError('PRIVATE')).startswith('phase=initial '))

    def test_numeric_diagnostics_and_fixed_guard_reasons_are_bounded(self):
        database_error = type('OperationalError', (Exception,), {})
        cases = [(BlockingIOError(errno.EAGAIN, 'PRIVATE'), 'os_errno=11 database_code=0'),
                 (OSError(999999, 'PRIVATE'), 'os_errno=0 database_code=0'),
                 (database_error(1045, 'PRIVATE'), 'os_errno=0 database_code=1045'),
                 (database_error('PRIVATE', 'PRIVATE'), 'os_errno=0 database_code=0'),
                 (database_error(True, 'PRIVATE'), 'os_errno=0 database_code=0'),
                 (database_error(999999, 'PRIVATE'), 'os_errno=0 database_code=0')]
        for error, suffix in cases:
            self.assertTrue(B.safe_failure(error).endswith(suffix))
            self.assertNotIn('PRIVATE', B.safe_failure(error))
        self.assertIn('reason=lock_file_mode ', B.safe_failure(B.Blocked('lock_file_mode')))
        self.assertIn('reason=unspecified ', B.safe_failure(B.Blocked('PRIVATE')))
        self.assertEqual(R.HELPER_PHASES, B.PHASES)
        self.assertEqual(R.HELPER_REASONS, B.REASONS)

    def test_bounded_exception_chain_distinguishes_timeout_without_private_text(self):
        database_error = type('OperationalError', (Exception,), {})
        for socket_error, reason, number in ((TimeoutError('PRIVATE'), 'database_timeout', 0),
                                              (ConnectionResetError(104, 'PRIVATE'), 'unspecified', 104)):
            driver = database_error(2013, 'PRIVATE SQL')
            driver.__context__ = socket_error
            error = database_error(2013, 'PRIVATE wrapper')
            error.__cause__ = driver
            diagnostic = B.safe_failure(error)
            self.assertIn('error=database_error ', diagnostic)
            self.assertTrue(diagnostic.endswith('reason=' + reason + ' os_errno=' + str(number) + ' database_code=2013'))
            self.assertNotIn('PRIVATE', diagnostic)
            self.assertIn('source=helper', R.failure(b'', (R.HELPER_PREFIX.decode() + diagnostic).encode(), '2'))
        driver.__context__ = error  # Cycles and long chains remain bounded.
        self.assertLess(len(B.safe_failure(error)), 200)
        deep = TimeoutError('PRIVATE')
        for _ in range(8):
            wrapper = database_error(2013, 'PRIVATE'); wrapper.__cause__ = deep; deep = wrapper
        self.assertIn('reason=unspecified ', B.safe_failure(deep))

    def test_dirty_checkout_and_head_mismatch_block(self):
        for outputs in ([b'b' * 40 + b'\n'], [SHA.encode() + b'\n', b'/synthetic\n', b'dirty'],
                        [SHA.encode() + b'\n', b'/synthetic\n', b'', b'untracked.py']):
            with self.subTest(outputs=outputs), patch.object(B.subprocess, 'check_output', side_effect=outputs):
                with self.assertRaises(B.Blocked): B.check_checkout(Path('/synthetic'), SHA)

    def test_transport_matches_pymysql_and_native_version_flags(self):
        for host in ('', 'localhost', '127.0.0.1', 'db.example.invalid'):
            with self.subTest(host=host):
                text = B.client_options(config() | {'HOST': host})
                self.assertIn('protocol=TCP', text)
                self.assertNotIn('socket=', text)
        text = B.client_options(config() | {'HOST': '/synthetic/mysql.sock'})
        self.assertIn('protocol=SOCKET', text)
        self.assertIn('socket="/synthetic/mysql.sock"', text)
        for version, gtid in ((b'mysqldump  Ver 8.0.44 for Linux on x86_64 (MySQL Community Server)', True),
                              (b'mysqldump Ver 10.13 Distrib 5.7.44', True),
                              (b'mysqldump  Ver 8.0.44-0ubuntu0.24.04.2 for Linux on x86_64 ((Ubuntu))', True),
                              (b'mariadb-dump Ver 10.19 Distrib 10.11.8-MariaDB', False)):
            with patch.object(B.subprocess, 'check_output', return_value=version):
                options = B.native_options('/synthetic/mysqldump')
                self.assertIn('--no-tablespaces', options)
                self.assertEqual('--set-gtid-purged=OFF' in options, gtid)
        for help_text, supported in ((b'Oracle MySQL 8.0 options, no login flag', False),
                                     (b'Oracle MySQL 8.4 options: --no-login-paths', True)):
            with patch.object(B.subprocess, 'check_output', side_effect=[
                    b'mysqldump Ver 8.0.46 for Linux on x86_64 ((Ubuntu))', help_text]) as probe:
                options = B.native_options('/synthetic/mysqldump')
                self.assertEqual('--no-login-paths' in options, supported)
                for call in probe.call_args_list:
                    self.assertEqual(call.kwargs['env']['MYSQL_TEST_LOGIN_FILE'], '/dev/null')

    def test_private_directory_and_lock_reject_symlinks_and_contention(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'app'; app.mkdir(mode=0o700)
            (root / 'tmp').mkdir(mode=0o700)
            private = root / 'private'; B.private_dir(private, create=True)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            alias = root / 'alias'; alias.symlink_to(private)
            with self.assertRaises(B.Blocked): B.private_dir(alias)
            private.chmod(0o755)
            with self.assertRaises(B.Blocked): B.private_dir(private)
            with B.deployment_lock(app):
                with self.assertRaises(BlockingIOError):
                    with B.deployment_lock(app): pass
            lock = root / 'tmp' / 'service2-deploy.lock'
            lock.unlink(); lock.symlink_to(private / 'lock')
            with self.assertRaises(OSError):
                with B.deployment_lock(app): pass

    def test_streaming_integrity_mode_and_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'dump.partial'
            count = B.dump_to_gzip([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(' + repr(SQL) + ')'],
                                   path, SCHEMA, time.monotonic() + 10)
            self.assertEqual(count, len(SQL))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(len(B.verify_gzip(path, count)), 64)
            path.write_bytes(path.read_bytes()[:-5])
            with self.assertRaises((EOFError, OSError)): B.verify_gzip(path, count)

    def test_dump_failure_timeout_and_compressed_cap_reap_child(self):
        cases = [('import sys; sys.stdout.buffer.write(' + repr(SQL) + '); sys.exit(2)', 10, False),
                 ('import time; time.sleep(30)', 0.1, False),
                 ('import sys; sys.stdout.buffer.write(' + repr(SQL) + ')', 10, True)]
        real_popen = subprocess.Popen
        for code, seconds, cap in cases:
            with self.subTest(code=code[:20]), tempfile.TemporaryDirectory() as temp:
                children = []
                def spawn(*args, **kwargs):
                    process = real_popen(*args, **kwargs); children.append(process); return process
                with patch.object(B.subprocess, 'Popen', side_effect=spawn), patch.object(B, 'MAX_GZIP_BYTES', 1 if cap else B.MAX_GZIP_BYTES):
                    with self.assertRaises(B.Blocked):
                        B.dump_to_gzip([sys.executable, '-c', code], Path(temp) / 'dump', SCHEMA, time.monotonic() + seconds)
                self.assertIsNotNone(children[0].poll())
                if cap: self.assertLessEqual((Path(temp) / 'dump').stat().st_size, 1)

    def run_backup(self, drift=False, missing=False, primary_error=None, rollback_error=None,
                   close_error=None, unlink_failure=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app = root / 'app'; app.mkdir(mode=0o700)
            (root / 'tmp').mkdir(mode=0o700)
            from unittest.mock import MagicMock
            connection = MagicMock(); connection.settings_dict = config()
            connection.rollback.side_effect = rollback_error
            connection.close.side_effect = close_error
            fake = types.ModuleType('django.db'); fake.connections = {'default': connection}
            captured = []
            def dump(command, path, expected, deadline):
                captured.append(command)
                options_path = Path(command[1].split('=', 1)[1])
                self.assertEqual(stat.S_IMODE(options_path.stat().st_mode), 0o600)
                self.assertIn('password=', options_path.read_text())
                self.assertNotIn(config()['PASSWORD'], json.dumps(command))
                self.assertIn('--single-transaction', command)
                self.assertIn('--skip-lock-tables', command)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as output: output.write(gzip.compress(SQL))
                return len(SQL)
            snapshots = [SCHEMA, SCHEMA, primary_error if primary_error is not None else ({} if drift else SCHEMA)]
            real_unlink = Path.unlink
            unlinked = []
            def unlink(path, *args, **kwargs):
                unlinked.append(path)
                if unlink_failure and len(unlinked) == 1:
                    raise OSError(13, 'PRIVATE cleanup path')
                return real_unlink(path, *args, **kwargs)
            with patch.object(B, 'check_checkout'), patch.object(B, 'native_options', return_value=['--no-tablespaces']), patch.object(B.shutil, 'which', return_value=None if missing else '/usr/bin/mysqldump'), patch.dict(sys.modules, {'django.db': fake}), patch.object(B, 'schema_snapshot', side_effect=snapshots), patch.object(B, 'dump_to_gzip', side_effect=dump), patch.object(Path, 'unlink', unlink):
                if primary_error is not None or rollback_error is not None or close_error is not None:
                    expected_error = primary_error if primary_error is not None else (
                        rollback_error if rollback_error is not None else close_error)
                    with self.assertRaises(type(expected_error)) as caught:
                        B.backup(app, SHA)
                    self.assertIs(caught.exception, expected_error)
                    connection.rollback.assert_called_once()
                    connection.close.assert_called_once()
                    destination = root / 'tmp' / 'service2-crm-backups'
                    if primary_error is not None:
                        self.assertEqual(B.PHASE, 'schema_after')
                        expected_paths = {'.sql.gz.partial', '.sql.gz', '.receipt.json.partial', '.receipt.json'}
                        self.assertEqual({suffix for suffix in expected_paths
                                          if any(str(path).endswith(suffix) for path in unlinked)}, expected_paths)
                        if not unlink_failure:
                            self.assertEqual(list(destination.iterdir()), [])
                    else:
                        # A previously published private archive/receipt is retained.
                        self.assertEqual(len(list(destination.iterdir())), 2)
                    return
                if missing or drift:
                    with self.assertRaises(B.Blocked): B.backup(app, SHA)
                    if not missing: self.assertEqual(list((root / 'tmp' / 'service2-crm-backups').iterdir()), [])
                else:
                    result = B.backup(app, SHA)
                    self.assertTrue(result['integrity_verified']); self.assertFalse(result['restore_verified'])
                    self.assertEqual(R.normalize(R.PREFIX + json.dumps(result), SHA), result)
                    destination = root / 'tmp' / 'service2-crm-backups'
                    self.assertEqual(len(list(destination.iterdir())), 2)
                    for item in destination.iterdir(): self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)
                    self.assertEqual(connection.set_autocommit.call_args.args, (False,))
                    connection.rollback.assert_called_once()
                    self.assertFalse(Path(captured[0][1].split('=', 1)[1]).exists())

    def test_complete_backup_and_private_receipt(self): self.run_backup()
    def test_schema_drift_removes_partial_and_receipt(self): self.run_backup(drift=True)
    def test_missing_native_dump_fails_before_settings(self): self.run_backup(missing=True)

    def test_primary_failure_survives_rollback_close_and_unlink_errors(self):
        database_error = type('OperationalError', (Exception,), {})
        primary = database_error(2013, 'PRIVATE original query')
        primary.__context__ = TimeoutError('PRIVATE socket')
        for unlink_failure in (False, True):
            self.run_backup(primary_error=primary,
                            rollback_error=type('InterfaceError', (Exception,), {})(0, 'PRIVATE rollback'),
                            close_error=OSError(13, 'PRIVATE close'), unlink_failure=unlink_failure)
            self.assertTrue(B.safe_failure(primary).endswith('reason=database_timeout os_errno=0 database_code=2013'))

    def test_success_cleanup_failure_still_fails_and_retains_published_files(self):
        for rollback_error, close_error in ((OSError(13, 'PRIVATE rollback'), OSError(13, 'PRIVATE close')),
                                            (None, OSError(13, 'PRIVATE close'))):
            try:
                raise ValueError('unrelated caller exception')
            except ValueError:
                self.run_backup(rollback_error=rollback_error, close_error=close_error)



class ReceiptWorkflowTests(unittest.TestCase):
    def helper_failure(self, error=None):
        with patch.object(B, 'PHASE', 'lock'):
            return (R.HELPER_PREFIX.decode() + B.safe_failure(error or BlockingIOError(errno.EAGAIN, 'PRIVATE'))).encode()

    def test_failure_relay_reconstructs_only_allowlisted_tokens(self):
        line = self.helper_failure()
        text = R.failure(b'PRIVATE stdout', b'PRIVATE error\n' + line + b'\n', '2')
        self.assertEqual(text, 'CRM_PILOT_DB_BACKUP_FAILURE_V1 source=helper ssh_exit=2 phase=lock '
                         'error=os_error native_exit=0 reason=unspecified os_errno=11 database_code=0')
        for phase, errors in R.REMOTE_ERRORS.items():
            for error in errors:
                raw = ('CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED phase=' + phase + ' error=' + error).encode()
                self.assertEqual(R.failure(b'', raw, '2'),
                                 'CRM_PILOT_DB_BACKUP_FAILURE_V1 source=remote ssh_exit=2 phase=' + phase + ' error=' + error)
        self.assertEqual(R.failure(b'', b'PRIVATE SSH ERROR', '255'),
                         'CRM_PILOT_DB_BACKUP_TRANSPORT_BLOCKED ssh_exit=255 diagnostic=absent')

    def test_hostile_ambiguous_malformed_and_oversized_failure_output_stays_closed(self):
        line = self.helper_failure()
        candidates = [(b'', line + b'\n' + line, '2'), (line, b'', '2'),
                      (b'', line + b' PRIVATE', '2'), (b'', line.replace(b'phase=lock', b'phase=PRIVATE'), '2'),
                      (b'', line.replace(b'error=os_error', b'error=PRIVATE'), '2'),
                      (b'', line.replace(b'reason=unspecified', b'reason=PRIVATE'), '2'),
                      (b'', line.replace(b'native_exit=0', b'native_exit=256'), '2'),
                      (b'', line.replace(b'native_exit=0', b'native_exit=-128'), '2'),
                      (b'', line.replace(b'os_errno=11', b'os_errno=134'), '2'),
                      (b'', line.replace(b'os_errno=11', b'os_errno=011'), '2'),
                      (b'', line.replace(b'database_code=0', b'database_code=3000'), '2'),
                      (b'', b'\x1b[31m' + line, '2'), (b'', b'\xff PRIVATE', '2'),
                      (b'', line + b'\x00PRIVATE', '2'), (b'x' * 16385, line, '2'),
                      (b'', line + b'\n' + b'x' * 16385, '2'),
                      ((R.PREFIX + json.dumps(receipt())).encode(), line, '2'),
                      (b'', line, '0'), (b'', line, '-1'), (b'', line, '256'), (b'', line, '02'),
                      (b'', line, 'PRIVATE'),
                      (b'', b'CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED phase=main error=invalid_sha', '2')]
        for stdout, stderr, code in candidates:
            with self.subTest(code=code, stderr=stderr[:50]):
                safe = R.failure(stdout, stderr, code)
                self.assertRegex(safe, r'^CRM_PILOT_DB_BACKUP_TRANSPORT_BLOCKED ssh_exit=[0-9]{1,3} diagnostic=[a-z_]+$')
                self.assertNotIn('PRIVATE', safe)
                self.assertLess(len(safe), 140)

    def test_failure_cli_always_fails_and_never_prints_raw_captures(self):
        with tempfile.TemporaryDirectory() as temp:
            stdout, stderr = Path(temp) / 'stdout', Path(temp) / 'stderr'
            stdout.write_bytes(b'PRIVATE')
            stderr.write_bytes(self.helper_failure())
            for args in ([str(stdout), str(stderr), '2'], [str(stdout), str(stderr), '0'],
                         [str(stdout), str(stderr), '2', 'PRIVATE'], [str(stdout), str(stderr) + '-missing', '2']):
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/crm_pilot_db_backup_receipt.py'),
                                         '--failure', *args], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, '')
                self.assertNotIn('PRIVATE', result.stdout)
                self.assertLess(len(result.stdout), 300)
                self.assertNotIn(R.PREFIX, result.stdout)

    def test_remote_guards_suppress_raw_errors_and_preserve_helper_result(self):
        remote = ROOT / '.github/scripts/crm_pilot_db_backup_remote.sh'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app = root / 'app'; app.mkdir(); (app / '.git').mkdir()
            (root / 'tmp').mkdir(); (root / 'venv' / 'bin').mkdir(parents=True)
            (root / 'venv' / 'bin' / 'python').symlink_to(sys.executable)
            (app / 'scripts').mkdir()
            helper = app / 'scripts/crm_pilot_db_backup.py'
            helper.write_text('import sys\nprint(' + repr(self.helper_failure().decode()) + ', file=sys.stderr)\nsys.exit(2)\n')
            bin_dir = root / 'bin'; bin_dir.mkdir()
            git = bin_dir / 'git'
            git.write_text('#!/usr/bin/env bash\n'
                           'if [[ "$*" == "$FAIL_GIT" ]]; then echo "PRIVATE git failure" >&2; exit 1; fi\n'
                           'case "$*" in\n'
                           '"rev-parse --show-toplevel") pwd -P ;;\n'
                           '"rev-parse HEAD") printf "%s\\n" "$TEST_SHA" ;;\n'
                           '"config --get remote.origin.url") echo git@github.com:borovikov88/service2.git ;;\n'
                           '"ls-remote --exit-code origin refs/heads/main") printf "%s refs/heads/main\\n" "$TEST_SHA" ;;\n'
                           'esac\n')
            git.chmod(0o700)
            cases = [([], '', 'arguments', 'invalid_count'),
                     (['PRIVATE', str(app)], '', 'arguments', 'invalid_sha'),
                     ([SHA, 'PRIVATE'], '', 'arguments', 'invalid_path'),
                     ([SHA, str(root / 'missing')], '', 'path', 'not_directory'),
                     ([SHA, str(app)], 'rev-parse --show-toplevel', 'git', 'toplevel_failed'),
                     ([SHA, str(app)], 'rev-parse HEAD', 'head', 'read_failed'),
                     ([SHA, str(app)], 'config --get remote.origin.url', 'origin', 'read_failed'),
                     ([SHA, str(app)], 'ls-remote --exit-code origin refs/heads/main', 'main', 'lookup_failed')]
            for args, fail_git, phase, error in cases:
                result = subprocess.run(['bash', str(remote), *args], capture_output=True, text=True,
                                        env={**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                                             'FAIL_GIT': fail_git, 'TEST_SHA': SHA})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, 'CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED phase=' + phase + ' error=' + error + '\n')
            env = {**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'FAIL_GIT': '', 'TEST_SHA': SHA}
            result = subprocess.run(['bash', str(remote), SHA, str(app)], capture_output=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, self.helper_failure() + b'\n')
            self.assertIn('source=helper', R.failure(result.stdout, result.stderr, str(result.returncode)))
            helper.write_text('print("SYNTHETIC success")\n')
            result = subprocess.run(['bash', str(remote), SHA, str(app)], capture_output=True, env=env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'SYNTHETIC success\n')
            self.assertEqual(result.stderr, b'')

    def test_receipt_allowlist_cannot_claim_restore_or_leak_extra_fields(self):
        value = receipt()
        self.assertEqual(R.normalize(R.PREFIX + json.dumps(value), SHA), value)
        for changes in ({'restore_verified': True}, {'expected_sha': 'b' * 40}, {'table_count': True},
                        {'private': 'SECRET'}, {'backup_file': '../secret'}, {'sha256': 'SECRET'},
                        {'completed_at': '2026-10-26T12:20:00+00:00'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                R.normalize(R.PREFIX + json.dumps(value | changes), SHA)
        with self.assertRaises(ValueError): R.normalize(R.PREFIX + json.dumps(value) + '\n' + R.PREFIX + json.dumps(value), SHA)

    def test_workflow_exact_opt_in_and_existing_guards(self):
        workflow = (ROOT / '.github/workflows/ci-deploy.yml').read_text()
        step = workflow.split('      - name: Create verified private CRM pilot database backup\n')[1].split('\n  calculator:')[0]
        self.assertIn("github.event_name == 'push' && github.ref == 'refs/heads/main'", step)
        self.assertIn("contains(github.event.head_commit.message, '[crm-pilot-db-backup-20261026]')", step)
        for guard in ('StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'PasswordAuthentication=no', '"$GITHUB_SHA"', '2>"$backup_temp/stderr"'):
            self.assertIn(guard, step)
        self.assertIn('scripts/crm_pilot_db_backup_receipt.py --failure', step)
        self.assertIn('"$backup_temp/stdout" "$backup_temp/stderr" "$backup_exit" || exit 2', step)
        self.assertLess(workflow.index('Verify deployed site health'), workflow.index('Create verified private CRM pilot database backup'))
        self.assertIn('python -m unittest discover -s .github/tests -p test_crm_pilot_db_backup.py -v', workflow)
        remote = (ROOT / '.github/scripts/crm_pilot_db_backup_remote.sh').read_text()
        self.assertIn('timeout 20 git ls-remote --exit-code origin refs/heads/main', remote)
        self.assertIn('git diff --quiet HEAD --', remote)
        subprocess.run(['bash', '-n', str(ROOT / '.github/scripts/crm_pilot_db_backup_remote.sh')], check=True)



@unittest.skipUnless(os.environ.get('SERVICE2_CRM_BACKUP_MYSQL_INTEGRATION') == '1',
                     'requires isolated CI MySQL service, never production')
class NativeMySQLTests(unittest.TestCase):
    def test_pinned_driver_lost_socket_error_is_preserved_through_cleanup(self):
        try:
            import pymysql
            from django.db.utils import DatabaseErrorWrapper, OperationalError
        except ImportError:
            self.skipTest('requires installed pinned Django/PyMySQL, no database access')
        from unittest.mock import MagicMock
        raw = pymysql.connections.Connection(defer_connect=True)
        raw._sock = MagicMock()
        raw._rfile = MagicMock()
        raw._rfile.read.side_effect = TimeoutError('PRIVATE socket timeout')
        wrapper = types.SimpleNamespace(Database=pymysql, errors_occurred=False)
        try:
            with DatabaseErrorWrapper(wrapper):
                raw._read_bytes(1)
        except OperationalError as primary:
            self.assertEqual(primary.args[0], 2013)
            self.assertIsNone(raw._sock)
            try:
                with DatabaseErrorWrapper(wrapper):
                    raw.rollback()
            except Exception as secondary:
                self.assertEqual(type(secondary).__name__, 'InterfaceError')
                self.assertEqual(secondary.args[0], 0)
                PrivateBackupTests.run_backup(self, primary_error=primary, rollback_error=secondary)
                self.assertTrue(B.safe_failure(primary).endswith('reason=database_timeout os_errno=0 database_code=2013'))
            else:
                self.fail('lost socket must fail rollback')
        else:
            self.fail('synthetic socket timeout must produce original driver failure')
        finally:
            raw.close()

    def test_restricted_native_backup_and_disposable_restore(self):
        import pymysql
        self.assertEqual(os.environ.get('DB_NAME'), 'service2_test')
        host = os.environ['DB_HOST']; port = int(os.environ['DB_PORT'])
        admin = pymysql.connect(host=host, port=port, user=os.environ['DB_USER'],
                                password=os.environ['DB_PASSWORD'], autocommit=True,
                                charset='utf8mb4', connect_timeout=10)
        token = secrets.token_hex(5)
        database, restored, user = 'crm_backup_' + token, 'crm_restore_' + token, 'crm_reader_' + token
        password = secrets.token_hex(16)
        try:
            with admin.cursor() as cursor:
                cursor.execute('CREATE DATABASE `' + database + '` CHARACTER SET utf8mb4')
                cursor.execute('CREATE DATABASE `' + restored + '` CHARACTER SET utf8mb4')
                cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s', (user, '%', password))
                cursor.execute('GRANT SELECT, SHOW VIEW ON `' + database + '`.* TO %s@%s', (user, '%'))
                cursor.execute('CREATE TABLE `' + database + '`.`synthetic` (id int PRIMARY KEY AUTO_INCREMENT, '
                               'label varchar(100), payload blob) ENGINE=InnoDB')
                cursor.execute('INSERT INTO `' + database + '`.`synthetic` (label, payload) VALUES (%s, %s)',
                               ('Тест CRM Ω', b'\x00\xff\n\r\x1a'))
            with tempfile.TemporaryDirectory(prefix='crm-native-ci-') as temp:
                root = Path(temp); app = root / 'app'; app.mkdir(mode=0o700)
                (root / 'tmp').mkdir(mode=0o700)
                settings = app / 'service_site'; settings.mkdir(mode=0o700)
                (settings / '__init__.py').write_text('import pymysql\npymysql.install_as_MySQLdb()\n')
                synthetic_config = config() | {'NAME': database, 'USER': user, 'PASSWORD': password,
                                               'HOST': host, 'PORT': str(port)}
                (settings / 'settings.py').write_text('SECRET_KEY="synthetic-ci"\nDATABASES=' +
                                                     repr({'default': synthetic_config}) + '\n')
                (app / 'manage.py').write_text('# synthetic fixture\n')
                subprocess.run(['git', 'init', '-q', str(app)], check=True)
                subprocess.run(['git', '-C', str(app), 'add', 'service_site', 'manage.py'], check=True)
                subprocess.run(['git', '-C', str(app), '-c', 'user.name=Synthetic CI', '-c',
                                'user.email=synthetic@example.invalid', 'commit', '-qm', 'Synthetic fixture'], check=True)
                sha = subprocess.check_output(['git', '-C', str(app), 'rev-parse', 'HEAD']).decode().strip()
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/crm_pilot_db_backup.py'),
                                         '--expected-sha', sha], cwd=app, capture_output=True, text=True, timeout=320,
                                         env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                # Probe only local client help, never connection settings or server data.
                native_dump = B.shutil.which('mysqldump') or B.shutil.which('mariadb-dump')
                probe = subprocess.run([native_dump, '--no-defaults', '--help'], capture_output=True,
                                       timeout=10, env={'PATH': '/usr/bin:/bin', 'MYSQL_TEST_LOGIN_FILE': '/dev/null'})
                supports_no_login = b'no-login-paths' in probe.stdout
                # Use the production bounded allowlist, never raw stderr/help/settings/SQL.
                code = (R.failure(result.stdout.encode(), result.stderr.encode(), str(result.returncode))
                        if result.returncode else 'no_failure')
                self.assertEqual(result.returncode, 0, 'native backup failed; ' + code +
                                 '; supports_no_login_paths=' + str(supports_no_login).lower())
                proof = R.normalize(result.stdout, sha)
                self.assertTrue(proof['integrity_verified']); self.assertFalse(proof['restore_verified'])
                archive = root / 'tmp' / 'service2-crm-backups' / proof['backup_file']
                sql = gzip.decompress(archive.read_bytes())
                self.assertIn('Тест CRM Ω'.encode(), sql)
                self.assertIn(b'0x00FF0A0D1A', sql)
                self.assertEqual(len(sql), proof['uncompressed_bytes'])
                # Actual restore uses only the disposable CI database and synthetic admin.
                native = os.path.join(os.path.dirname(B.shutil.which('mysqldump') or B.shutil.which('mariadb-dump')), 'mysql')
                if not Path(native).is_file(): native = B.shutil.which('mysql') or B.shutil.which('mariadb')
                self.assertIsNotNone(native, 'native restore client required for CI fixture')
                options = root / 'restore.cnf'
                admin_config = config() | {'HOST': host, 'PORT': str(port), 'USER': os.environ['DB_USER'],
                                           'PASSWORD': os.environ['DB_PASSWORD']}
                options.write_text(B.client_options(admin_config)); options.chmod(0o600)
                restore = subprocess.run([native, '--defaults-file=' + str(options), restored], input=sql,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                                         env={'PATH': '/usr/bin:/bin', 'MYSQL_TEST_LOGIN_FILE': '/dev/null'})
                self.assertEqual(restore.returncode, 0, 'synthetic restore failed; raw output suppressed')
                with admin.cursor() as cursor:
                    cursor.execute('SELECT id, label, payload FROM `' + restored + '`.`synthetic`')
                    self.assertEqual(cursor.fetchall(), ((1, 'Тест CRM Ω', b'\x00\xff\n\r\x1a'),))
        finally:
            with admin.cursor() as cursor:
                cursor.execute('DROP USER IF EXISTS %s@%s', (user, '%'))
                cursor.execute('DROP DATABASE IF EXISTS `' + database + '`')
                cursor.execute('DROP DATABASE IF EXISTS `' + restored + '`')
            admin.close()


if __name__ == '__main__': unittest.main()
