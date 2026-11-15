"""Offline synthetic guards, protocol, locks, failure and exact command traces."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('post_deploy', ROOT / '.github/scripts/service2_post_deploy.py')
post = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(post)


def env():
    return {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': post.CONTROL_REF,
        'GITHUB_REPOSITORY': 'borovikov88/service2', 'GITHUB_SHA': 'a'*40,
        'CONFIRM_CONTROL_SHA': 'a'*40, 'CONFIRM_MAIN_SHA': post.BASE,
        'RELEASE_MODE': 'post-deploy', 'POST_OPERATION': 'status',
        'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}


def receipt(operation='status', **changes):
    value = {**post.request({**env(), 'POST_OPERATION': operation}),
        'phase': 'final', 'status': 'completed', 'category': 'none', 'completed': ['runtime_read'], 'effects_possible': False}
    value.update(changes)
    return value


class RequestTests(unittest.TestCase):
    def test_exact_manual_branch_and_confirmation(self):
        self.assertEqual(post.request(env())['installed_sha'], post.BASE)
        changes = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_REPOSITORY': 'foreign/service2', 'GITHUB_SHA': 'b'*40,
            'CONFIRM_CONTROL_SHA': 'b'*40, 'CONFIRM_MAIN_SHA': 'b'*40,
            'RELEASE_MODE': 'release', 'POST_OPERATION': 'replay', 'GITHUB_RUN_ATTEMPT': '0'}
        for key, bad in changes.items():
            with self.subTest(key=key), self.assertRaises(post.Failure):
                post.request({**env(), key: bad})

    def test_current_refs_require_both_exact_shas_and_repository(self):
        control = 'a'*40
        good = f'{post.BASE}\trefs/heads/main\n{control}\t{post.CONTROL_REF}\n'.encode()
        with patch.object(post, 'git', side_effect=[b'https://github.com/borovikov88/service2.git\n', good]):
            post.current_refs(ROOT, control)
        for bad in (good.replace(post.BASE.encode(), b'b'*40), good.replace(control.encode(), b'b'*40), b''):
            with self.subTest(bad=bad[:4]):
                with patch.object(post, 'git', side_effect=[b'https://github.com/borovikov88/service2.git\n', bad]), self.assertRaises(post.Failure):
                    post.current_refs(ROOT, control)
        with patch.object(post, 'git', return_value=b'https://example.invalid/foreign.git\n'), self.assertRaises(post.Failure):
            post.current_refs(ROOT, control)

    def test_public_output_excludes_private_exceptions(self):
        for args in ([], ['run'], ['remote', 'PRIVATE']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(post, 'source_guard', side_effect=RuntimeError('PRIVATE TOKEN')):
                self.assertEqual(post.main(args, env()), 2)
            self.assertNotIn('PRIVATE', out.getvalue())

    def test_transport_failure_is_not_falsely_diagnosed_as_timeout(self):
        for code, data in ((255, b'PRIVATE'), (1, b'PRIVATE'), (0, b'PRIVATE')):
            out = io.StringIO()
            with patch.object(post, 'source_guard'), patch.object(post, 'transport', return_value=(code, data, b'PRIVATE ERROR')), contextlib.redirect_stdout(out):
                self.assertEqual(post.main(['run'], env()), 2)
            self.assertIn('outcome=uncertain', out.getvalue())
            self.assertNotIn('timeout', out.getvalue())
            self.assertNotIn('PRIVATE', out.getvalue())


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / 'service_site'
        self.app.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'synthetic@example.invalid')
        self.git('config', 'user.name', 'Synthetic tester')
        (self.app / 'code.py').write_text('synthetic = 1\n')
        (self.app / '.gitignore').write_text('.env\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        (self.app / '.env').write_text('SYNTHETIC_SECRET=never-output\n')
        (self.app / '.env').chmod(0o600)
        for name in ('tmp', 'venv'):
            (self.app.parent / name).mkdir()
        for name in ('service2-call-ai.lock', 'service2-deploy.lock'):
            (self.app.parent / 'tmp' / name).touch(mode=0o600)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.app, stderr=subprocess.DEVNULL).decode()

    def test_clean_exact_bytes(self):
        post.clean(self.app, self.base)
        with self.assertRaises(post.Failure):
            post.clean(self.app, 'a'*40)
        original = (self.app / 'code.py').stat()
        (self.app / 'code.py').write_text('synthetic = 2\n')
        os.utime(self.app / 'code.py', ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(post.Failure):
            post.clean(self.app, self.base)

    def test_dirty_and_untracked_are_not_hidden(self):
        for name in ('code.py', 'new.py'):
            with self.subTest(name=name):
                target = self.app / name
                before = target.read_bytes() if target.exists() else None
                target.write_text('private synthetic change\n')
                with self.assertRaises(post.Failure):
                    post.clean(self.app, self.base)
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)

    def test_index_flags_are_rejected(self):
        for option, undo in (('--assume-unchanged', '--no-assume-unchanged'), ('--skip-worktree', '--no-skip-worktree')):
            self.git('update-index', option, 'code.py')
            with self.assertRaises(post.Failure):
                post.clean(self.app, self.base)
            self.git('update-index', undo, 'code.py')

    def test_symlink_and_hardlink_source_rejected(self):
        target = self.app / 'code.py'
        saved = self.app.parent / 'saved.py'
        target.rename(saved)
        for make in (lambda: target.symlink_to(saved), lambda: os.link(saved, target)):
            make()
            with self.assertRaises(post.Failure):
                post.clean(self.app, self.base)
            target.unlink()

    def test_source_guard_rejects_mixed_application_diff(self):
        paths = ['.github/scripts/service2_post_deploy.py', '.github/workflows/ci-deploy.yml']
        for name in paths:
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'control only')
        expected = {'control_sha': self.git('rev-parse', 'HEAD').strip()}
        with patch.object(post, 'BASE', self.base), patch.object(post, 'current_refs') as refs:
            post.source_guard(self.app, expected)
            refs.assert_called_once()
            (self.app / 'code.py').write_text('synthetic = 2\n')
            self.git('commit', '-qam', 'mixed change')
            expected['control_sha'] = self.git('rev-parse', 'HEAD').strip()
            with self.assertRaises(post.Failure):
                post.source_guard(self.app, expected)

    def test_read_locks_do_not_create_files_or_change_bytes(self):
        before = {p.name: p.read_bytes() for p in (self.app.parent / 'tmp').iterdir()}
        with post.locks(self.app, False):
            pass
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.app.parent / 'tmp').iterdir()})
        (self.app.parent / 'tmp/service2-call-ai.lock').unlink()
        with self.assertRaises(FileNotFoundError), post.locks(self.app, False):
            pass
        self.assertFalse((self.app.parent / 'tmp/service2-call-ai.lock').exists())

    def test_existing_exclusive_deploy_blocks_all_operations(self):
        import fcntl
        with (self.app.parent / 'tmp/service2-deploy.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError), post.locks(self.app, True):
                self.fail('entered during deploy')

    def test_concurrent_addressed_writer_is_blocked(self):
        with post.locks(self.app, True):
            with self.assertRaises(BlockingIOError), post.locks(self.app, True):
                self.fail('second writer entered')

    def test_lock_replacement_is_detected(self):
        with self.assertRaises(post.Failure), post.locks(self.app, False):
            path = self.app.parent / 'tmp/service2-deploy.lock'
            path.unlink()
            path.touch()

    def test_symlink_lock_rejected(self):
        path = self.app.parent / 'tmp/service2-deploy.lock'
        path.unlink()
        path.symlink_to(self.app / '.env')
        with self.assertRaises(OSError), post.locks(self.app, True):
            pass

    def test_restart_preserves_bytes_and_rejects_symlink(self):
        path = self.app.parent / 'tmp/restart.txt'
        path.write_text('preserve synthetic bytes')
        post.restart(self.app)
        self.assertEqual(path.read_text(), 'preserve synthetic bytes')
        path.unlink()
        path.symlink_to(self.app / '.env')
        with self.assertRaises(post.Failure):
            post.restart(self.app)

    def test_actual_activation_helper_keeps_unrelated_bytes_and_pending_on_failure(self):
        script = self.app / 'scripts/enable_onec_diagnostic_mcp.py'
        script.parent.mkdir()
        script.write_bytes((ROOT / 'scripts/enable_onec_diagnostic_mcp.py').read_bytes())
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic helper fixture')
        base = self.git('rev-parse', 'HEAD').strip()
        original = (self.app / '.env').read_bytes()
        inode = (self.app / '.env').stat().st_ino
        expected = post.request({**env(), 'POST_OPERATION': 'mcp'})
        value = receipt('mcp', completed=[])
        with patch.object(post, 'BASE', base), patch.object(post, 'health'), patch.object(post, 'metadata', side_effect=post.Failure()):
            with self.assertRaises(post.Failure):
                post.operate(self.app, expected, value, Mock())
        self.assertTrue((self.app / '.env').read_bytes().startswith(original))
        self.assertEqual((self.app / '.env').stat().st_ino, inode)
        self.assertEqual((self.app.parent / 'tmp/onec-diagnostic-mcp-activated').read_bytes(), b'activated-v2-pending\n')
        self.assertEqual(value['completed'], ['configured', 'restart_requested', 'health_verified'])
        self.assertTrue(value['effects_possible'])

    def test_actual_remote_guard_failure_precedes_application_and_reveals_no_paths(self):
        (self.app.parent / 'venv/bin').mkdir()
        (self.app.parent / 'venv/bin/python').symlink_to(sys.executable)
        current, out = Path.cwd(), io.StringIO()
        try:
            os.chdir(self.app)
            with patch.object(post, 'BASE', self.base), patch.object(post, 'current_refs', side_effect=post.Failure()), patch.object(post, 'operate') as operation, contextlib.redirect_stdout(out):
                self.assertEqual(post.remote(self.app, post.request(env())), 2)
                operation.assert_not_called()
        finally:
            os.chdir(current)
        value = json.loads(out.getvalue()[len(post.PREFIX):])
        self.assertEqual((value['category'], value['phase'], value['effects_possible']), ('guard', 'main', False))
        self.assertNotIn(str(self.app), out.getvalue())
        self.assertNotIn('SYNTHETIC_SECRET', out.getvalue())

    def test_actual_remote_status_preserves_files_and_has_valid_receipt(self):
        (self.app.parent / 'venv/bin').mkdir()
        (self.app.parent / 'venv/bin/python').symlink_to(sys.executable)
        current, out, expected = Path.cwd(), io.StringIO(), post.request(env())
        counts = dict.fromkeys(('configured', 'enabled', 'baselined', 'due', 'running', 'failed'), 0)
        before = {p.relative_to(self.app.parent): p.read_bytes() for p in self.app.parent.rglob('*') if p.is_file()}
        try:
            os.chdir(self.app)
            with patch.object(post, 'BASE', self.base), patch.object(post, 'current_refs'), patch.object(post, 'monitor_status', return_value=counts), contextlib.redirect_stdout(out):
                self.assertEqual(post.remote(self.app, expected), 0)
        finally:
            os.chdir(current)
        self.assertEqual(post.normalize(0, out.getvalue().encode(), expected)['counts'], counts)
        self.assertEqual(before, {p.relative_to(self.app.parent): p.read_bytes() for p in self.app.parent.rglob('*') if p.is_file()})

    def test_host_deadline_releases_locks_while_waiting_for_env(self):
        import fcntl
        script = self.app / 'scripts/enable_onec_diagnostic_mcp.py'
        script.parent.mkdir()
        script.write_bytes((ROOT / 'scripts/enable_onec_diagnostic_mcp.py').read_bytes())
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic helper fixture')
        base = self.git('rev-parse', 'HEAD').strip()
        before = (self.app / '.env').read_bytes()
        with (self.app / '.env').open('r') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(post, 'BASE', base), self.assertRaises(post.Failure), post.remote_budget(.05), post.locks(self.app, True):
                post.helper(self.app, 'enable_onec_diagnostic_mcp').activate_once(self.app / '.env')
        self.assertEqual((self.app / '.env').read_bytes(), before)
        for name in ('service2-call-ai.lock', 'service2-deploy.lock', 'service2-post-deploy.lock'):
            with (self.app.parent / 'tmp' / name).open('r') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_outbox_nested_capture_bounded_without_changing_guards(self):
        scripts = self.app / 'scripts'
        scripts.mkdir()
        for name in ('avito_outbox_cron.py', 'avito_monitor_cron.py'):
            (scripts / name).write_bytes((ROOT / 'scripts' / name).read_bytes())
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic outbox fixture')
        with patch.object(post, 'BASE', self.git('rev-parse', 'HEAD').strip()):
            module = post.helper(self.app, 'avito_outbox_cron')
        original = module.OutboxCronManager.verify_release
        module = post.bounded_outbox(module)
        self.assertIs(module.OutboxCronManager.verify_release, original)
        overflow = [sys.executable, '-c', 'import sys; sys.stderr.write("PRIVATE"*20000)']
        for run in (module.bounded, module._shared.bounded, module._shared.subprocess.run):
            with self.assertRaises(post.Failure):
                run(overflow)
        self.assertIs(post.subprocess, subprocess)

    def test_remote_deadline_kills_owned_child_and_releases_locks(self):
        import fcntl
        pid_file = self.app.parent / 'child.pid'
        script = 'import os, time; open(' + repr(str(pid_file)) + ', "w").write(str(os.getpid())); time.sleep(30)'
        with self.assertRaises(post.Failure), post.remote_budget(.2), post.locks(self.app, True):
            post.capture([sys.executable, '-c', script], timeout=20)
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with (self.app.parent / 'tmp/service2-deploy.lock').open('r') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)


class ProtocolTests(unittest.TestCase):
    def test_closed_receipt_schema(self):
        good = receipt(counts=dict.fromkeys(('configured', 'enabled', 'baselined', 'due', 'running', 'failed'), 0))
        expected = post.request(env())
        encoded = lambda value: (post.PREFIX + json.dumps(value) + '\n').encode()
        self.assertEqual(post.normalize(0, encoded(good), expected), good)
        variants = [dict(good, secret='PRIVATE'), dict(good, control_sha='b'*40),
            dict(good, installed_sha='b'*40), dict(good, request='123.2'), dict(good, completed=['PRIVATE']),
            dict(good, category='PRIVATE'), dict(good, phase='PRIVATE'), dict(good, counts={'private': 1})]
        for value in variants:
            with self.subTest(value=list(value)[-1]), self.assertRaises(post.Failure):
                post.normalize(0, encoded(value), expected)
        for data in (encoded(good)*2, encoded(good) + b'PRIVATE', b'PRIVATE\n' + encoded(good)):
            with self.assertRaises(post.Failure):
                post.normalize(0, data, expected)

    def test_receipt_distinguishes_guard_and_application_failure(self):
        for category, phase in (('guard', 'main'), ('guard', 'checkout'), ('application', 'metadata')):
            value = receipt(status='blocked', category=category, phase=phase, completed=[], reason='unavailable')
            data = (post.PREFIX + json.dumps(value) + '\n').encode()
            self.assertEqual(post.normalize(2, data, post.request(env()))['category'], category)
            with self.assertRaises(post.Failure):
                post.normalize(0, data, post.request(env()))

    def test_counts_are_not_arbitrary_output_or_booleans(self):
        counts = dict.fromkeys(('configured', 'enabled', 'baselined', 'due', 'running', 'failed'), 1)
        for bad in ('PRIVATE', True, -1, 10**9):
            value = receipt(counts={**counts, 'enabled': bad})
            with self.assertRaises(post.Failure):
                post.normalize(0, (post.PREFIX + json.dumps(value) + '\n').encode(), post.request(env()))

    def test_success_requires_operation_specific_evidence(self):
        for value in (receipt(), receipt('mcp'), receipt('avito-services', completed=['applied']),
                      receipt('avito-outbox', completed=[])):
            expected = post.request({**env(), 'POST_OPERATION': value['operation']})
            with self.assertRaises(post.Failure):
                post.normalize(0, (post.PREFIX + json.dumps(value) + '\n').encode(), expected)


class TraceTests(unittest.TestCase):
    app = Path('/synthetic/service_site')

    def run_operation(self, operation, **patches):
        expected = post.request({**env(), 'POST_OPERATION': operation})
        value = receipt(operation, completed=[], category='guard', status='blocked')
        guard = Mock()
        with contextlib.ExitStack() as stack:
            for name, mock in patches.items():
                stack.enter_context(patch.object(post, name, mock))
            post.operate(self.app, expected, value, guard)
        return value, guard

    def test_status_never_starts_setup_or_worker(self):
        with patch.object(post, 'capture', return_value=(0, b'AVITO_STATUS_MONITOR_READY configured=2 enabled=1 baselined=1 due=1 running=0 failed=0\n', b'PRIVATE')) as call:
            value, guard = self.run_operation('status')
        self.assertEqual(value['counts']['due'], 1)
        self.assertEqual(value['completed'], ['runtime_read'])
        self.assertEqual(call.call_args.args[0][-3:], ['manage.py', 'monitor_avito_statuses', '--status'])
        guard.assert_called_once()

    def test_mcp_activation_smoke_before_finalize(self):
        module, events = Mock(), []
        module.activate_once.side_effect = lambda *a: events.append('activate') or 'configured'
        module.finalize_marker.side_effect = lambda *a: events.append('finalize') or 'finalized'
        value, guard = self.run_operation('mcp', helper=Mock(return_value=module),
            restart=lambda *a: events.append('restart'), health=lambda: events.append('health'), metadata=lambda: events.append('metadata'))
        self.assertEqual(events, ['activate', 'restart', 'health', 'metadata', 'finalize'])
        self.assertEqual(value['completed'][-1], 'finalized')
        self.assertEqual(guard.call_count, 5)

    def test_failed_smoke_never_finalizes_or_rolls_back(self):
        module = Mock()
        module.activate_once.return_value = 'configured'
        with self.assertRaises(post.Failure):
            self.run_operation('mcp', helper=Mock(return_value=module), restart=Mock(), health=Mock(),
                metadata=Mock(side_effect=post.Failure()))
        module.finalize_marker.assert_not_called()
        module.configure_production.assert_not_called()

    def test_current_marker_still_requires_metadata_no_restart(self):
        module, restart = Mock(), Mock()
        module.activate_once.return_value = 'already_marked'
        with self.assertRaises(post.Failure):
            self.run_operation('mcp', helper=Mock(return_value=module), restart=restart, health=Mock(), metadata=Mock(side_effect=post.Failure()))
        restart.assert_not_called()
        module.finalize_marker.assert_not_called()

    def test_manual_source_change_stops_before_next_mutation(self):
        module = Mock()
        module.activate_once.return_value = 'configured'
        expected = post.request({**env(), 'POST_OPERATION': 'mcp'})
        value, restart = receipt('mcp', completed=[]), Mock()
        with patch.object(post, 'helper', return_value=module), patch.object(post, 'restart', restart), self.assertRaises(post.Failure):
            post.operate(self.app, expected, value, Mock(side_effect=[None, post.Failure()]))
        module.activate_once.assert_called_once()
        restart.assert_not_called()

    def test_outbox_only_setup_not_tick_or_send(self):
        module = Mock()
        manager = module.OutboxCronManager.return_value
        for result in ('installed', 'present', 'disabled', 'panel_setup_required'):
            manager.setup.return_value = result
            with patch.dict(os.environ, {}, clear=False):
                value, _ = self.run_operation('avito-outbox', helper=Mock(return_value=module))
            self.assertEqual(value['completed'], [result])
        manager.tick.assert_not_called()
        manager.change.assert_not_called()
        module.OutboxCronManager.assert_called_with(self.app, post.BASE)

    def test_services_private_output_is_not_forwarded_partial_setup_fails(self):
        data = {'setup': 'applied', 'private': 'PRIVATE TOKEN', 'webhook': {'registration_confirmed': True},
            'storage_probe': {'checked': True},
            'manual_photo_evidence': {'state': 'recorded', 'code': '', 'manual_photo': {}, 'provider_report': {}, 'facts': {}},
            'daily_statistics': {'state': 'preserved', 'enabled': True, 'error_code': '', 'natural_background_verified': False},
            'period_statistics': {'state': 'already_checked', 'evidence': {'state': 'available', 'reports': []}},
            'water_experiment_baseline': {'state': 'available'},
            'crm_source_links': {'state': 'ready', 'schema_version': 1},
            'improvement_projects': {'state': 'already_attempted', 'attempt_state': 'prepared', 'results': []},
            'assistant_wallet': {'state': 'read_confirmed', 'code': '', 'request_performed': True},
            'placement_calendar_check': {'state': 'verified', 'provider_write_performed': False},
            'assistant_read_tools': {'state': 'readers_checked', 'oauth_delivery_confirmed': False},
            'tariff_subscription': {'state': 'subscription_missing', 'probe_state': 'attempted', 'code': ''},
            # Additional native observations are deliberately not replaced with state=ready.
            # Their saved UI evidence needs a separate read; this is a partial result.
        }
        def captured(value):
            return (0, b'AVITO_SERVICES_SETUP ' + json.dumps(value).encode() + b'\n', b'PRIVATE')
        with patch.object(post, 'capture', return_value=captured(data)) as command:
            value = receipt('avito-services', completed=[])
            with self.assertRaises(post.Failure) as caught:
                post.services(self.app, value)
            self.assertEqual(caught.exception.category, 'partial')
        self.assertEqual(value['completed'], ['applied'])
        self.assertFalse(value['checks']['additional_setup_failed'])
        self.assertTrue(value['checks']['additional_setup_unconfirmed'])
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertIn('configure_avito_services_workplace', command.call_args.args[0])
        self.assertEqual(command.call_args.kwargs['env']['SERVICE2_AVITO_DEPLOY_SETUP'], '1')
        for changes in ({'webhook': {'registration_confirmed': False}}, {'storage_probe': {'checked': False}},
                        {'daily_statistics': {'state': 'blocked', 'code': 'PRIVATE'}},
                        {'assistant_wallet': {'state': 'failed'}}, {'assistant_wallet': {'state': 'unconfirmed'}},
                        {'period_statistics': {'state': 'unavailable'}},
                        {'improvement_projects': {'state': 'already_attempted', 'attempt_state': 'blocked'}},
                        {'period_statistics': {'state': 'ready', 'evidence': {'state': 'unavailable'}}},
                        {'assistant_wallet': {'state': 'PRIVATE UNKNOWN'}}, {'assistant_wallet': None}):
            with patch.object(post, 'capture', return_value=captured({**data, **changes})), self.assertRaises(post.Failure):
                self.run_operation('avito-services')

    def test_services_unknown_or_missing_evidence_is_not_success(self):
        for value in (None, {}, [], {'enabled': True}, {'state': 'ready', 'code': 'PRIVATE'},
                      {'state': 'read_attempted'}, {'state': 'already_attempted', 'attempt_state': 'blocked'},
                      {'state': 'ready', 'evidence': [{'probe_state': 'unconfirmed'}]}):
            self.assertNotEqual(post.setup_evidence(value), 'confirmed')
        self.assertEqual(post.setup_evidence({'state': 'already_checked', 'evidence': {'state': 'available'}}), 'confirmed')
        self.assertEqual(post.setup_evidence({'state': 'recorded', 'code': ''}), 'confirmed')
        self.assertEqual(post.setup_evidence({'state': 'already_attempted', 'attempt_state': 'prepared'}), 'confirmed')
        self.assertEqual(post.setup_evidence({'state': 'available', 'probe_state': 'attempted'}), 'confirmed')
        self.assertEqual(post.setup_evidence({'state': 'available', 'probe_state': 'unconfirmed'}), 'unconfirmed')

    def test_services_partial_is_distinct_from_failure_and_success(self):
        value = receipt('avito-services', completed=['applied'], effects_possible=True,
            phase='services', status='partial', category='application', reason='private_review_required',
            checks={'webhook_registered': True, 'storage_verified': True,
                    'additional_setup_failed': False, 'additional_setup_unconfirmed': True})
        expected = post.request({**env(), 'POST_OPERATION': 'avito-services'})
        raw = (post.PREFIX + json.dumps(value) + '\n').encode()
        self.assertEqual(post.normalize(3, raw, expected), value)
        for code in (0, 2):
            with self.assertRaises(post.Failure):
                post.normalize(code, raw, expected)

    def test_https_metadata_and_health_are_exact_and_bounded(self):
        correct = {'resource': post.SITE + '/mcp/1c', 'authorization_servers': [post.SITE + '/onec-diagnostic'],
                   'scopes_supported': ['onec.diagnostic.read']}
        with patch.object(post, 'https_json', return_value=correct):
            post.metadata()
        for change in ({'resource': 'https://foreign.invalid'}, {'authorization_servers': [post.SITE]}, {'scopes_supported': []}):
            with patch.object(post, 'https_json', return_value={**correct, **change}), self.assertRaises(post.Failure):
                post.metadata()
        with patch.object(post, 'https_json', return_value={'status': 'ok'}) as call:
            post.health()
            self.assertEqual([c.args[0] for c in call.call_args_list], ['/health/live/', '/health/ready/'])
        self.assertIsNone(post.NoRedirect().redirect_request(None, None, None, None, None, None))


class CaptureTests(unittest.TestCase):
    def test_transport_is_pinned_and_does_not_allow_shell_input(self):
        valid = {**env(), 'DEPLOY_HOST': 'example.invalid', 'DEPLOY_USER': 'synthetic',
                 'DEPLOY_PORT': '22', 'DEPLOY_APP_PATH': '/synthetic/service_site'}
        with patch.object(post, 'regular'), patch.object(post, 'capture', return_value=(0, b'', b'')) as capture:
            post.transport(valid, post.request(env()), ROOT)
            args = capture.call_args.args[0]
            for item in ('/dev/null', 'IdentityAgent=none', 'StrictHostKeyChecking=yes',
                         'PasswordAuthentication=no', 'IdentitiesOnly=yes', 'ConnectTimeout=20'):
                self.assertIn(item, args)
            self.assertIn('exec ../venv/bin/python -B -I - remote', args[-1])
            self.assertEqual(capture.call_args.kwargs['source'], (ROOT / '.github/scripts/service2_post_deploy.py').read_bytes())
        for field, value in (('DEPLOY_HOST', '-oProxyCommand=PRIVATE'), ('DEPLOY_PORT', '22;PRIVATE'),
                             ('DEPLOY_USER', 'user$(PRIVATE)'), ('DEPLOY_APP_PATH', '/tmp/../service_site')):
            with patch.object(post, 'capture') as capture, self.assertRaises(post.Failure):
                post.transport({**valid, field: value}, post.request(env()), ROOT)
            capture.assert_not_called()

    def test_child_stdout_and_stderr_are_private_and_bounded(self):
        code, out, err = post.capture([sys.executable, '-c', 'import sys; print("synthetic"); print("PRIVATE", file=sys.stderr)'])
        self.assertEqual((code, out, err), (0, b'synthetic\n', b'PRIVATE\n'))
        with self.assertRaises(post.Failure):
            post.capture([sys.executable, '-c', 'print("x"*10000)'], limit=100)

    def test_actual_deadline_is_bounded(self):
        with self.assertRaises(post.Failure) as caught:
            post.capture([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.1)
        self.assertEqual(caught.exception.category, 'deadline')

    def test_closed_stdin_early_exit_is_not_retry(self):
        code, out, err = post.capture([sys.executable, '-c', 'raise SystemExit(7)'], source=b'x'*1000000)
        self.assertEqual(code, 7)


class WorkflowTests(unittest.TestCase):
    def test_addressed_job_never_reaches_existing_deploy_jobs(self):
        value = (ROOT / '.github/workflows/ci-deploy.yml').read_text()
        job = value.split('  post-deploy:\n', 1)[1].split('\n  scope:', 1)[0]
        self.assertIn('environment: production', job)
        self.assertIn("github.event_name == 'workflow_dispatch'", job)
        self.assertIn(post.CONTROL_REF, job)
        self.assertIn("if: github.event_name != 'workflow_dispatch' || inputs.release_mode != 'post-deploy'", value)
        self.assertIn('fetch-depth: 0', job)
        for forbidden in ('update.sh', 'migrate', 'normalize', 'pip install', 'worker', 'workflow_dispatch.py'):
            self.assertNotIn(forbidden, job)
        self.assertNotIn('service2-release-prepare', value)
        self.assertIn('branches: [main]', value)


if __name__ == '__main__':
    unittest.main()
