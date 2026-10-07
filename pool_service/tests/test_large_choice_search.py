"""Run the shared-select browser regression from the normal Django CI suite."""
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase


class LargeChoiceSearchBrowserTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "Node.js is required for UI regressions")
    def test_search_first_choices_in_chromium(self):
        script = Path(__file__).with_name("large_choice_search.test.cjs")
        result = subprocess.run(
            [shutil.which("node"), "--test", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if "# skipped 1" in result.stdout and "# pass 0" in result.stdout:
            self.skipTest("Chromium or Node WebSocket is unavailable")
