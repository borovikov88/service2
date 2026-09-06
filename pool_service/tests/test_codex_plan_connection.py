"""Offline canary boundaries; no real credentials, model calls or database."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "codex_plan_connection", ROOT / ".github/scripts/check_codex_plan_connection.py"
)
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def valid_events():
    return [
        {"type": "thread.started", "thread_id": "test-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": canary.EXPECTED,
        }},
        {"type": "turn.completed", "usage": {
            "input_tokens": 12, "cached_input_tokens": 0, "output_tokens": 4,
        }},
    ]


def encoded(events):
    return "\n".join(json.dumps(event) for event in events).encode()


class CodexPlanConnectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REPOSITORY": "borovikov88/service2", "GITHUB_REF": "refs/heads/main",
            "GITHUB_ACTOR": "borovikov88", "GITHUB_TRIGGERING_ACTOR": "borovikov88",
            "GITHUB_SHA": "a" * 40, "CODEX_ACCESS_TOKEN": "test-plan-token-not-real",
            "SERVICE2_CODEX_BINARY": sys.executable,
        }

    def test_bad_context_or_missing_token_never_starts_child(self):
        changes = [
            ("CODEX_ACCESS_TOKEN", ""), ("CODEX_ACCESS_TOKEN", "sk-not-a-plan-token"),
            ("CODEX_ACCESS_TOKEN", " token "), ("GITHUB_REF", "refs/heads/untrusted"),
            ("GITHUB_ACTOR", "other"), ("GITHUB_TRIGGERING_ACTOR", "other"),
            ("GITHUB_REPOSITORY", "other/service2"), ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_SHA", "short"), ("GITHUB_ACTIONS", "false"),
        ]
        with patch.object(canary, "capture_process") as process:
            for key, value in changes:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(canary.ConnectionFailure):
                        canary.check_connection(dict(self.environment, **{key: value}))
            process.assert_not_called()

    def test_api_or_provider_override_never_starts_child(self):
        with patch.object(canary, "capture_process") as process:
            for key in (
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "AZURE_OPENAI_API_KEY",
                "CODEX_API_KEY", "CODEX_HOME", "CODEX_CONFIG", "CODEX_MODEL_PROVIDER", "CODEX_BASE_URL",
            ):
                with self.subTest(key=key):
                    with self.assertRaises(canary.ConnectionFailure):
                        canary.check_connection(dict(self.environment, **{key: ""}))
            process.assert_not_called()

    def test_fixed_prompt_clean_environment_ephemeral_auth_and_cleanup(self):
        observed = {}

        def fake_process(argv, environ, directory):
            observed.update(argv=argv, environ=environ, directory=directory)
            self.assertEqual(list(directory.iterdir()), [])
            self.assertEqual(set(environ), {"PATH", "TMPDIR", "CODEX_ACCESS_TOKEN"})
            self.assertNotIn(self.environment["CODEX_ACCESS_TOKEN"], " ".join(argv))
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(canary.EXPECTED)
            return encoded(valid_events())

        polluted = dict(self.environment, GH_TOKEN="secret-github", DEPLOY_SSH_KEY="secret-ssh",
                        HOME="/existing/home", HTTPS_PROXY="https://untrusted", LD_PRELOAD="bad")
        with patch.object(canary, "capture_process", side_effect=fake_process):
            self.assertEqual(canary.check_connection(polluted)["output_tokens"], 4)
        self.assertFalse(observed["directory"].exists())
        argv = observed["argv"]
        for flag in ("--strict-config", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
            self.assertIn(flag, argv)
        self.assertIn('cli_auth_credentials_store="ephemeral"', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("features.unified_exec=false", argv)
        self.assertIn("features.apply_patch_freeform=false", argv)
        self.assertIn("features.multi_agent=false", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "never")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-sol")

    def test_valid_result_and_private_reasoning(self):
        events = valid_events()
        events.insert(2, {"type": "item.completed", "item": {"type": "reasoning", "text": "private"}})
        self.assertEqual(canary.validate_result(encoded(events), canary.EXPECTED), {
            "input_tokens": 12, "cached_input_tokens": 0, "output_tokens": 4,
        })

    def test_unexpected_tool_events_or_malformed_results_fail_closed(self):
        invalid = [[], valid_events()[:-1], valid_events() + valid_events(),
                   [{"type": "error", "message": "private"}], [None]]
        for item_type in ("command_execution", "mcp_tool_call", "file_change", "web_search", "unknown"):
            events = valid_events()
            events.insert(2, {"type": "item.completed", "item": {"type": item_type, "text": "private"}})
            invalid.append(events)
        for change in ({"output_tokens": 0}, {"output_tokens": True}, {"input_tokens": -1},
                       {"cached_input_tokens": 100}, {"output_tokens": "4"}):
            events = valid_events()
            events[-1]["usage"].update(change)
            invalid.append(events)
        for events in invalid:
            with self.subTest(events=events):
                with self.assertRaises(canary.ConnectionFailure):
                    canary.validate_result(encoded(events), canary.EXPECTED)
        for raw in (b"not-json", b"\xff", b" " * (canary.MAX_OUTPUT_BYTES + 1)):
            with self.assertRaises((canary.ConnectionFailure, ValueError, UnicodeError)):
                canary.validate_result(raw, canary.EXPECTED)
        with self.assertRaises(canary.ConnectionFailure):
            canary.validate_result(encoded(valid_events()), "wrong-output")

    def test_error_output_never_exposes_credentials_or_model_text(self):
        output = io.StringIO()
        with patch.object(canary, "check_connection", side_effect=RuntimeError("secret-token model details")):
            with redirect_stdout(output):
                self.assertEqual(canary.main(), 1)
        self.assertEqual(output.getvalue(),
                         "CODEX_PLAN_CONNECTION_FAILED: credential, configuration or response was rejected.\n")

    def test_success_output_is_only_fixed_status_and_validated_usage(self):
        output = io.StringIO()
        with patch.object(canary, "check_connection", return_value={"output_tokens": 4}):
            with redirect_stdout(output):
                self.assertEqual(canary.main(), 0)
        self.assertNotIn("test-plan-token", output.getvalue())
        self.assertIn("no review or deployment was authorized", output.getvalue())

    def test_actual_subprocess_timeout_overflow_and_exit_fail_closed(self):
        # Local Python children exercise real pipe bounds and termination, with
        # no credentials or external requests; this does not run Codex.
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(canary.capture_process(
                [sys.executable, "-c", "print('ok')"], {"PATH": "/usr/bin:/bin"}, temporary), b"ok\n")
            for program in ("raise SystemExit(2)", "print('x' * 70000)", "import time; time.sleep(3)"):
                with self.subTest(program=program), patch.object(canary, "TIMEOUT_SECONDS", 0.2):
                    with self.assertRaises(canary.ConnectionFailure):
                        canary.capture_process([sys.executable, "-c", program], {"PATH": "/usr/bin:/bin"}, temporary)

    def test_workflow_is_manual_guarded_pinned_and_contains_only_plan_secret(self):
        workflow = (ROOT / ".github/workflows/codex-plan-connection.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        for forbidden in ("pull_request:", "push:", "schedule:", "SERVICE2_REVIEW_TOKEN", "OPENAI_API_KEY",
                          "DEPLOY_", "upload-artifact", "pull-requests: write", "contents: write"):
            self.assertNotIn(forbidden, workflow)
        for expected in ("github.actor == 'borovikov88'", "github.triggering_actor == 'borovikov88'",
                         "github.ref == 'refs/heads/main'", "github.repository == 'borovikov88/service2'",
                         "persist-credentials: false", "ref: ${{ github.sha }}", "environment: review"):
            self.assertIn(expected, workflow)
        self.assertEqual(workflow.count("secrets."), 1)
        self.assertIn("CODEX_ACCESS_TOKEN: ${{ secrets.CODEX_PLAN_ACCESS_TOKEN }}", workflow)
        self.assertIn("f479424eca092484dc40d87ae28c44f4cc40234a60045d6131e493800d814a30", workflow)
        self.assertLess(workflow.index("sha256sum --check"), workflow.index("secrets.CODEX_PLAN_ACCESS_TOKEN"))


if __name__ == "__main__":
    unittest.main()
