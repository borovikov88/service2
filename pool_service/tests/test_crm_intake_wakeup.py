import fcntl
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.db import transaction
from django.test import TestCase, SimpleTestCase, override_settings
from pool_service.services.crm_intake_wakeup import schedule_wakeup, wake
from scripts import crm_intake_event_wakeup as worker


@override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=True)
class CommitWakeTests(TestCase):
    def event(self, **kw):
        return SimpleNamespace(**dict(dict(organization_id=1, scope_verified=True,
            historical=False, state='pending', _state=SimpleNamespace(db='default')), **kw))

    def test_commit_only_and_rollback_discards_wake(self):
        with patch('pool_service.services.crm_intake_wakeup.wake') as start:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic(): schedule_wakeup(self.event())
                start.assert_not_called()
            start.assert_called_once()
            start.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        schedule_wakeup(self.event())
                        raise ValueError('rollback')
                except ValueError: pass
            start.assert_not_called()

    def test_disabled_historical_unverified_other_org_terminal_do_not_wake(self):
        with self.captureOnCommitCallbacks(execute=True), patch('pool_service.services.crm_intake_wakeup.wake') as start:
            for kw in ({'historical':True},{'scope_verified':False},{'organization_id':2},{'state':'done'},{'state':'failed'}):
                schedule_wakeup(self.event(**kw))
            with override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=False):schedule_wakeup(self.event())
            start.assert_not_called()

    def test_spawn_failure_does_not_escape_into_ack(self):
        with patch('scripts.crm_intake_event_wakeup.launch',side_effect=OSError('synthetic')):
            with self.assertLogs('pool_service.services.crm_intake_wakeup',level='WARNING') as logs:wake()
        self.assertNotIn('synthetic',str(logs.output))


class HostWakeTests(SimpleTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Path(self.temp.name)/'app';self.app.mkdir();(self.app/'.git').mkdir()
        (self.app.parent/'tmp').mkdir()

    def test_busy_coalesces_without_second_process_or_network(self):
        state=worker._state(self.app);fd=worker._open(state/'worker.lock')
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            with patch.object(worker.subprocess,'Popen') as spawn:
                self.assertIsNone(worker.launch(self.app,'python'));spawn.assert_not_called()
            self.assertEqual(len((state/'generation').read_text()),32)
        finally:worker.os.close(fd)

    def test_spawn_uses_detached_process_and_inherited_lock(self):
        with patch.object(worker.subprocess,'Popen') as spawn:
            worker.launch(self.app,'python')
            kw=spawn.call_args.kwargs
            self.assertTrue(kw['start_new_session']);self.assertEqual(len(kw['pass_fds']),1)
            self.assertEqual(kw['stdout'],worker.subprocess.DEVNULL)

    def test_symlink_and_public_state_rejected(self):
        state=worker._state(self.app);(state/'dispatch.lock').symlink_to(self.app/'victim')
        with self.assertRaises(OSError):worker.launch(self.app,'python')
        (state/'dispatch.lock').unlink();state.chmod(0o755)
        with self.assertRaises(ValueError):worker.launch(self.app,'python')

    def test_generation_change_during_worker_gets_second_pass_and_unlocks(self):
        state=worker._state(self.app);fd=worker._open(state/'worker.lock')
        fcntl.flock(fd,fcntl.LOCK_EX);(state/'generation').write_text('first');(state/'generation').chmod(0o600)
        calls=[]
        def run(*args,**kwargs):
            calls.append(args)
            if len(calls)==1:
                with patch.object(worker.subprocess,'Popen') as spawn:
                    self.assertIsNone(worker.launch(self.app,'python'));spawn.assert_not_called()
            return 0
        with patch.object(worker.subprocess,'check_output',return_value='a'*40),patch.object(worker,'_wrapper',side_effect=run):worker.run(self.app,fd)
        self.assertEqual(len(calls),2)
        fd=worker._open(state/'worker.lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:worker.os.close(fd)

    def test_wrapper_failure_does_not_loop_or_requeue(self):
        state=worker._state(self.app);fd=worker._open(state/'worker.lock');fcntl.flock(fd,fcntl.LOCK_EX)
        with patch.object(worker.subprocess,'check_output',return_value='a'*40),patch.object(worker,'_wrapper',return_value=2) as execute:
            worker.run(self.app,fd)
        execute.assert_called_once()

from pool_service.tests.test_crm_inbox import Fixture
from pool_service.crm_inbox_models import CrmInboxEvent

@override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=True, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class CaptureWakeTests(Fixture, TestCase):
    def test_capture_registers_commit_and_failure_keeps_durable_row(self):
        from pool_service.services.crm_inbox import capture_source
        with patch('pool_service.services.crm_intake_wakeup.wake') as start:
            with self.captureOnCommitCallbacks(execute=True):
                self.incoming()
                start.assert_not_called()
            # Fixture organization is not necessarily pilot ID=1. Exercise exact
            # real capture with the configured explicit pilot in a fresh DB.
            self.assertEqual(start.call_count, int(self.org.pk == 1))
        event=CrmInboxEvent.objects.get()
        self.assertEqual(event.state,'pending')
        with patch('scripts.crm_intake_event_wakeup.launch',side_effect=OSError('synthetic')):
            with self.captureOnCommitCallbacks(execute=True):
                self.incoming()
        self.assertEqual(CrmInboxEvent.objects.count(),1)
        self.assertEqual(CrmInboxEvent.objects.get().state,'pending')

class ProcessLifetimeTests(SimpleTestCase):
    def test_timeout_stops_wrapper_and_grandchild(self):
        self._timeout_case('30',0.5)

    def test_inner_timeout_also_cleans_orphan(self):
        self._timeout_case('0.3',2)

    def _timeout_case(self, inner, outer):
        import os, sys, time
        with tempfile.TemporaryDirectory() as directory:
            pidfile=Path(directory)/'child.pid'
            code="import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)"
            self.assertEqual(worker._wrapper(['timeout','--foreground',inner,sys.executable,'-c',code,str(pidfile)],directory,outer),124)
            self.assertTrue(pidfile.exists())
            pid=int(pidfile.read_text())
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                status=Path('/proc')/str(pid)/'stat'
                if not status.exists() or status.read_text().split()[2]=='Z':break
                time.sleep(0.01)
            else:self.fail('grandchild remains running after bounded cleanup')

    def test_dispatch_wait_is_bounded_and_normal_exit_handoff_retries(self):
        import os,threading,time
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lock';first=worker._open(path);second=worker._open(path)
            try:
                fcntl.flock(first,fcntl.LOCK_EX)
                start=time.monotonic();self.assertFalse(worker._lock(second,0.03))
                self.assertLess(time.monotonic()-start,0.2)
                thread=threading.Thread(target=lambda:(time.sleep(0.03),fcntl.flock(first,fcntl.LOCK_UN)))
                thread.start()
                self.assertTrue(worker._lock(second,0.1));thread.join()
            finally:os.close(first);os.close(second)
