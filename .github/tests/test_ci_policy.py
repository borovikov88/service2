"""Selection is conservative; missing/failing workers can never turn the gate green."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_policy", ROOT / ".github/scripts/ci_policy.py")
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
HEAD, BASE = "a" * 40, "b" * 40


def needs(mode="full", reuse=False):
    selected = P.plan(mode, ["crm"] if mode == "ui" else [])
    outputs = {"ci_" + name: (str(value).lower() if isinstance(value, bool) else
               value if isinstance(value, str) else json.dumps(value)) for name, value in selected.items()}
    outputs["ci_reuse"] = str(reuse).lower()
    return {"scope": {"result": "success", "outputs": outputs}, "checks": {"result": "success"},
            **{job: {"result": "success" if selected[job] and not reuse else "skipped"}
               for job in ("server", "browser", "mysql")}}


class SelectionTests(unittest.TestCase):
    def test_only_explicit_non_executable_docs_are_lightweight(self):
        self.assertEqual(P.classify(["AGENTS.md", "docs/ai/deployment.md"])["mode"], "docs")
        for path in ("docs/fix.py", "docs/query.sql", "pool_service/templates/help.md", "README.py",
                     "requirements.txt", "pool_service/models.py", "pool_service/urls.py", "pool_service/services/call_access.py",
                     "pool_service/migrations/9999_change.py", ".github/workflows/ci-deploy.yml", "new/file.js", "../docs/x.md"):
            with self.subTest(path=path):
                self.assertEqual(P.classify(["docs/guide.md", path])["mode"], "full")
        self.assertEqual(P.classify([])["mode"], "full")

    def test_known_ui_includes_dependent_crm_avito_and_all_browser_groups(self):
        for path in ("pool_service/static/pool_service/crm_live/bulk.js",
                     "pool_service/templates/pool_service/crm_live/leads.html",
                     "pool_service/templates/pool_service/avito/workspace.html"):
            selected = P.classify([path, "docs/notes.md"])
            self.assertEqual(selected["mode"], "ui")
            self.assertEqual(selected["domains"], ["avito", "crm"])
            self.assertTrue(selected["server"])
            self.assertFalse(selected["mysql"])
            self.assertEqual(selected["browser_matrix"]["group"], list(P.BROWSER_GROUPS))
        for path in ("pool_service/static/pool_service/shared.css", "pool_service/templates/base.html",
                     "pool_service/templates/pool_service/crm_live/unsafe.py"):
            self.assertEqual(P.classify([path])["mode"], "full")

    def test_isolated_calculator_and_mixed_changes(self):
        self.assertEqual(P.classify(["tools/aqualine22-calculator/src/engine.mjs", "README.md"])["mode"], "calculator")
        self.assertEqual(P.classify(["tools/aqualine22-calculator/src/engine.mjs", "pool_service/models.py"])["mode"], "full")
        self.assertEqual(P.classify(["tools/aqualine22-calculator/src/engine.mjs",
                                    "pool_service/static/pool_service/crm_live/bulk.js"])["mode"], "full")

    def test_complete_exact_git_diff_for_pr_and_multi_commit_push(self):
        calls = []
        def read(*args):
            calls.append(args)
            return {"rev-parse": HEAD.encode(), "merge-base": b"", "diff": b"docs/a.md\0docs/b.md\0"}[args[0]]
        for event, payload in (("pull_request", {"pull_request": {"base": {"sha": BASE}}}), ("push", {"before": BASE})):
            selected = P.event_plan({"GITHUB_SHA": HEAD, "GITHUB_EVENT_NAME": event, "GITHUB_REF": "refs/heads/main"}, payload, read)
            self.assertEqual(selected["mode"], "docs")
            self.assertIn(("diff", "--no-renames", "--name-only", "-z", BASE, HEAD), calls)

    def test_unknown_or_unavailable_boundaries_and_manual_runs_are_full(self):
        env = {"GITHUB_SHA": HEAD, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}
        for payload in ({}, {"before": "0" * 40}, {"before": "bad"}):
            self.assertEqual(P.event_plan(env, payload, lambda *a: HEAD.encode())["mode"], "full")
        def denied(*args):
            raise subprocess.CalledProcessError(1, ["git"])
        self.assertEqual(P.event_plan(env, {"before": BASE}, denied)["mode"], "full")
        self.assertEqual(P.event_plan(env | {"GITHUB_EVENT_NAME": "workflow_dispatch"}, {}, lambda *a: HEAD.encode())["mode"], "full")
        # The old and new names are both considered, including moves out of docs.
        self.assertEqual(P.classify(["docs/old.md", "pool_service/new.py"])["mode"], "full")


class GateTests(unittest.TestCase):
    def test_all_expected_plans_and_exact_tree_reuse(self):
        for mode in ("full", "docs", "calculator", "ui"):
            for reuse in (False, True):
                self.assertEqual(P.validate_gate(needs(mode, reuse)), mode)

    def test_every_required_worker_and_policy_check_fails_closed(self):
        for mode in ("full", "docs", "calculator", "ui"):
            for job in P.JOBS:
                for state in ("failure", "cancelled", "skipped", "success", ""):
                    value = needs(mode)
                    if state == value[job]["result"]:
                        continue
                    value[job]["result"] = state
                    with self.subTest(mode=mode, job=job, state=state), self.assertRaises(ValueError):
                        P.validate_gate(value)

    def test_missing_evidence_and_partial_matrix_are_rejected(self):
        for job in P.JOBS:
            value = needs()
            value.pop(job)
            with self.assertRaises(ValueError):
                P.validate_gate(value)
        for key, value in (("ci_reuse", ""), ("ci_server", "false"), ("ci_mode", "unknown"),
                           ("ci_browser_matrix", '{"group":["shared"]}')):
            evidence = needs()
            evidence["scope"]["outputs"][key] = value
            with self.assertRaises(ValueError):
                P.validate_gate(evidence)


if __name__ == "__main__":
    unittest.main()
