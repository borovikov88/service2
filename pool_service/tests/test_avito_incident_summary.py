import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pool_service.tests import test_hosting_connection


hosting = test_hosting_connection.hosting
SCRIPT, incident = hosting.incident_helper()


def event(when="2026-10-08 13:09:12,345", route="/avito/1/refresh/", body=""):
    return f"{when} ERROR django.request status=500 Internal Server Error: {route}\n{body}\n"


class AvitoIncidentSummaryTests(unittest.TestCase):
    def test_only_known_metadata_survives_secret_messages_and_chained_tracebacks(self):
        raw = event(body='''Traceback (most recent call last):
  File "/private/owner/site/pool_service/avito_management.py", line 511, in avito_refresh_data
    token = "SECRET_SOURCE"
  File "/private/owner/venv/lib/mysql.py", line 20, in query
    private_customer = "PRIVATE_PERSON"
MySQLdb.OperationalError: (2006, 'SECRET_DB_MESSAGE')
The above exception was the direct cause of the following exception:
  File "/private/owner/site/pool_service/avito_management.py", line 540, in avito_refresh_data
    raise SECRET_SOURCE
django.db.utils.OperationalError: SECRET_TOKEN
SECRET_EXCEPTIONError: private body
  File "/private/site/pool_service/SECRET_PATH.py", line 4, in unknown
    SECRET_CODE
''')
        result = incident.summarize(raw.encode())
        self.assertEqual(result["records"], [{
            "timestamp": "2026-10-08T13:09:12.345000+00:00",
            "exceptions": ["MySQLdb.OperationalError", "django.db.utils.OperationalError"],
            "frames": [{"file": "pool_service/avito_management.py", "line": 511},
                       {"file": "pool_service/avito_management.py", "line": 540}],
            "unknown_exception_withheld": True,
        }])
        for forbidden in ("SECRET", "private", "PRIVATE", "2006", "/avito/1"):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_exact_window_timezone_and_endpoint_boundaries(self):
        raw = "".join([
            event("2026-10-08 13:07:59,999", body="TypeError: private"),
            event("2026-10-08 13:08:00,000", body="ValueError: private"),
            event("2026-10-08T20:09:00+07:00", body="KeyError: private"),
            event("2026-10-08 13:11:00,000", body="AttributeError: private"),
            event(route="/finance/", body="TypeError: private"),
            event(route="/avito/1/refresh/?secret=private", body="TypeError: private"),
            event("2026-10-08 25:09:00", body="TypeError: private"),
        ])
        result = incident.summarize(raw.encode())
        self.assertEqual([r["exceptions"] for r in result["records"]], [["ValueError"], ["KeyError"]])
        self.assertEqual(result["records"][1]["timestamp"], "2026-10-08T13:09:00+00:00")

    def test_continuation_from_cutoff_and_other_record_is_not_attached(self):
        raw = (event(body="TypeError: secret") + "2026-10-08 13:09:13 ERROR other.logger secret\n"
               "ValueError: private\n" + event(body="KeyError: private"))
        result = incident.summarize(raw.encode(), tail_cutoff=True)
        self.assertTrue(result["tail_cutoff"])
        self.assertEqual([x["exceptions"] for x in result["records"]], [["KeyError"]])

    def test_records_and_frames_are_bounded(self):
        body = "\n".join(f'  File "/app/pool_service/avito_management.py", line {i}, in refresh' for i in range(1, 50))
        result = incident.summarize((event(body=body) * 50).encode())
        self.assertEqual(len(result["records"]), 10)
        self.assertEqual(len(result["records"][0]["frames"]), 12)

    def test_runner_revalidates_and_never_echoes_unexpected_stdout(self):
        valid = incident.summarize(event(body="TypeError: SECRET_TOKEN").encode())
        valid["SECRET_KEY"] = "SECRET_VALUE"
        valid["records"][0]["payload"] = "SECRET_VALUE"
        output = hosting.incident_output("PRIVATE_BANNER\n" + incident.MARKER + json.dumps(valid), incident)
        self.assertNotIn("SECRET", output)
        self.assertNotIn("PRIVATE", output)
        for changes in ({"status": []}, {"records": [{"timestamp": {}}]},
                        {"records": [{"timestamp": "2026-10-08T13:09:00Z", "exceptions": ["SECRETError"], "frames": []}]}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "Invalid incident output"):
                hosting.incident_output(incident.MARKER + json.dumps({**valid, **changes}), incident)
        for value in ("PRIVATE_RAW_LOG", incident.MARKER + "not json", incident.MARKER + "{}\n" + incident.MARKER + "{}"):
            with self.assertRaises(ValueError):
                hosting.incident_output(value, incident)

    def test_fixed_file_read_is_bounded_read_only_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            log = root / "var/log/django-request-error.log"
            log.parent.mkdir(parents=True)
            log.write_bytes(b"private old line\n" * 100 + event(body="TypeError: PRIVATE").encode())
            before = log.read_bytes()
            with patch.object(incident.Path, "cwd", return_value=app), patch.object(incident, "MAX_BYTES", 200):
                result = incident.read_fixed_log()
                self.assertTrue(result["tail_cutoff"])
                self.assertEqual(result["records"][0]["exceptions"], ["TypeError"])
                self.assertEqual(before, log.read_bytes())
                log.unlink()
                log.symlink_to(root / "secret")
                self.assertEqual(incident.read_fixed_log()["status"], "log_not_regular")

    def test_isolated_remote_script_does_not_import_settings_or_write_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            (app / "manage.py").write_text("raise Exception('MUST_NOT_RUN')")
            (app / ".env").write_text("SECRET_TOKEN=PRIVATE")
            log = root / "var/log/django-request-error.log"
            log.parent.mkdir(parents=True)
            log.write_text(event(body="ValueError: PRIVATE"))
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            run = subprocess.run([sys.executable, "-B", "-I", "-"], input=SCRIPT.read_text(),
                                 cwd=app, capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(run.stderr, "")
            self.assertNotIn("PRIVATE", run.stdout)
            self.assertIn('"ValueError"', run.stdout)
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})


class AvitoIncidentHostingTests(unittest.TestCase):
    setUp = test_hosting_connection.HostingConnectionTests.setUp
    _mock_run = test_hosting_connection.HostingConnectionTests._mock_run

    def test_incident_uses_same_credentials_and_fixed_stdin_only(self):
        self.commands, self.temporary_paths = [], []
        def run(command, **kwargs):
            result = self._mock_run(command, **kwargs)
            if command[0] == "ssh":
                result.stdout = "PRIVATE_BANNER\n" + incident.MARKER + json.dumps(incident.summarize(b""))
            return result
        output = io.StringIO()
        with patch.object(hosting.subprocess, "run", side_effect=run), contextlib.redirect_stdout(output):
            hosting.run_check(self.config, avito_incident=True)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(self.commands[3][1]["input"].split("AVITO_FIXED_INCIDENT'\n")[1].rsplit("\nAVITO_FIXED_INCIDENT", 1)[0], SCRIPT.read_text())
        self.assertTrue(all(not p.exists() for p in self.temporary_paths))
