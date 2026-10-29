"""Synthetic pre-migrate fail-closed and inherited-lock integration proofs."""
import datetime as dt
import fcntl
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import crm_pre_migrate_backup as G
B = G.backup
SHA = 'a' * 40


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'app'; self.app.mkdir()
        (self.root / 'tmp').mkdir(mode=0o700)
        self.saved = {}
        for fd, name in ((8, 'service2-call-ai.lock'), (9, 'service2-deploy.lock')):
            try: self.saved[fd] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 30)
            except OSError: self.saved[fd] = None
            opened = os.open(self.root / 'tmp' / name, os.O_RDWR | os.O_CREAT, 0o600)
            os.dup2(opened, fd)
            if opened != fd: os.close(opened)
            fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(self.restore_fds)

    def restore_fds(self):
        for fd, saved in self.saved.items():
            os.close(fd)
            if saved is not None:
                os.dup2(saved, fd); os.close(saved)

    def make_backup(self, app, expected):
        target = app.parent / 'tmp' / 'service2-crm-backups'
        B.private_dir(target, create=True)
        now = dt.datetime.now(dt.timezone.utc)
        basename = expected + '-' + now.strftime('%Y%m%dT%H%M%SZ') + '-0123456789abcdef'
        archive = target / (basename + '.sql.gz')
        with gzip.open(archive, 'wb') as stream: stream.write(b'synthetic SQL')
        archive.chmod(0o600)
        value = {'kind':'crm_pilot_db_backup','schema_version':1,'expected_sha':expected,
                 'table_count':1,'size_bytes':archive.stat().st_size,'uncompressed_bytes':13,
                 'sha256':B.verify_gzip(archive,13),'schema_sha256':'b'*64,
                 'started_at':now.isoformat(),'completed_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                 'integrity_verified':True,'restore_verified':False,'backup_file':archive.name}
        proof = target / (basename + '.receipt.json')
        proof.write_text(json.dumps(value)); proof.chmod(0o600)
        return value

    def invoke(self, operation=None):
        with patch.object(B, 'check_checkout'), patch.object(B,'backup',side_effect=operation or self.make_backup):
            return G.run(self.app, SHA)

    def test_valid_backup_keeps_both_parent_locks_and_inherited_child_does_not_deadlock(self):
        self.assertEqual(self.invoke()['expected_sha'],SHA)
        code = 'import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;import crm_pre_migrate_backup as g;g.locks(Path(sys.argv[2]))'
        child=subprocess.run([sys.executable,'-c',code,str(ROOT/'scripts'),str(self.app)],pass_fds=(8,9),capture_output=True,timeout=5)
        self.assertEqual(child.returncode,0,child.stderr)
        G.locks(self.app)
        for name in ('service2-call-ai.lock','service2-deploy.lock'):
            code='import os,fcntl,sys;f=os.open(sys.argv[1],os.O_RDWR);fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
            contender=subprocess.run([sys.executable,'-c',code,str(self.root/'tmp'/name)],capture_output=True,timeout=5)
            self.assertNotEqual(contender.returncode,0)

    def test_unlocked_shared_or_separate_open_description_cannot_be_promoted(self):
        for mode in (fcntl.LOCK_UN,fcntl.LOCK_SH):
            fcntl.flock(9,mode)
            with self.assertRaises(G.GateBlocked): G.locks(self.app)
            fcntl.flock(9,fcntl.LOCK_EX)
        other=os.open(self.root/'tmp/service2-deploy.lock',os.O_RDWR)
        try:
            with self.assertRaises(G.GateBlocked): G.inherited_lock(self.app,other,'service2-deploy.lock')
        finally: os.close(other)

    def test_replaced_symlink_hardlink_or_writable_lock_is_rejected(self):
        path=self.root/'tmp/service2-deploy.lock'
        path.chmod(0o666)
        with self.assertRaises(G.GateBlocked): G.locks(self.app)
        path.chmod(0o600)
        os.link(path,self.root/'tmp/extra')
        with self.assertRaises(G.GateBlocked): G.locks(self.app)
        (self.root/'tmp/extra').unlink()
        path.rename(self.root/'tmp/old')
        path.symlink_to(self.root/'tmp/old')
        with self.assertRaises(G.GateBlocked): G.locks(self.app)
        path.unlink(); path.touch(mode=0o600)
        with self.assertRaises(G.GateBlocked): G.locks(self.app)

    def test_no_space_prevents_dump(self):
        with patch.object(B,'check_checkout'), patch.object(G.shutil,'disk_usage',return_value=types.SimpleNamespace(free=0)),patch.object(B,'backup') as called:
            with self.assertRaisesRegex(G.GateBlocked,'disk_space'): G.run(self.app,SHA)
            called.assert_not_called()

    def test_dump_error_propagates_and_never_returns_success(self):
        for error in (B.NativeFailure(2),OSError(28,'private'),TimeoutError('private')):
            with self.subTest(error=type(error).__name__),self.assertRaises(type(error)):
                self.invoke(lambda *_: (_ for _ in ()).throw(error))

    def test_stale_wrong_sha_missing_corrupt_or_unsafe_files_fail(self):
        def operation(kind):
            def write(app, expected):
                value=self.make_backup(app,expected)
                archive=app.parent/'tmp/service2-crm-backups'/value['backup_file']
                if kind=='stale': value['started_at']=value['completed_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=1)).isoformat()
                if kind=='sha': value['expected_sha']='c'*40
                if kind=='missing': archive.unlink()
                if kind=='corrupt': archive.write_bytes(b'x'*value['size_bytes'])
                if kind=='mode': archive.chmod(0o644)
                if kind=='extra': value['private_field']='must not be accepted'
                return value
            return write
        for kind in ('stale','sha','missing','corrupt','mode','extra'):
            with self.subTest(kind=kind),self.assertRaises(Exception): self.invoke(operation(kind))
            for path in (self.root/'tmp/service2-crm-backups').iterdir(): path.unlink()

    def test_deadline_stops_and_reaps_native_child(self):
        children=[]; real=B.subprocess.Popen
        def spawn(*args,**kwargs):
            child=real(*args,**kwargs);children.append(child);return child
        with patch.object(B.subprocess,'Popen',side_effect=spawn):
            with self.assertRaises(B.Blocked):
                B.dump_to_gzip([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],
                              self.root/'timeout.partial',{},time.monotonic()+0.1)
        self.assertIsNotNone(children[0].poll())

    def test_update_order_and_fixed_watchdog(self):
        script=(ROOT/'update.sh').read_text()
        self.assertLess(script.index('git checkout --detach "$EXPECTED_SHA"'),script.index('scripts/crm_pre_migrate_backup.py'))
        self.assertLess(script.index('scripts/crm_pre_migrate_backup.py'),script.index('python manage.py migrate --noinput'))
        self.assertIn('timeout --signal=TERM --kill-after=10 330 python scripts/crm_pre_migrate_backup.py',script)
        self.assertLess(script.index('python manage.py migrate --noinput'),script.index('flock -u 9'))
        self.assertIn('test_crm_pre_migrate_backup.py',(ROOT/'.github/workflows/ci-deploy.yml').read_text())

    def test_main_sanitizes_failure_and_clears_watchdog(self):
        import contextlib
        import io
        out,err=io.StringIO(),io.StringIO()
        previous=signal.getsignal(signal.SIGTERM)
        with patch.object(sys,'argv',['gate','--expected-sha',SHA]), patch.object(G,'run',side_effect=RuntimeError('PRIVATE secret')):
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                self.assertEqual(G.main(),2)
        self.assertEqual(out.getvalue(),'')
        self.assertNotIn('PRIVATE',err.getvalue())
        self.assertIn('CRM_PRE_MIGRATE_BACKUP_BLOCKED',err.getvalue())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0],0)
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous)

    def test_no_receipt_is_not_success(self):
        for value in (None,{}, {'integrity_verified':True}):
            with self.subTest(value=value),self.assertRaises(Exception): self.invoke(lambda *_:value)

    def test_sigkill_helper_stops_separate_native_session(self):
        pid_file = self.root / 'native.pid'
        child_code = 'import os,time;from pathlib import Path;Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid()));time.sleep(60)'
        helper_code = ('import sys,time;from pathlib import Path;sys.path.insert(0,' + repr(str(ROOT/'scripts')) + ');'
                       'import crm_pilot_db_backup as b;b.dump_to_gzip(' + repr([sys.executable,'-c',child_code]) + ','
                       'Path(' + repr(str(self.root/'hard-kill.partial')) + '),{},time.monotonic()+60)')
        helper = subprocess.Popen([sys.executable,'-c',helper_code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        child = None
        def running(pid):
            try:
                data=Path(f'/proc/{pid}/stat').read_text()
                return data[data.rindex(')')+2:].split()[0] != 'Z'
            except FileNotFoundError: return False
        try:
            deadline=time.monotonic()+5
            while not pid_file.exists() and time.monotonic()<deadline: time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            child=int(pid_file.read_text())
            self.assertTrue(running(child))
            helper.kill();helper.wait(timeout=3)
            deadline=time.monotonic()+3
            while running(child) and time.monotonic()<deadline: time.sleep(0.01)
            self.assertFalse(running(child), 'Native process must not continue after helper SIGKILL')
        finally:
            if helper.poll() is None: helper.kill();helper.wait(timeout=3)
            if child and running(child): os.killpg(child,signal.SIGKILL)

    def test_threaded_helper_is_rejected_before_native_spawn(self):
        with patch.object(Path,'iterdir',return_value=iter((Path('one'),Path('two')))):
            with self.assertRaisesRegex(B.Blocked,'native_process_threads'):
                B.native_parent_death_guard(sys.executable)

    def test_parent_death_setup_failure_never_executes_dump(self):
        class FailedPrctl:
            def __call__(self, *args): return -1
        marker=self.root/'must-not-run'
        code='from pathlib import Path;Path('+repr(str(marker))+').touch()'
        with patch.object(B.ctypes,'CDLL',return_value=types.SimpleNamespace(prctl=FailedPrctl())):
            with self.assertRaises(B.NativeFailure):
                B.dump_to_gzip([sys.executable,'-c',code],self.root/'failed.partial',{},time.monotonic()+2)
        self.assertFalse(marker.exists())

    def test_file_capabilities_or_unreadable_attributes_reject_before_spawn(self):
        import errno
        for outcome in (b'capability',b'',OSError(errno.EACCES,'private')):
            kwargs={'side_effect':outcome} if isinstance(outcome,Exception) else {'return_value':outcome}
            with self.subTest(outcome=type(outcome).__name__), patch.object(B.os,'getxattr',**kwargs),patch.object(B.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(B.Blocked,'native_parent_guard'):
                    B.dump_to_gzip([sys.executable,'-c','pass'],self.root/'must-not-open',{},time.monotonic()+2)
                spawn.assert_not_called()

if __name__ == '__main__': unittest.main()
