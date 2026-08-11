from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
    Notification,
    Organization,
    OrganizationAccess,
)
from pool_service.services.development_review import resolve_human_review


SETTINGS = override_settings(
    GITHUB_DEVELOPMENT_TOKEN="test-token",
    GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
    GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml",
    GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=40000,
    DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS=3,
)


class DevelopmentHumanReviewResolutionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Human Review",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = self.user_with_role("human-owner", "owner")
        self.admin = self.user_with_role("human-admin", "admin")
        self.manager = self.user_with_role("human-manager", "manager")

    def user_with_role(self, username, role, *, organization=None):
        user = User.objects.create_user(username, password="test-password")
        OrganizationAccess.objects.create(
            user=user,
            organization=organization or self.organization,
            role=role,
        )
        return user

    def create_human_required_review(self, *, organization=None, initiator=None):
        task = DevelopmentTask.objects.create(
            organization=organization or self.organization,
            initiator=initiator or self.owner,
            title="Resolve review",
            description="Implementation",
            status=DevelopmentTask.STATUS_BLOCKED,
            current_stage=DevelopmentTask.STAGE_REVIEW,
            current_activity="Human decision required",
            blockers="Baseline must be confirmed",
            automation_metadata={"effective_model": "gpt-5.6-luna"},
        )
        DevelopmentIteration.objects.create(
            task=task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            status=DevelopmentIteration.STATUS_ACCEPTED,
            automation_metadata={"purpose": "primary_analysis"},
        )
        codex = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=2,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_ACCEPTED,
            automation_metadata={
                "purpose": "codex_execution",
                "state": "completed",
                "applied": True,
            },
        )
        review = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=3,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_REVISION,
            prompt="sensitive internal AI review prompt",
            result_summary="Baseline needs confirmation",
            automation_metadata={
                "purpose": "ai_review",
                "state": "completed",
                "decision": "human_required",
                "human_reason": "Confirm the known baseline failure",
                "applied": True,
                "codex_iteration_id": codex.pk,
                "fingerprint": "ai-human-required-fingerprint",
            },
        )
        metadata = dict(task.automation_metadata)
        metadata["active_codex_iteration_id"] = codex.pk
        task.automation_metadata = metadata
        task.save(update_fields=["automation_metadata"])
        return task, review

    def resolution_url(self, task, review):
        return reverse(
            "development_task_review_resolve",
            args=[task.pk, review.pk],
        )

    def test_owner_approve_sets_ready_for_deploy_and_preserves_ai_decision(self):
        task, review = self.create_human_required_review()
        self.client.force_login(self.owner)

        with patch("pool_service.services.development_review._create_response") as openai, patch(
            "pool_service.services.development_codex._dispatch_workflow"
        ) as github:
            response = self.client.post(
                self.resolution_url(task, review),
                {"verdict": "approve", "note": "Known finance baseline confirmed"},
            )

        task.refresh_from_db()
        review.refresh_from_db()
        self.assertRedirects(response, reverse("development_task_detail", args=[task.pk]))
        self.assertEqual(task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_COMPLETION)
        self.assertEqual(task.blockers, "")
        self.assertEqual(review.automation_metadata["decision"], "human_required")
        self.assertEqual(review.automation_metadata["human_resolution"], "approve")
        self.assertEqual(
            review.automation_metadata["human_resolution_actor_id"], self.owner.pk
        )
        event = task.events.get(metadata__action="human_review_resolved")
        self.assertEqual(event.actor, self.owner)
        self.assertEqual(event.metadata["human_verdict"], "approve")
        self.assertEqual(event.metadata["review_iteration_id"], review.pk)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{task.pk}:ready-for-deploy"
            ).count(),
            1,
        )
        openai.assert_not_called()
        github.assert_not_called()

    def test_admin_can_approve(self):
        task, review = self.create_human_required_review()
        self.client.force_login(self.admin)

        response = self.client.post(
            self.resolution_url(task, review),
            {"verdict": "approve", "note": "Approved by admin"},
        )

        task.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)

    def test_corrective_sets_revision_and_preserves_human_instructions(self):
        task, review = self.create_human_required_review()
        self.client.force_login(self.owner)

        with patch("pool_service.services.development_codex._dispatch_workflow") as dispatch:
            response = self.client.post(
                self.resolution_url(task, review),
                {"verdict": "corrective", "note": "Add a regression proof for the baseline"},
            )

        task.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.status, DevelopmentTask.STATUS_REVISION)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_DEVELOPMENT)
        self.assertIs(task.automation_metadata["auto_cycle_enabled"], True)
        self.assertEqual(
            task.automation_metadata["human_corrective_review_id"], review.pk
        )
        self.assertEqual(review.automation_metadata["decision"], "human_required")
        self.assertEqual(review.automation_metadata["human_resolution"], "corrective")
        self.assertEqual(
            review.automation_metadata["human_resolution_note"],
            "Add a regression proof for the baseline",
        )
        dispatch.assert_not_called()

    def test_resolution_is_post_only_and_csrf_protected(self):
        task, review = self.create_human_required_review()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.resolution_url(task, review)).status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(
            self.resolution_url(task, review),
            {"verdict": "approve"},
        )
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)

    def test_manager_is_forbidden_and_cross_tenant_review_is_hidden(self):
        task, review = self.create_human_required_review()
        self.client.force_login(self.manager)
        denied = self.client.post(
            self.resolution_url(task, review), {"verdict": "approve"}
        )
        self.assertEqual(denied.status_code, 403)

        other_org = Organization.objects.create(
            name="Other tenant",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role(
            "other-human-admin", "admin", organization=other_org
        )
        self.client.force_login(other_admin)
        hidden = self.client.post(
            self.resolution_url(task, review), {"verdict": "approve"}
        )
        self.assertEqual(hidden.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)

    def test_wrong_or_stale_review_is_rejected(self):
        task, review = self.create_human_required_review()
        stale = review
        current = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=4,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_REVISION,
            automation_metadata={
                "purpose": "ai_review",
                "state": "completed",
                "decision": "human_required",
                "human_reason": "New decision",
                "applied": True,
            },
        )

        stale_result = resolve_human_review(
            task.pk, stale.pk, self.owner.pk, "approve", "old"
        )
        current.automation_metadata["decision"] = "accepted"
        current.save(update_fields=["automation_metadata"])
        wrong_result = resolve_human_review(
            task.pk, current.pk, self.owner.pk, "approve", "wrong"
        )

        task.refresh_from_db()
        self.assertEqual(stale_result.state, "not_available")
        self.assertEqual(wrong_result.state, "not_available")
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertFalse(task.events.filter(metadata__action="human_review_resolved").exists())

    def test_incomplete_or_non_ai_review_is_rejected(self):
        cases = (
            {"state": "pending", "applied": True, "executor": "chatgpt"},
            {"state": "completed", "applied": False, "executor": "chatgpt"},
            {"state": "completed", "applied": True, "executor": "human"},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                task, review = self.create_human_required_review()
                metadata = dict(review.automation_metadata)
                metadata["state"] = case["state"]
                metadata["applied"] = case["applied"]
                review.automation_metadata = metadata
                review.executor_type = case["executor"]
                review.save(update_fields=["automation_metadata", "executor_type"])

                result = resolve_human_review(
                    task.pk, review.pk, self.owner.pk, "approve", f"case {index}"
                )

                task.refresh_from_db()
                self.assertEqual(result.state, "not_available")
                self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
                self.assertFalse(
                    task.events.filter(metadata__action="human_review_resolved").exists()
                )

    def test_repeat_approve_is_idempotent_and_conflicting_verdict_is_rejected(self):
        task, review = self.create_human_required_review()

        first = resolve_human_review(
            task.pk, review.pk, self.owner.pk, "approve", "baseline confirmed"
        )
        second = resolve_human_review(
            task.pk, review.pk, self.owner.pk, "approve", "duplicate"
        )
        conflict = resolve_human_review(
            task.pk, review.pk, self.owner.pk, "corrective", "change it"
        )

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.state, "approve")
        self.assertEqual(conflict.state, "conflict")
        self.assertEqual(
            task.events.filter(metadata__action="human_review_resolved").count(), 1
        )
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{task.pk}:ready-for-deploy"
            ).count(),
            1,
        )

    def test_repeat_corrective_is_idempotent_and_note_is_required(self):
        task, review = self.create_human_required_review()
        missing = resolve_human_review(
            task.pk, review.pk, self.owner.pk, "corrective", ""
        )
        first = resolve_human_review(
            task.pk, review.pk, self.owner.pk, "corrective", "Fix the regression"
        )
        second = resolve_human_review(
            task.pk, review.pk, self.owner.pk, "corrective", "Fix the regression"
        )

        self.assertEqual(missing.state, "note_required")
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(
            task.events.filter(metadata__action="human_review_resolved").count(), 1
        )

    @SETTINGS
    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_poller_dispatches_one_human_corrective_with_note(self, dispatch):
        task, review = self.create_human_required_review()
        resolve_human_review(
            task.pk,
            review.pk,
            self.owner.pk,
            "corrective",
            "Add explicit baseline regression coverage",
        )
        output = StringIO()

        call_command(
            "poll_development_codex",
            task_id=task.pk,
            stdout=output,
        )

        task.refresh_from_db()
        corrective = task.iterations.get(
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            automation_metadata__corrective_review_id=review.pk,
        )
        self.assertIn("Add explicit baseline regression coverage", corrective.prompt)
        self.assertEqual(task.status, DevelopmentTask.STATUS_CODEX_WORKING)
        self.assertEqual(dispatch.call_count, 1)
        self.assertIn("corrective=1", output.getvalue())

    def test_resolution_block_is_visible_only_until_resolution(self):
        task, review = self.create_human_required_review()
        self.client.force_login(self.owner)
        before = self.client.get(reverse("development_task_detail", args=[task.pk]))
        self.assertContains(before, "Принять — готово к деплою")
        self.assertContains(before, "Запросить корректировку")
        self.assertContains(before, "csrfmiddlewaretoken")
        self.assertNotContains(before, "sensitive internal AI review prompt")
        self.assertContains(before, "Внутренний prompt AI Review скрыт.")

        self.client.post(
            self.resolution_url(task, review),
            {"verdict": "approve", "note": "approved"},
        )
        after = self.client.get(reverse("development_task_detail", args=[task.pk]))
        self.assertNotContains(after, "Принять — готово к деплою")
