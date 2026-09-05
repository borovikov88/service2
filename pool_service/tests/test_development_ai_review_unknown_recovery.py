import copy
import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    Notification,
    Organization,
    OrganizationAccess,
)
from pool_service.services.development_review import (
    resolve_unknown_ai_review,
    retry_unknown_ai_review,
)
from pool_service.services.development_audit import (
    finalize_development_task_after_audit,
)


SETTINGS = override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_DEVELOPMENT_MODEL="gpt-5.6-luna",
    GITHUB_DEVELOPMENT_TOKEN="test-token",
    GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
    GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml",
    GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=40000,
    DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS=3,
)


@SETTINGS
class DevelopmentAIReviewUnknownRecoveryTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Unknown Review Recovery",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = self.user_with_role("unknown-owner", "owner")
        self.admin = self.user_with_role("unknown-admin", "admin")
        self.manager = self.user_with_role("unknown-manager", "manager")
        self.task, self.codex, self.review = self.create_unknown_review()

    def user_with_role(self, username, role, *, organization=None):
        user = User.objects.create_user(username, password="test-password")
        OrganizationAccess.objects.create(
            user=user,
            organization=organization or self.organization,
            role=role,
        )
        return user

    def create_unknown_review(self, *, organization=None, initiator=None):
        task = DevelopmentTask.objects.create(
            organization=organization or self.organization,
            initiator=initiator or self.owner,
            title="Recover uncertain review",
            description="Implementation was published as a validated PR",
            business_goal="Recover safely",
            definition_of_done="No duplicate external request",
            status=DevelopmentTask.STATUS_BLOCKED,
            current_stage=DevelopmentTask.STAGE_REVIEW,
            current_activity="AI Review requires manual verification",
            blockers="External create outcome is unknown",
            automation_metadata={
                "effective_model": "gpt-5.6-luna",
                "auto_cycle_enabled": True,
            },
        )
        DevelopmentIteration.objects.create(
            task=task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            status=DevelopmentIteration.STATUS_ACCEPTED,
            response="Primary analysis",
            automation_metadata={"purpose": "primary_analysis"},
        )
        codex = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=2,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_ACCEPTED,
            result_summary="Validated implementation",
            response="PR result",
            test_result="passed",
            automation_metadata={
                "purpose": "codex_execution",
                "state": "completed",
                "applied": True,
                "validation_state": "passed",
                "pr_number": 29,
            },
        )
        operation_key = f"task:{task.pk}:codex:{codex.pk}:review"
        review = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=3,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_FAILED,
            prompt="stored AI Review prompt",
            result_summary="AI Review requires manual verification",
            technical_errors="External create outcome is unknown",
            started_at=timezone.now(),
            completed_at=timezone.now(),
            automation_metadata={
                "purpose": "ai_review",
                "operation_key": operation_key,
                "state": "launch_unknown",
                "decision": "human_required",
                "human_reason": "External create outcome is unknown",
                "applied": True,
                "codex_iteration_id": codex.pk,
                "launch_token": "historical-launch-token",
                "launch_started_at": timezone.now().isoformat(),
                "completed_at": timezone.now().isoformat(),
            },
        )
        metadata = dict(task.automation_metadata)
        metadata["active_codex_iteration_id"] = codex.pk
        task.automation_metadata = metadata
        task.save(update_fields=["automation_metadata"])
        return task, codex, review

    def response(self, decision="accepted"):
        body = {
            "decision": decision,
            "summary": f"Decision {decision}",
            "findings": [],
            "corrective_instructions": [],
            "human_reason": None,
        }
        usage = SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(
            id="resp-retry",
            model="gpt-5.6-luna",
            output_text=json.dumps(body),
            usage=usage,
        )

    def retry_url(self, task=None, review=None):
        return reverse(
            "development_task_review_retry_unknown",
            args=[(task or self.task).pk, (review or self.review).pk],
        )

    def resolve_url(self, task=None, review=None):
        return reverse(
            "development_task_review_resolve_unknown",
            args=[(task or self.task).pk, (review or self.review).pk],
        )

    def test_launch_unknown_shows_recovery_and_hides_generic_audit_action(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("development_task_detail", args=[self.task.pk])
        )

        self.assertContains(response, "Не удалось подтвердить запуск AI Review")
        self.assertContains(response, "Повторить AI Review")
        self.assertContains(response, "Решить вручную — готово к деплою")
        self.assertNotContains(response, "Завершить после аудита")
        self.assertNotContains(response, "stored AI Review prompt")

        audit_result = finalize_development_task_after_audit(
            self.task.pk, self.owner.pk, "Must not bypass recovery"
        )
        self.task.refresh_from_db()
        self.assertEqual(audit_result.state, "not_available")
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)

    def test_completed_human_required_keeps_existing_resolution_block(self):
        metadata = dict(self.review.automation_metadata)
        metadata["state"] = "completed"
        self.review.automation_metadata = metadata
        self.review.status = DevelopmentIteration.STATUS_REVISION
        self.review.save(update_fields=["automation_metadata", "status"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("development_task_detail", args=[self.task.pk])
        )

        self.assertContains(response, "Требуется решение по AI Review")
        self.assertContains(response, "Принять — готово к деплою")
        self.assertNotContains(response, "Не удалось подтвердить запуск AI Review")

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_retry_creates_one_pending_review_and_preserves_old_evidence(
        self, github, openai
    ):
        old_metadata = copy.deepcopy(self.review.automation_metadata)

        first = retry_unknown_ai_review(
            self.task.pk, self.review.pk, self.owner.pk
        )
        second = retry_unknown_ai_review(
            self.task.pk, self.review.pk, self.owner.pk
        )

        self.task.refresh_from_db()
        self.review.refresh_from_db()
        retry = self.task.iterations.get(
            automation_metadata__retry_of_review_id=self.review.pk
        )
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(first.review_id, retry.pk)
        self.assertEqual(second.review_id, retry.pk)
        self.assertEqual(self.review.automation_metadata, old_metadata)
        self.assertEqual(retry.automation_metadata["state"], "pending")
        self.assertEqual(
            retry.automation_metadata["operation_key"],
            f"task:{self.task.pk}:codex:{self.codex.pk}:review-retry:1",
        )
        self.assertNotEqual(
            retry.automation_metadata["operation_key"],
            self.review.automation_metadata["operation_key"],
        )
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_REVIEW)
        self.assertEqual(self.task.blockers, "")
        self.assertIs(self.task.automation_metadata["auto_cycle_enabled"], True)
        self.assertEqual(
            self.task.events.filter(
                metadata__action="ai_review_retry_authorized"
            ).count(),
            1,
        )
        openai.assert_not_called()
        github.assert_not_called()

    @patch("pool_service.services.development_review._create_response")
    def test_cron_processes_authorized_retry_once(self, create):
        create.return_value = self.response("accepted")
        retry_unknown_ai_review(self.task.pk, self.review.pk, self.owner.pk)
        first_output = StringIO()
        second_output = StringIO()

        call_command("poll_development_codex", stdout=first_output)
        call_command("poll_development_codex", stdout=second_output)

        self.task.refresh_from_db()
        retry = self.task.iterations.get(
            automation_metadata__retry_of_review_id=self.review.pk
        )
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(retry.automation_metadata["state"], "completed")
        self.assertEqual(create.call_count, 1)
        self.assertIn("reviewed=1", first_output.getvalue())
        self.assertIn("reviewed=0", second_output.getvalue())

    @patch("pool_service.management.commands.poll_development_codex.run_review")
    @patch("pool_service.management.commands.poll_development_codex.check_codex")
    def test_regular_cron_skips_unresolved_launch_unknown(self, check, review):
        output = StringIO()

        call_command("poll_development_codex", stdout=output)

        self.assertEqual(
            output.getvalue().strip(),
            "checked=0 reviewed=0 corrective=0 delivered=0 errors=0",
        )
        check.assert_not_called()
        review.assert_not_called()

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_manual_approve_is_idempotent_and_preserves_unknown_review(
        self, github, openai
    ):
        old_metadata = copy.deepcopy(self.review.automation_metadata)

        first = resolve_unknown_ai_review(
            self.task.pk,
            self.review.pk,
            self.owner.pk,
            "approve",
            "Validated manually against the PR and trusted tests",
        )
        completed_at = self.review.completed_at
        second = resolve_unknown_ai_review(
            self.task.pk,
            self.review.pk,
            self.owner.pk,
            "approve",
            "duplicate",
        )

        self.task.refresh_from_db()
        self.review.refresh_from_db()
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(self.task.current_stage, DevelopmentTask.STAGE_COMPLETION)
        self.assertEqual(self.task.blockers, "")
        self.assertEqual(self.review.automation_metadata, old_metadata)
        self.assertEqual(self.review.completed_at, completed_at)
        self.assertEqual(
            self.task.events.filter(
                metadata__action="ai_review_launch_unknown_resolved"
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{self.task.pk}:ready-for-deploy"
            ).count(),
            1,
        )
        openai.assert_not_called()
        github.assert_not_called()

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_manual_corrective_uses_existing_deferred_dispatch_path(self, dispatch):
        old_metadata = copy.deepcopy(self.review.automation_metadata)

        result = resolve_unknown_ai_review(
            self.task.pk,
            self.review.pk,
            self.owner.pk,
            "corrective",
            "Add explicit regression coverage",
        )

        self.task.refresh_from_db()
        self.review.refresh_from_db()
        resolution_review = self.task.iterations.get(
            automation_metadata__launch_unknown_review_id=self.review.pk
        )
        self.assertTrue(result.changed)
        self.assertEqual(self.review.automation_metadata, old_metadata)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_REVISION)
        self.assertEqual(resolution_review.next_prompt, "Add explicit regression coverage")
        self.assertEqual(
            resolution_review.automation_metadata["human_resolution"], "corrective"
        )
        dispatch.assert_not_called()

        output = StringIO()
        call_command("poll_development_codex", stdout=output)
        self.task.refresh_from_db()
        corrective = self.task.iterations.get(
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            automation_metadata__corrective_review_id=resolution_review.pk,
        )
        self.assertIn("Add explicit regression coverage", corrective.prompt)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_CODEX_WORKING)
        self.assertEqual(dispatch.call_count, 1)
        self.assertIn("corrective=1", output.getvalue())

    def test_manual_resolution_requires_note_and_does_not_fake_response_id(self):
        result = resolve_unknown_ai_review(
            self.task.pk, self.review.pk, self.owner.pk, "approve", ""
        )

        self.task.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual(result.state, "note_required")
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertNotIn("response_id", self.review.automation_metadata)

        too_long = resolve_unknown_ai_review(
            self.task.pk,
            self.review.pk,
            self.owner.pk,
            "approve",
            "x" * 2001,
        )
        self.assertEqual(too_long.state, "invalid_note")

    def test_endpoints_are_post_only_and_csrf_protected(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.retry_url()).status_code, 405)
        self.assertEqual(self.client.get(self.resolve_url()).status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(self.retry_url()).status_code, 403)
        self.assertEqual(
            csrf_client.post(
                self.resolve_url(), {"verdict": "approve", "note": "manual"}
            ).status_code,
            403,
        )
        self.assertEqual(self.task.iterations.count(), 3)

    def test_manager_is_forbidden_and_cross_tenant_is_hidden(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(self.retry_url()).status_code, 403)

        other_org = Organization.objects.create(
            name="Other tenant",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role(
            "unknown-other-admin", "admin", organization=other_org
        )
        self.client.force_login(other_admin)
        self.assertEqual(self.client.post(self.retry_url()).status_code, 404)
        self.assertEqual(
            self.client.post(
                self.resolve_url(), {"verdict": "approve", "note": "manual"}
            ).status_code,
            404,
        )
        self.assertEqual(self.task.iterations.count(), 3)

    def test_admin_can_authorize_retry(self):
        self.client.force_login(self.admin)

        response = self.client.post(self.retry_url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.task.iterations.count(), 4)

    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_legacy_launching_review_without_marker_is_untouched(self, run_review):
        legacy, _codex, legacy_review = self.create_unknown_review()
        legacy.status = DevelopmentTask.STATUS_REVIEW
        metadata = dict(legacy.automation_metadata)
        metadata.pop("auto_cycle_enabled")
        legacy.automation_metadata = metadata
        legacy.save(update_fields=["status", "automation_metadata"])
        review_metadata = dict(legacy_review.automation_metadata)
        review_metadata["state"] = "launching"
        review_metadata.pop("decision")
        review_metadata.pop("applied")
        legacy_review.automation_metadata = review_metadata
        legacy_review.status = DevelopmentIteration.STATUS_WORKING
        legacy_review.save(update_fields=["automation_metadata", "status"])
        before = copy.deepcopy(legacy_review.automation_metadata)

        call_command("poll_development_codex", stdout=StringIO())

        legacy_review.refresh_from_db()
        self.assertEqual(legacy_review.automation_metadata, before)
        run_review.assert_not_called()
