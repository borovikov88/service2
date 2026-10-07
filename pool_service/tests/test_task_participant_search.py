"""Run the task participant DOM regressions in the existing Django CI suite."""
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.test import SimpleTestCase


class TaskParticipantSearchTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "Node.js is needed for JavaScript DOM regressions")
    def test_task_participant_search_script(self):
        tests_dir = Path(__file__).resolve().parent
        result = subprocess.run(
            [shutil.which("node"), "--test", str(tests_dir / "task_participant_search.test.cjs")],
            cwd=tests_dir.parent.parent,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
