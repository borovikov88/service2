import contextlib
import hashlib
import importlib.util
import io
import json
import re
import ssl
import subprocess
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
    Organization,
    OrganizationAccess,
)
from pool_service.services import development_codex


CODEX_SETTINGS = override_settings(
    GITHUB_DEVELOPMENT_TOKEN="github-test-token-never-sent",
    GITHUB_DEVELOPMENT_REPOSITORY="borovikov88/service2",
    GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml",
    GITHUB_DEVELOPMENT_TIMEOUT_SECONDS=3,
    GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=40000,
)


def load_patch_validator():
    path = Path(settings.BASE_DIR) / ".github/scripts/validate_codex_patch.py"
    spec = importlib.util.spec_from_file_location("validate_codex_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_usage_builder():
    path = Path(settings.BASE_DIR) / ".github/scripts/build_codex_usage.py"
    spec = importlib.util.spec_from_file_location("build_codex_usage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow_trigger_names(workflow_text):
    """Return top-level workflow event names from a canonical ``on`` mapping."""
    lines = workflow_text.splitlines()
    try:
        on_index = lines.index("on:")
    except ValueError as exc:
        raise AssertionError("Workflow must use an explicit top-level on mapping") from exc

    triggers = []
    for line in lines[on_index + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip())
        if indentation == 0:
            break
        if indentation != 2:
            continue
        match = re.fullmatch(r"  ([A-Za-z_][A-Za-z0-9_-]*):(?:\s.*)?", line)
        if match is None:
            raise AssertionError("Workflow trigger mapping has an unsupported shape")
        triggers.append(match.group(1))
    return triggers


def artifact_files(*, result="no_changes", patch_content=b"", final=b"Codex summary"):
    title = b"Safe pull request title"
    usage = {
        "schema_version": 1,
        "task_reference": "DEV-0002",
        "launch_token": "launch-token",
        "branch_name": "codex/dev-2-123456789abc",
        "workflow_run_id": 501,
        "model": "gpt-5.6-sol",
        "input_tokens": 1000,
        "cached_input_tokens": 100,
        "output_tokens": 200,
        "usage_source": "codex_exec_jsonl_turn_completed",
    }
    usage_content = json.dumps(usage, sort_keys=True).encode("utf-8")
    manifest = {
        "task_reference": "DEV-0002",
        "launch_token": "launch-token",
        "branch_name": "codex/dev-2-123456789abc",
        "workflow_run_id": 501,
        "model": "gpt-5.6-sol",
        "result": result,
        "patch_sha256": hashlib.sha256(patch_content).hexdigest(),
        "patch_size": len(patch_content),
        "final_sha256": hashlib.sha256(final).hexdigest(),
        "final_size": len(final),
        "title_sha256": hashlib.sha256(title).hexdigest(),
        "title_size": len(title),
        "usage_sha256": hashlib.sha256(usage_content).hexdigest(),
        "usage_size": len(usage_content),
    }
    return {
        "codex.patch": patch_content,
        "codex-final.txt": final,
        "codex-usage.json": usage_content,
        "manifest.json": json.dumps(manifest, sort_keys=True).encode("utf-8"),
        "pr-title.txt": title,
    }


def artifact_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in files.items():
            zipped.writestr(name, content)
    return buffer.getvalue()


def validator_argv(artifact_dir):
    return [
        "validate_codex_patch.py",
        "--artifact-dir", str(artifact_dir),
        "--task-reference", "DEV-0002",
        "--launch-token", "launch-token",
        "--branch-name", "codex/dev-2-123456789abc",
        "--workflow-run-id", "501",
        "--model", "gpt-5.6-sol",
    ]


class CodexTestMixin:
    def make_user(self, username, role, organization=None):
        user = User.objects.create_user(username, password="test-password")
        OrganizationAccess.objects.create(
            user=user,
            organization=organization or self.organization,
            role=role,
        )
        return user

    def make_ready_task(self, organization=None):
        task = DevelopmentTask.objects.create(
            organization=organization or self.organization,
            initiator=self.owner,
            title="Автоматизировать безопасный workflow",
            description="Добавить передачу задачи в Codex.",
            business_goal="Создавать проверяемый Pull Request.",
            definition_of_done="Workflow безопасен, тесты проходят.",
            priority=DevelopmentTask.PRIORITY_HIGH,
            status=DevelopmentTask.STATUS_READY_FOR_CODEX,
            current_stage=DevelopmentTask.STAGE_DEVELOPMENT,
        )
        DevelopmentIteration.objects.create(
            task=task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            status=DevelopmentIteration.STATUS_ACCEPTED,
            prompt="Первичный prompt",
            response="Проверенный первичный анализ и технический план.",
            result_summary="Анализ завершён.",
            automation_metadata={"purpose": "primary_analysis", "applied": True},
        )
        return task

    def start_url(self, task):
        return reverse("development_task_codex_start", args=[task.pk])

    def check_url(self, task):
        return reverse("development_task_codex_check", args=[task.pk])

    def matching_run(self, task, iteration, *, status="in_progress", conclusion=None):
        token = iteration.automation_metadata["launch_token"]
        return {
            "id": 501,
            "display_title": f"development-{task.reference}-{token}",
            "head_branch": "main",
            "status": status,
            "conclusion": conclusion,
            "html_url": "https://github.com/borovikov88/service2/actions/runs/501",
        }

    def usage_artifact(self, iteration, *, result="changes", input_tokens=1000,
                       cached_input_tokens=100, output_tokens=200):
        metadata = iteration.automation_metadata
        return {
            "summary": "Trusted Codex summary",
            "artifact_id": 901,
            "result": result,
            "usage": {
                "model": metadata["effective_model"],
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "usage_source": "codex_exec_jsonl_turn_completed",
                "workflow_run_id": 501,
                "launch_token": metadata["launch_token"],
            },
        }


@CODEX_SETTINGS
class DevelopmentCodexAutomationTests(CodexTestMixin, TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Codex org", paid_until=timezone.now() + timedelta(days=30)
        )
        self.owner = self.make_user("codex-owner", "owner")
        self.admin = self.make_user("codex-admin", "admin")
        self.manager = self.make_user("codex-manager", "manager")
        self.artifact_lookup_patcher = patch(
            "pool_service.services.development_codex._codex_artifact",
            return_value=None,
        )
        self.artifact_lookup = self.artifact_lookup_patcher.start()
        self.addCleanup(self.artifact_lookup_patcher.stop)

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_dispatch_creates_separate_codex_iteration_and_structured_prompt(self, dispatch):
        task = self.make_ready_task()

        result = development_codex.dispatch_codex(task.pk, self.owner.pk)

        task.refresh_from_db()
        self.assertEqual(result.state, development_codex.STATE_DISPATCHED)
        self.assertIs(
            task.automation_metadata[development_codex.AUTO_CYCLE_METADATA_KEY],
            True,
        )
        self.assertEqual(task.iterations.count(), 2)
        iteration = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_CODEX)
        self.assertEqual(iteration.automation_metadata["purpose"], development_codex.PURPOSE)
        self.assertEqual(iteration.automation_metadata["provider"], development_codex.PROVIDER)
        self.assertIn(task.reference, iteration.prompt)
        self.assertIn(task.business_goal, iteration.prompt)
        self.assertIn("первичный анализ", iteration.prompt.lower())
        self.assertIn("Не выполняй deploy", iteration.prompt)
        self.assertEqual(
            iteration.automation_metadata["prompt_bytes"],
            len(iteration.prompt.encode("utf-8")),
        )
        self.assertEqual(iteration.automation_metadata["prompt_limit_bytes"], 40000)
        self.assertFalse(iteration.automation_metadata["prompt_truncated"])
        self.assertEqual(iteration.automation_metadata["truncated_sections"], [])
        payload = dispatch.call_args.args[0]
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["branch_name"], iteration.automation_metadata["branch_name"])
        self.assertNotIn(settings.GITHUB_DEVELOPMENT_TOKEN, str(payload))
        self.assertNotIn(settings.GITHUB_DEVELOPMENT_TOKEN, str(iteration.automation_metadata))

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_confirmed_dispatch_advances_task_and_writes_audit_event(self, dispatch):
        task = self.make_ready_task()

        development_codex.dispatch_codex(task.pk, self.admin.pk)

        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_CODEX_WORKING)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_DEVELOPMENT)
        event = task.events.get(metadata__action="codex_dispatched")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.metadata["old_status"], DevelopmentTask.STATUS_READY_FOR_CODEX)

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_double_submit_creates_one_iteration_and_one_dispatch(self, dispatch):
        task = self.make_ready_task()

        first = development_codex.dispatch_codex(task.pk, self.owner.pk)
        second = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(task.iterations.filter(executor_type="codex").count(), 1)
        task.refresh_from_db()
        self.assertIs(
            task.automation_metadata[development_codex.AUTO_CYCLE_METADATA_KEY],
            True,
        )

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_uncertain_dispatch_is_not_retried_and_becomes_recoverable(self, dispatch):
        dispatch.side_effect = TimeoutError("transport uncertainty")
        task = self.make_ready_task()

        first = development_codex.dispatch_codex(task.pk, self.owner.pk)
        second = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(first.state, development_codex.STATE_DISPATCH_UNKNOWN)
        self.assertFalse(second.changed)
        self.assertEqual(dispatch.call_count, 1)
        task.refresh_from_db()
        iteration = task.iterations.get(executor_type="codex")
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertIs(
            task.automation_metadata[development_codex.AUTO_CYCLE_METADATA_KEY],
            True,
        )
        self.assertEqual(iteration.automation_metadata["state"], "dispatch_unknown")
        self.assertNotIn("transport uncertainty", iteration.technical_errors)

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_invalid_task_state_never_dispatches(self, dispatch):
        task = self.make_ready_task()
        task.status = DevelopmentTask.STATUS_REVIEW
        task.save(update_fields=["status", "updated_at"])

        result = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "not_available")
        dispatch.assert_not_called()

    @override_settings(GITHUB_DEVELOPMENT_REPOSITORY="../service2")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_invalid_server_repository_is_rejected_before_dispatch(self, dispatch):
        task = self.make_ready_task()

        with self.assertRaises(development_codex.CodexConfigurationError):
            development_codex.dispatch_codex(task.pk, self.owner.pk)

        dispatch.assert_not_called()
        self.assertFalse(task.iterations.filter(executor_type="codex").exists())

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_manual_state_change_during_dispatch_is_not_overwritten(self, dispatch):
        task = self.make_ready_task()

        def change_task_state(_payload):
            DevelopmentTask.objects.filter(pk=task.pk).update(
                status=DevelopmentTask.STATUS_CANCELLED
            )

        dispatch.side_effect = change_task_state

        result = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "task_state_changed")
        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_CANCELLED)
        self.assertTrue(
            task.events.filter(metadata__action="codex_dispatched_task_state_changed").exists()
        )

    def test_short_prompt_is_unchanged_at_exact_byte_boundary(self):
        task = self.make_ready_task()
        analysis = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_SYSTEM)
        original = development_codex.build_codex_prompt(task, analysis)
        exact_limit = len(original.encode("utf-8"))

        with override_settings(GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=exact_limit):
            built = development_codex._build_codex_prompt(task, analysis)

        self.assertEqual(built.prompt, original)
        self.assertEqual(built.prompt_bytes, exact_limit)
        self.assertFalse(built.truncated)
        self.assertEqual(built.truncated_sections, ())

    @override_settings(GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=5000)
    def test_oversized_prompt_is_budgeted_with_utf8_safe_marker(self):
        task = self.make_ready_task()
        task.description = "Подробное описание задачи. " * 500
        analysis = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_SYSTEM)
        analysis.response = "Технический анализ с кириллицей. " * 1000

        built = development_codex._build_codex_prompt(task, analysis)

        self.assertLessEqual(len(built.prompt.encode("utf-8")), 5000)
        self.assertEqual(built.prompt.encode("utf-8").decode("utf-8"), built.prompt)
        self.assertIn(development_codex.PROMPT_TRUNCATION_MARKER, built.prompt)
        self.assertTrue(built.truncated)

    @override_settings(GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=6000)
    def test_analysis_is_truncated_before_description_and_definition_of_done(self):
        task = self.make_ready_task()
        task.description = "Критически важное исходное описание. " * 20
        task.definition_of_done = "Проверяемый критерий готовности. " * 20
        analysis = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_SYSTEM)
        analysis.response = "Избыточный результат анализа. " * 2000

        built = development_codex._build_codex_prompt(task, analysis)

        self.assertEqual(built.truncated_sections, ("analysis",))
        self.assertIn(task.description, built.prompt)
        self.assertIn(task.definition_of_done, built.prompt)
        self.assertIn(development_codex.PROMPT_TRUNCATION_MARKER, built.prompt)

    @override_settings(GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=5000)
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_successful_budgeting_saves_only_prompt_metadata(self, dispatch):
        task = self.make_ready_task()
        original_description = "Исходный пользовательский текст. " * 400
        task.description = original_description
        task.save(update_fields=["description", "updated_at"])
        analysis = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_SYSTEM)
        analysis.response = "Большой результат AI-анализа. " * 1000
        analysis.save(update_fields=["response", "updated_at"])

        result = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, development_codex.STATE_DISPATCHED)
        iteration = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_CODEX)
        metadata = iteration.automation_metadata
        self.assertLessEqual(metadata["prompt_bytes"], metadata["prompt_limit_bytes"])
        self.assertTrue(metadata["prompt_truncated"])
        self.assertIn("analysis", metadata["truncated_sections"])
        task.refresh_from_db()
        self.assertEqual(task.description, original_description)
        dispatch.assert_called_once()

    @override_settings(GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=1000)
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_codex.uuid4")
    def test_impossible_prompt_returns_safe_state_without_mutation(self, uuid4, dispatch):
        task = self.make_ready_task()
        task.description = "я" * 2000
        task.save(update_fields=["description", "updated_at"])
        before = {
            "status": task.status,
            "stage": task.current_stage,
            "metadata": task.automation_metadata,
            "iterations": task.iterations.count(),
            "events": task.events.count(),
        }

        result = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "prompt_too_large")
        self.assertFalse(result.changed)
        dispatch.assert_not_called()
        uuid4.assert_not_called()
        self.assertFalse(task.iterations.filter(executor_type="codex").exists())
        task.refresh_from_db()
        self.assertEqual(task.status, before["status"])
        self.assertEqual(task.current_stage, before["stage"])
        self.assertEqual(task.automation_metadata, before["metadata"])
        self.assertEqual(task.iterations.count(), before["iterations"])
        self.assertEqual(task.events.count(), before["events"])
        self.assertNotIn("codex_launch_token", task.automation_metadata)
        self.assertNotIn("codex_branch_name", task.automation_metadata)

    @override_settings(GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=1000)
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_prompt_too_large_view_redirects_with_message_instead_of_500(self, dispatch):
        task = self.make_ready_task()
        task.description = "я" * 2000
        task.save(update_fields=["description", "updated_at"])
        self.client.force_login(self.owner)

        response = self.client.post(self.start_url(task), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Не удалось безопасно сформировать prompt Codex в пределах допустимого размера.",
        )
        dispatch.assert_not_called()
        self.assertFalse(task.iterations.filter(executor_type="codex").exists())

    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_pending_check_keeps_task_and_iteration_working(self, dispatch, runs):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {"workflow_runs": [self.matching_run(task, iteration)]}

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "in_progress")
        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_CODEX_WORKING)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_WORKING)
        self.assertEqual(iteration.automation_metadata["workflow_run_id"], 501)

        self.client.force_login(self.owner)
        response = self.client.get(reverse("development_task_detail", args=[task.pk]))
        self.assertContains(response, "Открыть запуск GitHub Actions")
        self.assertContains(response, "Run #501")
        self.assertContains(
            response,
            'href="https://github.com/borovikov88/service2/actions/runs/501"',
        )
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, 'rel="noopener noreferrer"')

    @patch("pool_service.services.development_codex._workflow_validation_state")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_run_id_is_saved_before_outcome_lookup_failure(
        self, dispatch, runs, validation
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration, status="completed", conclusion="failure")
            ]
        }
        validation.side_effect = TimeoutError("temporary GitHub failure")

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "check_failed")
        iteration.refresh_from_db()
        self.assertEqual(iteration.automation_metadata["workflow_run_id"], 501)

    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_ambiguous_runs_do_not_save_run_id(self, dispatch, runs):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration),
                {**self.matching_run(task, iteration), "id": 502},
            ]
        }

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "not_found")
        iteration.refresh_from_db()
        self.assertNotIn("workflow_run_id", iteration.automation_metadata)

    def test_run_link_ignores_metadata_url_and_rejects_invalid_run_ids(self):
        task = self.make_ready_task()
        iteration = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=2,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_FAILED,
            automation_metadata={
                "purpose": development_codex.PURPOSE,
                "state": development_codex.STATE_FAILED,
                "applied": True,
                "workflow_run_id": True,
                "workflow_run_url": "javascript:alert(1)",
            },
        )
        task.automation_metadata = {"active_codex_iteration_id": iteration.pk}
        task.save(update_fields=["automation_metadata", "updated_at"])
        self.client.force_login(self.owner)

        response = self.client.get(reverse("development_task_detail", args=[task.pk]))

        self.assertNotContains(response, "Открыть запуск GitHub Actions")
        self.assertNotContains(response, "Run #")
        self.assertNotContains(response, "javascript:alert(1)")

    @override_settings(GITHUB_DEVELOPMENT_REPOSITORY="attacker/repo/actions/runs/9")
    def test_run_url_requires_valid_server_repository(self):
        self.assertEqual(development_codex.github_actions_run_url(501), "")
        self.assertEqual(development_codex.github_actions_run_url("not-a-number"), "")

    @patch("pool_service.services.development_codex._workflow_validation_state", return_value="passed")
    @patch("pool_service.services.development_codex._pull_request_files")
    @patch("pool_service.services.development_codex._find_pull_request")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_success_with_pull_request_advances_to_review(
        self, dispatch, runs, find_pr, pr_files, validation
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [self.matching_run(task, iteration, status="completed", conclusion="success")]
        }
        find_pr.return_value = {
            "number": 17,
            "title": f"[{task.reference}] {task.title}",
            "body": "Codex summary. Tests passed.\n<!-- codex-tests: passed -->",
            "html_url": "https://github.com/borovikov88/service2/pull/17",
        }
        pr_files.return_value = ["pool_service/development_views.py", "pool_service/tests/test_x.py"]
        self.artifact_lookup.return_value = self.usage_artifact(iteration)

        result = development_codex.check_codex(task.pk, self.admin.pk)

        self.assertEqual(result.state, "completed")
        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_REVIEW)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_REVIEW)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_ACCEPTED)
        self.assertEqual(iteration.automation_metadata["pr_number"], 17)
        usage = iteration.automation_metadata["ai_usage"]
        self.assertEqual(usage["stage"], "codex")
        self.assertEqual(len(usage["calls"]), 1)
        self.assertEqual(usage["calls"][0]["workflow_run_id"], 501)
        self.assertIsNotNone(usage["calls"][0]["calculated_cost_usd"])
        self.assertIn("development_views.py", iteration.changed_files)
        self.assertIn("прошли успешно", iteration.test_result)
        self.assertTrue(task.events.filter(metadata__action="codex_completed").exists())

    @patch("pool_service.services.development_codex._workflow_validation_state", return_value="no_changes")
    @patch("pool_service.services.development_codex._pull_request_files")
    @patch("pool_service.services.development_codex._find_pull_request")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_no_changes_advances_to_review_without_pull_request_metadata(
        self, dispatch, runs, find_pr, pr_files, validation
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration, status="completed", conclusion="success")
            ]
        }
        self.artifact_lookup.return_value = {
            "summary": "Codex verified that the implementation is already complete.",
            "artifact_id": 901,
            "result": "no_changes",
            "usage": {
                "model": "gpt-5.6-sol",
                "input_tokens": 1000,
                "cached_input_tokens": 100,
                "output_tokens": 200,
                "usage_source": "codex_exec_jsonl_turn_completed",
                "workflow_run_id": 501,
                "launch_token": iteration.automation_metadata["launch_token"],
            },
        }

        first = development_codex.check_codex(task.pk, self.admin.pk)
        event_count = task.events.count()
        second = development_codex.check_codex(task.pk, self.admin.pk)

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(first.state, development_codex.STATE_NO_CHANGES)
        self.assertTrue(first.changed)
        self.assertEqual(second.state, development_codex.STATE_NO_CHANGES)
        self.assertFalse(second.changed)
        self.assertEqual(task.status, DevelopmentTask.STATUS_REVIEW)
        self.assertEqual(len(iteration.automation_metadata["ai_usage"]["calls"]), 1)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_REVIEW)
        self.assertEqual(
            task.current_activity,
            "Codex не предложил изменений; требуется проверка результата",
        )
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_ACCEPTED)
        self.assertEqual(
            iteration.response,
            "Codex verified that the implementation is already complete.",
        )
        self.assertEqual(iteration.changed_files, "")
        self.assertEqual(iteration.automation_metadata["artifact_id"], 901)
        self.assertEqual(self.artifact_lookup.call_count, 1)
        self.assertNotIn("pr_number", iteration.automation_metadata)
        self.assertNotIn("pr_url", iteration.automation_metadata)
        self.assertEqual(task.events.count(), event_count)
        self.assertTrue(task.events.filter(metadata__action="codex_no_changes").exists())
        self.assertEqual(runs.call_count, 1)
        find_pr.assert_not_called()
        pr_files.assert_not_called()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("development_task_detail", args=[task.pk]))
        self.assertContains(
            response,
            "Codex не предложил изменений; результат ожидает проверки",
        )

    @patch(
        "pool_service.services.development_codex._workflow_validation_state",
        return_value="infrastructure_failed",
    )
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_failed_run_blocks_task(self, dispatch, runs, validation):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [self.matching_run(task, iteration, status="completed", conclusion="failure")]
        }

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "infrastructure_failed")
        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_FAILED)
        self.assertNotIn("ai_usage", iteration.automation_metadata)

    @patch(
        "pool_service.services.development_codex._workflow_validation_state",
        return_value="passed",
    )
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_publish_failure_still_records_actual_usage(self, dispatch, runs, validation):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration, status="completed", conclusion="failure")
            ]
        }
        self.artifact_lookup.return_value = self.usage_artifact(iteration)

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "infrastructure_failed")
        iteration.refresh_from_db()
        self.assertEqual(len(iteration.automation_metadata["ai_usage"]["calls"]), 1)

    @patch(
        "pool_service.services.development_codex._workflow_validation_state",
        return_value="infrastructure_failed",
    )
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_cancelled_and_timed_out_runs_block_task(self, dispatch, runs, validation):
        for index, conclusion in enumerate(("cancelled", "timed_out"), start=1):
            with self.subTest(conclusion=conclusion):
                task = self.make_ready_task()
                development_codex.dispatch_codex(task.pk, self.owner.pk)
                iteration = task.iterations.get(executor_type="codex")
                runs.return_value = {
                    "workflow_runs": [
                        self.matching_run(task, iteration, status="completed", conclusion=conclusion)
                    ]
                }

                result = development_codex.check_codex(task.pk, self.owner.pk)

                self.assertEqual(result.state, conclusion)
                task.refresh_from_db()
                self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)

    @patch("pool_service.services.development_codex._workflow_validation_state", return_value="failed")
    @patch("pool_service.services.development_codex._pull_request_files")
    @patch("pool_service.services.development_codex._find_pull_request")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_failed_validation_preserves_diagnostic_pr_and_blocks_task(
        self, dispatch, runs, find_pr, files, validation
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration, status="completed", conclusion="failure")
            ]
        }
        find_pr.return_value = {
            "number": 19,
            "title": "Diagnostic PR",
            "body": "Diagnostics\n<!-- codex-tests: failed -->",
            "html_url": "https://github.com/borovikov88/service2/pull/19",
        }
        files.return_value = ["pool_service/development_views.py"]
        self.artifact_lookup.return_value = self.usage_artifact(iteration)

        first = development_codex.check_codex(task.pk, self.admin.pk)
        event_count = task.events.count()
        second = development_codex.check_codex(task.pk, self.admin.pk)

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(first.state, "validation_failed")
        self.assertFalse(second.changed)
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_DEVELOPMENT)
        self.assertEqual(
            task.current_activity, "Codex создал изменения, но проверки не прошли"
        )
        self.assertEqual(iteration.automation_metadata["pr_number"], 19)
        self.assertEqual(iteration.automation_metadata["validation_state"], "failed")
        self.assertEqual(len(iteration.automation_metadata["ai_usage"]["calls"]), 1)
        self.assertIn("проверки", iteration.technical_errors.lower())
        self.assertEqual(task.events.count(), event_count)
        self.assertEqual(runs.call_count, 1)

    @patch(
        "pool_service.services.development_codex._workflow_validation_state",
        return_value="security_blocked",
    )
    @patch("pool_service.services.development_codex._find_pull_request")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_security_violation_blocks_without_pr_lookup(
        self, dispatch, runs, find_pr, validation
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration, status="completed", conclusion="failure")
            ]
        }

        result = development_codex.check_codex(task.pk, self.owner.pk)

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(result.state, "security_blocked")
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertIn("защищённые файлы", task.blockers)
        self.assertNotIn("pr_number", iteration.automation_metadata)
        find_pr.assert_not_called()

    @patch("pool_service.services.development_codex._workflow_validation_state", return_value="passed")
    @patch("pool_service.services.development_codex._pull_request_files")
    @patch("pool_service.services.development_codex._find_pull_request")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_repeated_completed_check_is_idempotent(
        self, dispatch, runs, find_pr, files, validation
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [self.matching_run(task, iteration, status="completed", conclusion="success")]
        }
        find_pr.return_value = {
            "number": 18,
            "title": "Safe PR",
            "body": "Done",
            "html_url": "https://github.com/borovikov88/service2/pull/18",
        }
        files.return_value = []

        first = development_codex.check_codex(task.pk, self.owner.pk)
        event_count = task.events.count()
        second = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(task.events.count(), event_count)
        self.assertEqual(runs.call_count, 1)

    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_run_for_other_launch_token_is_ignored(self, dispatch, runs):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        wrong = self.matching_run(task, iteration)
        wrong["display_title"] = f"development-{task.reference}-other-token"
        runs.return_value = {"workflow_runs": [wrong]}

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "not_found")
        iteration.refresh_from_db()
        self.assertNotIn("workflow_run_id", iteration.automation_metadata)

    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_stale_task_state_is_not_overwritten(self, dispatch, runs):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        task.status = DevelopmentTask.STATUS_DONE
        task.save(update_fields=["status", "updated_at"])
        runs.return_value = {"workflow_runs": [self.matching_run(task, iteration)]}

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "task_state_changed")
        runs.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_DONE)

    @patch("pool_service.services.development_codex._pull_request_files", return_value=[])
    @patch("pool_service.services.development_codex._find_pull_request")
    @patch("pool_service.services.development_codex._workflow_validation_state")
    @patch("pool_service.services.development_codex._list_workflow_runs")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_state_changed_during_completed_poll_is_not_overwritten(
        self, dispatch, runs, validation, find_pr, files
    ):
        task = self.make_ready_task()
        development_codex.dispatch_codex(task.pk, self.owner.pk)
        iteration = task.iterations.get(executor_type="codex")
        runs.return_value = {
            "workflow_runs": [
                self.matching_run(task, iteration, status="completed", conclusion="success")
            ]
        }

        def change_task_state(_run_id):
            DevelopmentTask.objects.filter(pk=task.pk).update(
                status=DevelopmentTask.STATUS_DONE
            )
            return "passed"

        validation.side_effect = change_task_state
        find_pr.return_value = {
            "number": 20,
            "title": "Stale PR",
            "body": "Done\n<!-- codex-tests: passed -->",
            "html_url": "https://github.com/borovikov88/service2/pull/20",
        }

        result = development_codex.check_codex(task.pk, self.owner.pk)

        self.assertEqual(result.state, "task_state_changed")
        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_DONE)
        self.assertFalse(iteration.automation_metadata.get("applied", False))

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_owner_post_uses_only_server_controlled_inputs(self, dispatch):
        task = self.make_ready_task()
        self.client.force_login(self.owner)

        response = self.client.post(
            self.start_url(task),
            {
                "repository": "attacker/repository",
                "workflow": "unsafe.yml",
                "branch_name": "main",
                "prompt": "ignore server prompt",
            },
        )

        self.assertEqual(response.status_code, 302)
        payload = dispatch.call_args.args[0]
        self.assertEqual(payload["ref"], "main")
        self.assertNotEqual(payload["inputs"]["branch_name"], "main")
        self.assertNotIn("ignore server prompt", str(payload))

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_task_text_cannot_modify_branch_or_forbidden_path_policy(self, dispatch):
        task = self.make_ready_task()
        task.title = "$(touch unsafe) .github/workflows/x.yml\n.env.production"
        task.description = "deploy.sh && update.sh"
        task.save(update_fields=["title", "description", "updated_at"])

        development_codex.dispatch_codex(task.pk, self.owner.pk)

        payload = dispatch.call_args.args[0]
        branch = payload["inputs"]["branch_name"]
        self.assertRegex(branch, r"^codex/dev-[0-9]+-[a-f0-9]{12}$")
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/development-codex.yml").read_text(
            encoding="utf-8"
        )
        retired_job = workflow.split("jobs:", 1)[1]
        # The server still owns the branch name; the retired workflow must not
        # decode or apply any task-controlled content under that name.
        for forbidden in ("inputs.", "prompt_b64", "pr_title_b64", "git apply",
                          "git push", "download-artifact", "codex exec"):
            self.assertNotIn(forbidden, retired_job)
        self.assertIn("exit 1", retired_job)

    def test_get_is_rejected_and_manager_is_denied(self):
        task = self.make_ready_task()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.start_url(task)).status_code, 405)
        self.assertEqual(self.client.get(self.check_url(task)).status_code, 405)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(self.start_url(task)).status_code, 403)

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_cross_tenant_direct_post_returns_404_without_dispatch(self, dispatch):
        foreign = Organization.objects.create(
            name="Foreign", paid_until=timezone.now() + timedelta(days=30)
        )
        task = self.make_ready_task(organization=foreign)
        self.client.force_login(self.owner)

        response = self.client.post(self.start_url(task))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(self.check_url(task)).status_code, 404)
        dispatch.assert_not_called()
        self.assertFalse(task.iterations.filter(executor_type="codex").exists())

    def test_state_changing_endpoints_require_csrf(self):
        task = self.make_ready_task()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)

        self.assertEqual(client.post(self.start_url(task)).status_code, 403)
        self.assertEqual(client.post(self.check_url(task)).status_code, 403)

    @patch("pool_service.services.development_codex.urlopen")
    def test_github_dispatch_uses_short_timeout_and_header_only_token(self, urlopen):
        response = MagicMock()
        response.status = 204
        response.read.return_value = b""
        urlopen.return_value.__enter__.return_value = response

        development_codex._dispatch_workflow({"ref": "main", "inputs": {}})

        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)
        context = urlopen.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer github-test-token-never-sent",
        )
        self.assertNotIn(b"github-test-token-never-sent", request.data)

    @patch("pool_service.services.development_codex.ssl.create_default_context")
    @patch("pool_service.services.development_codex.certifi.where")
    def test_github_ssl_context_uses_certifi_ca_bundle(self, certifi_where, create_context):
        certifi_where.return_value = "trusted-certifi-ca.pem"
        expected = MagicMock()
        create_context.return_value = expected

        context = development_codex._github_ssl_context()

        self.assertIs(context, expected)
        certifi_where.assert_called_once_with()
        create_context.assert_called_once_with(cafile="trusted-certifi-ca.pem")

    def test_github_ssl_context_requires_certificates_and_hostname_verification(self):
        context = development_codex._github_ssl_context()

        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    @patch("pool_service.services.development_codex.urlopen")
    def test_successful_github_get_uses_verified_context(self, urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"state": "active"}'
        urlopen.return_value.__enter__.return_value = response

        result = development_codex._github_request("GET", "/repos/borovikov88/service2")

        self.assertEqual(result, {"state": "active"})
        context = urlopen.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    @patch("pool_service.services.development_codex.urlopen")
    def test_http_error_exposes_only_safe_status_diagnostics(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://api.github.com/repos/borovikov88/service2?private_query=do-not-log",
            403,
            "forbidden",
            {},
            io.BytesIO(b"private response body github-test-token-never-sent"),
        )

        with self.assertLogs(development_codex.logger.name, level="WARNING") as logs:
            with self.assertRaises(development_codex.GitHubRequestError) as caught:
                development_codex._github_request(
                    "GET", "/repos/borovikov88/service2?private_query=do-not-log"
                )

        error = caught.exception
        self.assertEqual(error.category, "http")
        self.assertEqual(error.status_code, 403)
        self.assertEqual(error.cause_type, "HTTPError")
        diagnostics = " ".join(logs.output) + " " + str(error)
        self.assertNotIn("github-test-token-never-sent", diagnostics)
        self.assertNotIn("private response body", diagnostics)
        self.assertNotIn("private_query", diagnostics)

    @patch("pool_service.services.development_codex.urlopen")
    def test_ssl_failure_is_safe_transport_error(self, urlopen):
        urlopen.side_effect = URLError(
            ssl.SSLCertVerificationError("github-test-token-never-sent")
        )

        with self.assertLogs(development_codex.logger.name, level="WARNING") as logs:
            with self.assertRaises(development_codex.GitHubRequestError) as caught:
                development_codex._github_request("GET", "/repos/borovikov88/service2")

        error = caught.exception
        self.assertEqual(error.category, "transport")
        self.assertIsNone(error.status_code)
        self.assertEqual(error.cause_type, "SSLCertVerificationError")
        diagnostics = " ".join(logs.output) + " " + str(error)
        self.assertNotIn("github-test-token-never-sent", diagnostics)

    @patch("pool_service.services.development_codex.urlopen")
    def test_timeout_is_safe_transport_error(self, urlopen):
        urlopen.side_effect = TimeoutError("github-test-token-never-sent")

        with self.assertLogs(development_codex.logger.name, level="WARNING") as logs:
            with self.assertRaises(development_codex.GitHubRequestError) as caught:
                development_codex._github_request("GET", "/repos/borovikov88/service2")

        error = caught.exception
        self.assertEqual(error.category, "transport")
        self.assertEqual(error.cause_type, "TimeoutError")
        diagnostics = " ".join(logs.output) + " " + str(error)
        self.assertNotIn("github-test-token-never-sent", diagnostics)

    @patch("pool_service.services.development_codex.urlopen")
    def test_post_transport_uncertainty_remains_unknown_without_retry(self, urlopen):
        urlopen.side_effect = URLError(TimeoutError("uncertain POST"))
        task = self.make_ready_task()

        first = development_codex.dispatch_codex(task.pk, self.owner.pk)
        second = development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(first.state, development_codex.STATE_DISPATCH_UNKNOWN)
        self.assertFalse(second.changed)
        self.assertEqual(urlopen.call_count, 1)
        task.refresh_from_db()
        iteration = task.iterations.get(executor_type=DevelopmentIteration.EXECUTOR_CODEX)
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(iteration.automation_metadata["state"], "dispatch_unknown")

    @patch("pool_service.services.development_codex._download_artifact_archive")
    @patch("pool_service.services.development_codex._github_request")
    def test_no_changes_summary_validates_correlated_trusted_artifact(self, request, download):
        self.artifact_lookup_patcher.stop()
        files = artifact_files(final=b"No repository changes are required.")
        request.return_value = {
            "artifacts": [
                {
                    "id": 44,
                    "name": "codex-change-launch-token",
                    "expired": False,
                    "size_in_bytes": len(artifact_zip(files)),
                }
            ]
        }
        download.return_value = artifact_zip(files)

        artifact = development_codex._codex_artifact(
            501,
            "DEV-0002",
            "launch-token",
            "codex/dev-2-123456789abc",
            "gpt-5.6-sol",
            required=True,
        )

        self.assertEqual(artifact["summary"], "No repository changes are required.")
        self.assertEqual(artifact["artifact_id"], 44)
        self.assertEqual(artifact["usage"]["input_tokens"], 1000)
        self.assertIn("/actions/runs/501/artifacts?", request.call_args.args[1])

    @patch("pool_service.services.development_codex.urlopen")
    @patch("pool_service.services.development_codex.build_opener")
    def test_artifact_redirect_does_not_forward_authorization(self, build_opener, urlopen):
        location = "https://trusted-artifacts.example/download?signature=value"
        build_opener.return_value.open.side_effect = HTTPError(
            "https://api.github.com/artifact",
            302,
            "redirect",
            {"Location": location},
            io.BytesIO(),
        )
        response = MagicMock()
        response.status = 200
        response.headers = {"Content-Length": "3"}
        response.read.return_value = b"zip"
        urlopen.return_value.__enter__.return_value = response

        content = development_codex._download_artifact_archive(44)

        self.assertEqual(content, b"zip")
        redirected_request = urlopen.call_args.args[0]
        self.assertEqual(redirected_request.full_url, location)
        self.assertIsNone(redirected_request.get_header("Authorization"))

    @patch("pool_service.services.development_codex._github_request")
    def test_workflow_outcome_job_provides_trusted_validation_state(self, request):
        request.return_value = {
            "jobs": [
                {"name": "codex"},
                {"name": "outcome-security_blocked"},
            ]
        }

        state = development_codex._workflow_validation_state(501)

        self.assertEqual(state, "security_blocked")
        self.assertIn("/actions/runs/501/jobs", request.call_args.args[1])

    @patch("pool_service.services.development_codex._github_request")
    def test_workflow_outcome_job_recognizes_no_changes(self, request):
        request.return_value = {
            "jobs": [
                {"name": "codex"},
                {"name": "outcome-no_changes"},
            ]
        }

        state = development_codex._workflow_validation_state(501)

        self.assertEqual(state, development_codex.STATE_NO_CHANGES)

    @override_settings(GITHUB_DEVELOPMENT_TOKEN="")
    def test_unconfigured_ui_hides_launch_button(self):
        task = self.make_ready_task()
        self.client.force_login(self.owner)

        response = self.client.get(reverse("development_task_detail", args=[task.pk]))

        self.assertContains(response, "Интеграция GitHub Actions пока не настроена")
        self.assertNotContains(response, ">Передать в Codex<", html=False)
        self.assertNotContains(response, "Открыть запуск GitHub Actions")
        self.assertNotContains(response, "Run #")

    def test_retired_workflow_has_no_model_credentials_or_write_jobs(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/development-codex.yml").read_text()
        self.assertIn("permissions: {}", workflow)
        for forbidden in ("secrets.", "openai/codex-action", "codex exec", "contents: write",
                          "pull-requests: write", "  publish:", "actions/checkout"):
            self.assertNotIn(forbidden, workflow)

    def test_retired_dispatch_cannot_execute_for_any_actor(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/development-codex.yml").read_text()
        self.assertEqual(workflow_trigger_names(workflow), ["workflow_dispatch"])
        self.assertNotIn("if:", workflow)
        self.assertNotIn("github.actor", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertIn("exit 1", workflow)

    def test_repository_has_no_conflicting_automatic_workflows(self):
        workflow_dir = Path(settings.BASE_DIR) / ".github/workflows"
        workflows = sorted(workflow_dir.glob("*.y*ml"))
        production_workflow = "development-codex.yml"
        manual_canary_workflows = {
            "codex-plan-connection.yml",
            "development-codex-chatgpt-canary.yml",
            "development-codex-chatgpt-repo-canary.yml",
        }
        allowed_automatic_workflows = {
            "ci-deploy.yml",
            "direct-pr-review.yml",
            "hosting-connection-check.yml",
            "management-finance-mysql.yml",
        }
        expected_workflows = (
            manual_canary_workflows | {production_workflow} | allowed_automatic_workflows
        )

        self.assertEqual({item.name for item in workflows}, expected_workflows)
        self.assertEqual(
            [item.name for item in workflows if item.name not in manual_canary_workflows],
            sorted({production_workflow} | allowed_automatic_workflows),
        )
        for workflow in workflows:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                if workflow.name == "ci-deploy.yml":
                    expected_triggers = ["pull_request", "push", "workflow_dispatch"]
                elif workflow.name == "direct-pr-review.yml":
                    expected_triggers = ["workflow_dispatch"]
                elif workflow.name == "hosting-connection-check.yml":
                    expected_triggers = ["workflow_dispatch", "push"]
                elif workflow.name in allowed_automatic_workflows:
                    expected_triggers = ["pull_request", "workflow_dispatch"]
                else:
                    expected_triggers = ["workflow_dispatch"]
                self.assertEqual(workflow_trigger_names(text), expected_triggers)

    def test_ci_deploy_workflow_enforces_review_and_fail_closed_deployment_policy(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/ci-deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("ADVISOR_MCP_TEST_ENABLED: \"false\"", workflow)
        self.assertIn("review-gate:", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn(".merge_commit_sha == $sha", workflow)
        self.assertIn("gh api --paginate --slurp", workflow)
        self.assertIn("verify_deploy_review.py", workflow)
        self.assertIn("needs: [test, review-gate]", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertIn('HOST_LOOKUP="[$DEPLOY_HOST]:$PORT"', workflow)
        self.assertIn('"$REMOTE_COMMAND" < update.sh', workflow)
        self.assertIn("DEPLOY_HEALTH_URL", workflow)
        self.assertIn("--connect-timeout 10 --max-time 30", workflow)
        self.assertIn('[[ "$status" =~ ^2[0-9][0-9]$ ]]', workflow)
        self.assertIn('value.scheme != "https"', workflow)
        self.assertIn("concurrency:", workflow)

    def test_codex_jsonl_usage_parser_accepts_trusted_completed_usage(self):
        builder = load_usage_builder()
        jsonl = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "untrusted"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1200,
                            "cached_input_tokens": 300,
                            "output_tokens": 400,
                        },
                    }
                ),
            ]
        )
        for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            with self.subTest(model=model):
                usage = builder.build_usage(
                    jsonl_text=jsonl,
                    process_exit_code=0,
                    task_reference="DEV-0002",
                    launch_token="launch-token",
                    branch_name="codex/dev-2-123456789abc",
                    workflow_run_id=501,
                    model=model,
                )
                self.assertEqual(usage["cached_input_tokens"], 300)
                self.assertEqual(usage["model"], model)
                self.assertEqual(usage["usage_source"], builder.USAGE_SOURCE)

    def test_codex_jsonl_usage_parser_fails_closed(self):
        builder = load_usage_builder()
        cases = {
            "missing completion": json.dumps({"type": "turn.started"}),
            "missing usage": json.dumps({"type": "turn.completed"}),
            "malformed json": "{not-json",
            "negative tokens": json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": -1, "cached_input_tokens": 0, "output_tokens": 0,
            }}),
            "cached exceeds input": json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0,
            }}),
        }
        for name, jsonl in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.parse_codex_usage(jsonl)
        valid = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
        }})
        with self.assertRaisesRegex(ValueError, "did not exit successfully"):
            builder.build_usage(
                jsonl_text=valid, process_exit_code=1, task_reference="DEV-0002",
                launch_token="launch-token", branch_name="codex/dev-2-123456789abc",
                workflow_run_id=501, model="gpt-5.6-sol",
            )

    def test_retired_workflow_explains_subscription_path_without_api_fallback(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/development-codex.yml").read_text()
        self.assertIn("Codex signed in with ChatGPT", workflow)
        self.assertIn("native Codex GitHub code review", workflow)
        self.assertNotIn("openai-api-key", workflow)
        self.assertNotIn("OPENAI_API_KEY", workflow)

    def test_validator_accepts_correlated_no_changes_artifact(self):
        validator = load_patch_validator()
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir)
            for name, content in artifact_files().items():
                (artifact_dir / name).write_bytes(content)
            argv = validator_argv(artifact_dir)
            output = io.StringIO()
            with patch.object(validator.sys, "argv", argv), contextlib.redirect_stdout(output):
                validator.main()

        self.assertEqual(json.loads(output.getvalue())["state"], "no_changes")

    def test_validator_rejects_usage_correlation_and_digest_mismatches(self):
        validator = load_patch_validator()
        expected = {
            "task_reference": "DEV-0002",
            "launch_token": "launch-token",
            "branch_name": "codex/dev-2-123456789abc",
            "workflow_run_id": 501,
            "model": "gpt-5.6-sol",
        }
        valid_usage = json.loads(artifact_files()["codex-usage.json"])
        for key, invalid in (
            ("task_reference", "DEV-9999"),
            ("launch_token", "other-token"),
            ("branch_name", "codex/dev-9-deadbeefdead"),
            ("workflow_run_id", 999),
            ("model", "gpt-5.6-terra"),
        ):
            usage = {**valid_usage, key: invalid}
            with self.subTest(key=key), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as blocked:
                    validator.validate_usage(json.dumps(usage).encode("utf-8"), expected)
                self.assertEqual(blocked.exception.code, validator.SECURITY_BLOCKED_EXIT)

        manifest = json.loads(artifact_files()["manifest.json"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as blocked:
            validator.validate_digest(manifest, "usage", b"tampered")
        self.assertEqual(blocked.exception.code, validator.SECURITY_BLOCKED_EXIT)

        for forbidden in ("calculated_cost_usd", "pricing_version"):
            usage = {**valid_usage, forbidden: "untrusted"}
            with self.subTest(forbidden=forbidden), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as blocked:
                    validator.validate_usage(json.dumps(usage).encode("utf-8"), expected)
                self.assertEqual(blocked.exception.code, validator.SECURITY_BLOCKED_EXIT)

    def test_validator_accepts_regular_changes_artifact(self):
        validator = load_patch_validator()
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repository"
            artifact_dir = Path(temp_dir) / "artifact"
            repository.mkdir()
            artifact_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            source = repository / "application.py"
            source.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "application.py"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            source.write_text("value = 2\n", encoding="utf-8")
            patch_content = subprocess.check_output(
                ["git", "diff", "--binary", "--full-index"], cwd=repository
            )
            subprocess.run(["git", "restore", "application.py"], cwd=repository, check=True)
            for name, content in artifact_files(
                result="changes", patch_content=patch_content
            ).items():
                (artifact_dir / name).write_bytes(content)
            argv = validator_argv(artifact_dir)
            output = io.StringIO()
            with (
                contextlib.chdir(repository),
                patch.object(validator.sys, "argv", argv),
                contextlib.redirect_stdout(output),
            ):
                validator.main()

        self.assertEqual(json.loads(output.getvalue())["state"], "changes")

    def test_validator_rejects_empty_patch_for_changes_result(self):
        validator = load_patch_validator()
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir)
            for name, content in artifact_files(result="changes").items():
                (artifact_dir / name).write_bytes(content)
            argv = validator_argv(artifact_dir)
            with patch.object(validator.sys, "argv", argv), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit) as invalid:
                    validator.main()

        self.assertEqual(invalid.exception.code, 2)

    def test_validator_rejects_nonempty_patch_for_no_changes_result(self):
        validator = load_patch_validator()
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir)
            for name, content in artifact_files(patch_content=b"untrusted patch").items():
                (artifact_dir / name).write_bytes(content)
            argv = validator_argv(artifact_dir)
            with patch.object(validator.sys, "argv", argv), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit) as blocked:
                    validator.main()

        self.assertEqual(blocked.exception.code, validator.SECURITY_BLOCKED_EXIT)

    def test_forbidden_patch_path_policy_is_fail_closed(self):
        validator = load_patch_validator()
        blocked = {
            ".github/workflows/x.yml",
            ".github/actions/check/action.yml",
            ".gitmodules",
            ".gitattributes",
            ".env",
            "config/.env.production",
            "deploy",
            "deploy.ps1",
            "deploy/release.sh",
            "update.sh",
            "passenger_wsgi.py",
            "service_site/wsgi.py",
            "service_site/asgi.py",
        }
        for path in blocked:
            with self.subTest(path=path):
                self.assertTrue(validator.forbidden_reason(path))
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as blocked_exit:
                        validator.validate_relative_path(path)
                self.assertEqual(blocked_exit.exception.code, validator.SECURITY_BLOCKED_EXIT)
        for path in {
            ".gitignore",
            "pool_service/development_views.py",
            "pool_service/tests/test_feature.py",
        }:
            with self.subTest(path=path):
                self.assertEqual(validator.forbidden_reason(path), "")
                validator.validate_relative_path(path)

    def test_binary_patch_path_parser_blocks_protected_path_and_allows_application_file(self):
        validator = load_patch_validator()
        with patch.object(validator.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=b"1\t0\tpool_service/development_views.py\0",
                stderr=b"",
            )
            self.assertEqual(
                validator.patch_paths(Path("unused.patch")),
                ["pool_service/development_views.py"],
            )
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=b"1\t0\t.github/workflows/unsafe.yml\0",
                stderr=b"",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as blocked_exit:
                    validator.patch_paths(Path("unused.patch"))
            self.assertEqual(blocked_exit.exception.code, validator.SECURITY_BLOCKED_EXIT)

    def test_retired_workflow_never_applies_or_publishes_legacy_artifacts(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/development-codex.yml").read_text()
        for forbidden in ("git apply", "git commit", "git push", "gh pr", "gh workflow",
                          "download-artifact", "upload-artifact", "manage.py", "pip install"):
            self.assertNotIn(forbidden, workflow)

    def test_retired_dispatch_reports_failure_instead_of_false_success(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/development-codex.yml").read_text()
        script = workflow.split("        run: |\n", 1)[1]
        script = "\n".join(line[10:] for line in script.splitlines())
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Separate API development is disabled", result.stdout)
        self.assertNotIn("success", result.stdout.lower())


@CODEX_SETTINGS
class DevelopmentCodexTransactionTests(CodexTestMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Codex transaction org", paid_until=timezone.now() + timedelta(days=30)
        )
        self.owner = self.make_user("codex-transaction-owner", "owner")

    def test_dispatch_http_call_runs_outside_database_transaction(self):
        task = self.make_ready_task()
        atomic_states = []

        def observe(_payload):
            atomic_states.append(connection.in_atomic_block)

        with patch(
            "pool_service.services.development_codex._dispatch_workflow",
            side_effect=observe,
        ), patch(
            "pool_service.services.development_db.close_old_connections"
        ) as close_connections:
            development_codex.dispatch_codex(task.pk, self.owner.pk)

        self.assertEqual(atomic_states, [False])
        self.assertEqual(close_connections.call_count, 2)
