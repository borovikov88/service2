"""Subscription boundary tests: no application database or external requests."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]


class SubscriptionDevelopmentTests(unittest.TestCase):
    def test_development_client_refuses_even_a_configured_api_key(self):
        # Execute the production function, isolated from ORM imports. A mocked
        # constructor catches a future accidental SDK fallback before I/O.
        path = ROOT / "pool_service/services/development_ai.py"
        tree = ast.parse(path.read_text())
        selected = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and node.name in {"AnalysisConfigurationError", "_client"}]
        constructor = Mock(side_effect=AssertionError("API client must not be constructed"))
        namespace = {"OpenAI": constructor, "settings": SimpleNamespace(
            OPENAI_API_KEY="configured-credential-not-real",
            OPENAI_DEVELOPMENT_TIMEOUT_SECONDS=1,
        )}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
        for retries in (0, 2):
            with self.subTest(retries=retries):
                with self.assertRaisesRegex(namespace["AnalysisConfigurationError"],
                                            "Separate development API billing is disabled"):
                    namespace["_client"](max_retries=retries)
        constructor.assert_not_called()

    def test_legacy_delivery_refuses_before_config_state_cache_or_network(self):
        path = ROOT / "pool_service/services/development_delivery.py"
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "publish_approval_and_enable_auto_merge")
        request, settings, task = Mock(), Mock(), Mock()
        namespace = {
            "DeliveryResult": lambda state, changed=False: SimpleNamespace(state=state, changed=changed),
            "_request": request, "settings": settings, "DevelopmentTask": task,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        for task_id in (1, 999999, None):
            result = namespace["publish_approval_and_enable_auto_merge"](task_id)
            self.assertEqual(result.state, "retired")
            self.assertFalse(result.changed)
        request.assert_not_called()
        self.assertEqual(task.mock_calls, [])
        self.assertEqual(settings.mock_calls, [])

    def test_independent_backend_review_uses_the_same_blocked_client(self):
        tree = ast.parse((ROOT / "pool_service/services/development_review.py").read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertTrue(any(node.module == "pool_service.services.development_ai"
                            and any(item.name == "_client" for item in node.names)
                            for node in imports))
        self.assertFalse(any(node.module == "openai" for node in imports))

    def test_workflows_never_supply_api_credentials_or_api_codex_action(self):
        workflows = list((ROOT / ".github/workflows").glob("*.y*ml"))
        self.assertTrue(workflows)
        for path in workflows:
            with self.subTest(workflow=path.name):
                text = path.read_text()
                # Canary checks for an absent API key are intentionally allowed.
                self.assertNotRegex(text, r"secrets\s*(?:\.\s*OPENAI_API_KEY|\[\s*['\"]OPENAI_API_KEY)")
                self.assertNotRegex(text, r"(?mi)^\s*openai-api-key\s*:")
                self.assertNotRegex(text, r"(?mi)^\s*(?:-\s*)?uses:\s*openai/codex-action@")
                self.assertNotIn("api.openai.com", text)

    def test_legacy_cli_cannot_be_called_by_workflow(self):
        for path in (ROOT / ".github/workflows").glob("*.y*ml"):
            self.assertNotRegex(path.read_text(), r"review_direct_pr\.py\s+(review|publish)")


if __name__ == "__main__":
    unittest.main()
