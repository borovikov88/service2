import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from scripts import crm_wakeup_diagnostic as sink
from pool_service.services.crm_intake_wakeup import wake


class WakeSnapshotTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / 'app'
        self.app.mkdir()
        self.tmp = self.app.parent / 'tmp'
        self.tmp.mkdir(mode=0o700)
        self.path = self.tmp / sink.NAME
        self.metadata = ('unsafe_wakeup_directory_owner', 'NONE', False)

    def test_bounded_replacement_private_mode_and_read_only_command(self):
        for _ in range(50):
            sink.record_failure(self.app, self.metadata)
        self.assertLessEqual(self.path.stat().st_size, sink.LIMIT)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        before = self.path.read_bytes()
        with override_settings(BASE_DIR=self.app):
            out = io.StringIO()
            call_command('crm_wakeup_diagnostic', stdout=out)
        result = json.loads(out.getvalue().split(' ', 1)[1])
        self.assertEqual(result['sample']['code'], self.metadata[0])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.tmp.iterdir()), [self.path])

    def test_owner_mismatch_launcher_failure_recorded_independently(self):
        # Real launcher stops at existing real-UID guard, not mocked launch.
        (self.app / '.git').mkdir()
        with override_settings(BASE_DIR=self.app), patch('scripts.crm_intake_event_wakeup.os.getuid', return_value=os.geteuid()+1):
            with self.assertLogs('pool_service.services.crm_intake_wakeup', level='WARNING'):
                wake()
        sample = sink.read_failure(self.app)['sample']
        self.assertEqual(sample['code'], 'unsafe_wakeup_directory_owner')
        self.assertFalse(sample['real_effective_uid_equal'])
        self.assertEqual(list((self.tmp / 'crm-intake-wakeup').iterdir()), [])

    def test_symlink_hardlink_public_mode_and_foreign_owner_rejected(self):
        victim = self.app / 'victim'
        victim.write_text('unchanged')
        self.path.symlink_to(victim)
        sink.record_failure(self.app, self.metadata)
        self.assertEqual(sink.read_failure(self.app)['status'], 'unavailable')
        self.path.unlink()
        os.link(victim, self.path)
        sink.record_failure(self.app, self.metadata)
        self.assertEqual(victim.read_text(), 'unchanged')
        self.path.unlink()
        sink.record_failure(self.app, self.metadata)
        self.path.chmod(0o644)
        before = self.path.read_bytes()
        sink.record_failure(self.app, self.metadata)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sink.read_failure(self.app)['status'], 'unavailable')
        self.path.chmod(0o600)
        with patch.object(sink.os, 'geteuid', return_value=os.geteuid()+1):
            sink.record_failure(self.app, self.metadata)
            self.assertEqual(sink.read_failure(self.app)['status'], 'unavailable')
        self.assertEqual(self.path.read_bytes(), before)

    def test_busy_lock_drops_sample_without_wait_or_mutation(self):
        sink.record_failure(self.app, self.metadata)
        before = self.path.read_bytes()
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sink.record_failure(self.app, ('unexpected_failure', 'EACCES', True))
            self.assertEqual(sink.read_failure(self.app)['status'], 'unavailable')
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            os.close(fd)

    def test_untrusted_content_absent_partial_oversized_and_no_secret_echo(self):
        self.assertEqual(sink.read_failure(self.app), {'status': 'absent'})
        sink.record_failure(self.app, ('secret path', 'private payload', 123))
        sample = sink.read_failure(self.app)['sample']
        self.assertEqual((sample['code'], sample['errno'], sample['real_effective_uid_equal']), ('unexpected_failure', 'OTHER', None))
        for content in ('secret', 'x' * 513, '{"code":"secret"}'):
            self.path.write_text(content)
            self.assertEqual(sink.read_failure(self.app), {'status': 'unavailable'})

    def test_sink_failure_never_breaks_callback(self):
        with override_settings(BASE_DIR=self.app), patch('scripts.crm_intake_event_wakeup.launch', side_effect=PermissionError('secret')):
            with patch.object(sink, 'record_failure', side_effect=OSError('secret')):
                with self.assertLogs('pool_service.services.crm_intake_wakeup', level='WARNING') as logs:
                    wake()
        self.assertNotIn('secret', str(logs.output))

    def test_directory_symlink_or_writable_by_others_rejected(self):
        self.tmp.chmod(0o777)
        sink.record_failure(self.app, self.metadata)
        self.assertFalse(self.path.exists())
        self.tmp.rmdir()
        self.tmp.symlink_to(self.app, target_is_directory=True)
        sink.record_failure(self.app, self.metadata)
        self.assertFalse((self.app / sink.NAME).exists())

    def test_io_error_or_partial_write_is_safe(self):
        with patch.object(sink.os, 'write', side_effect=OSError('disk full secret')):
            sink.record_failure(self.app, self.metadata)
        self.assertEqual(sink.read_failure(self.app), {'status': 'unavailable'})
