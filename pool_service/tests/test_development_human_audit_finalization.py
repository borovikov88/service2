from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    Notification,
    Organization,
    OrganizationAccess,
)
from pool_service.services.development_audit import (
    HUMAN_AUDIT_ACTIVITY,
    finalize_development_task_after_audit,
)


class DevelopmentHumanAuditFinalizationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Human Audit",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = self.user_with_role("audit-owner", "owner")
        self.admin = self.user_with_role("audit-admin", "admin")
        self.manager = self.user_with_role("audit-manager", "manager")
        self.service = self.user_with_role("audit-service", "service")

    def user_with_role(self, username, role, *, organization=None, superuser=False):
        user = User.objects.create_user(
            username,
            password="test-password",
            is_superuser=superuser,
            is_staff=superuser,
        )
        OrganizationAccess.objects.create(
            user=user,
            organization=organization or self.organization,
            role=role,
        )
        return user

    def create_task(self, *, status=DevelopmentTask.STATUS_REVIEW, organization=None):
        stage = {
            DevelopmentTask.STATUS_REVIEW: DevelopmentTask.STAGE_REVIEW,
            DevelopmentTask.STATUS_BLOCKED: DevelopmentTask.STAGE_REVIEW,
            DevelopmentTask.STATUS_READY_FOR_DEPLOY: DevelopmentTask.STAGE_COMPLETION,
            DevelopmentTask.STATUS_REVISION: DevelopmentTask.STAGE_DEVELOPMENT,
            DevelopmentTask.STATUS_ANALYSIS: DevelopmentTask.STAGE_ANALYSIS,
            DevelopmentTask.STATUS_CODEX_WORKING: DevelopmentTask.STAGE_DEVELOPMENT,
            DevelopmentTask.STATUS_CANCELLED: DevelopmentTask.STAGE_COMPLETION,
            DevelopmentTask.STATUS_DONE: DevelopmentTask.STAGE_COMPLETION,
        }[status]
        return DevelopmentTask.objects.create(
            organization=organization or self.organization,
            initiator=self.owner,
            title="Audited task",
            description="Already independently verified",
            status=status,
            current_stage=stage,
            current_activity="Awaiting audit decision",
            blockers="Historical blocker",
            automation_metadata={"active_codex_iteration_id": 42},
        )

    def create_iteration(
        self,
        task,
        *,
        executor=DevelopmentIteration.EXECUTOR_CODEX,
        status=DevelopmentIteration.STATUS_ACCEPTED,
        metadata=None,
    ):
        return DevelopmentIteration.objects.create(
            task=task,
            iteration_number=task.iterations.count() + 1,
            executor_type=executor,
            status=status,
            prompt="historical prompt",
            response="historical response",
            automation_metadata=metadata or {"purpose": "codex_execution", "state": "completed"},
        )

    def url(self, task):
        return reverse("development_task_finalize_audit", args=[task.pk])

    def test_owner_admin_and_tenant_superuser_can_finalize_review(self):
        users = (
            self.owner,
            self.admin,
            self.user_with_role("audit-superuser", "manager", superuser=True),
        )
        for index, user in enumerate(users):
            with self.subTest(user=user.username):
                task = self.create_task()
                self.client.force_login(user)
                response = self.client.post(
                    self.url(task),
                    {"note": f"Independent audit #{index} confirmed"},
                )
                task.refresh_from_db()
                self.assertRedirects(
                    response,
                    reverse("development_task_detail", args=[task.pk]),
                )
                self.assertEqual(task.status, DevelopmentTask.STATUS_DONE)
                self.assertEqual(task.current_stage, DevelopmentTask.STAGE_COMPLETION)
                self.assertEqual(task.current_activity, HUMAN_AUDIT_ACTIVITY)
                self.assertEqual(task.blockers, "")
                self.assertIsNotNone(task.completed_at)
                event = task.events.get(metadata__action="human_audit_finalized")
                self.assertEqual(event.actor, user)
                self.assertEqual(event.metadata["actor_id"], user.pk)
                self.assertEqual(event.metadata["old_status"], DevelopmentTask.STATUS_REVIEW)
                self.assertEqual(event.metadata["new_status"], DevelopmentTask.STATUS_DONE)
                self.assertEqual(
                    event.metadata["note"], f"Independent audit #{index} confirmed"
                )
                self.assertEqual(
                    event.metadata["operation_key"],
                    f"task:{task.pk}:human-audit-finalization",
                )
                self.assertTrue(event.metadata["finalized_at"])
                self.assertEqual(event.metadata["finalized_at"], task.completed_at.isoformat())

    def test_blocked_ready_for_deploy_and_revision_are_allowed(self):
        for status in (
            DevelopmentTask.STATUS_BLOCKED,
            DevelopmentTask.STATUS_READY_FOR_DEPLOY,
            DevelopmentTask.STATUS_REVISION,
        ):
            with self.subTest(status=status):
                task = self.create_task(status=status)
                result = finalize_development_task_after_audit(
                    task.pk, self.owner.pk, "Human audit confirmed"
                )
                task.refresh_from_db()
                self.assertEqual(result.state, "finalized")
                self.assertTrue(result.changed)
                self.assertEqual(task.status, DevelopmentTask.STATUS_DONE)
                self.assertEqual(
                    task.events.get(metadata__action="human_audit_finalized").metadata[
                        "old_status"
                    ],
                    status,
                )

    def test_repeat_is_idempotent_and_completed_at_is_set_once(self):
        task = self.create_task()
        first = finalize_development_task_after_audit(
            task.pk, self.owner.pk, "Audit confirmed"
        )
        task.refresh_from_db()
        completed_at = task.completed_at
        second = finalize_development_task_after_audit(
            task.pk, self.owner.pk, "Duplicate submission"
        )
        task.refresh_from_db()

        self.assertTrue(first.changed)
        self.assertEqual(second.state, "already_finalized")
        self.assertFalse(second.changed)
        self.assertEqual(task.completed_at, completed_at)
        self.assertEqual(
            task.events.filter(metadata__action="human_audit_finalized").count(), 1
        )
        self.assertEqual(Notification.objects.count(), 0)

    def test_task_done_by_another_mechanism_is_not_re_finalized(self):
        task = self.create_task(status=DevelopmentTask.STATUS_DONE)
        original_completed_at = timezone.now() - timedelta(days=1)
        task.completed_at = original_completed_at
        task.save(update_fields=["completed_at"])

        result = finalize_development_task_after_audit(
            task.pk, self.owner.pk, "Audit note"
        )
        task.refresh_from_db()

        self.assertEqual(result.state, "already_done")
        self.assertFalse(result.changed)
        self.assertEqual(task.completed_at, original_completed_at)
        self.assertFalse(
            task.events.filter(metadata__action="human_audit_finalized").exists()
        )

    def test_manager_and_worker_are_forbidden(self):
        for user in (self.manager, self.service):
            with self.subTest(user=user.username):
                task = self.create_task()
                self.client.force_login(user)
                response = self.client.post(self.url(task), {"note": "Denied"})
                task.refresh_from_db()
                self.assertEqual(response.status_code, 403)
                self.assertEqual(task.status, DevelopmentTask.STATUS_REVIEW)

    def test_cross_tenant_is_hidden(self):
        task = self.create_task()
        other = Organization.objects.create(
            name="Other Human Audit",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role(
            "other-audit-admin", "admin", organization=other
        )
        self.client.force_login(other_admin)

        response = self.client.post(self.url(task), {"note": "Cross tenant"})
        task.refresh_from_db()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(task.status, DevelopmentTask.STATUS_REVIEW)

    def test_endpoint_is_post_only_and_csrf_protected(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url(task)).status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(self.url(task), {"note": "No CSRF"})
        task.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(task.status, DevelopmentTask.STATUS_REVIEW)

    def test_empty_and_too_long_notes_are_rejected(self):
        cases = (("", "note_required"), ("x" * 2001, "invalid_note"))
        for note, expected in cases:
            with self.subTest(expected=expected):
                task = self.create_task()
                result = finalize_development_task_after_audit(
                    task.pk, self.owner.pk, note
                )
                task.refresh_from_db()
                self.assertEqual(result.state, expected)
                self.assertFalse(result.changed)
                self.assertEqual(task.status, DevelopmentTask.STATUS_REVIEW)
                self.assertEqual(task.events.count(), 0)

    def test_active_analysis_and_codex_working_are_rejected(self):
        cases = (
            (DevelopmentTask.STATUS_ANALYSIS, DevelopmentIteration.EXECUTOR_SYSTEM),
            (DevelopmentTask.STATUS_CODEX_WORKING, DevelopmentIteration.EXECUTOR_CODEX),
        )
        for task_status, executor in cases:
            with self.subTest(task_status=task_status):
                task = self.create_task(status=task_status)
                self.create_iteration(
                    task,
                    executor=executor,
                    status=DevelopmentIteration.STATUS_WORKING,
                    metadata={"purpose": "active_execution", "state": "in_progress"},
                )
                result = finalize_development_task_after_audit(
                    task.pk, self.owner.pk, "Must be rejected"
                )
                task.refresh_from_db()
                self.assertEqual(result.state, "not_available")
                self.assertEqual(task.status, task_status)
                self.assertEqual(task.events.count(), 0)

    def test_cancelled_task_is_rejected(self):
        task = self.create_task(status=DevelopmentTask.STATUS_CANCELLED)
        result = finalize_development_task_after_audit(
            task.pk, self.owner.pk, "Cancelled tasks stay cancelled"
        )
        task.refresh_from_db()
        self.assertEqual(result.state, "not_available")
        self.assertFalse(result.changed)
        self.assertEqual(task.status, DevelopmentTask.STATUS_CANCELLED)
        self.assertEqual(task.events.count(), 0)

    @patch("pool_service.services.development_ai._create_background_response")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_review._create_response")
    def test_iterations_and_external_integrations_are_untouched(
        self, review_openai, github, analysis_openai
    ):
        task = self.create_task(status=DevelopmentTask.STATUS_BLOCKED)
        codex = self.create_iteration(task)
        before = list(task.iterations.order_by("id").values())
        task_metadata = deepcopy(task.automation_metadata)

        result = finalize_development_task_after_audit(
            task.pk, self.owner.pk, "Historical implementation verified"
        )

        task.refresh_from_db()
        codex.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertEqual(list(task.iterations.order_by("id").values()), before)
        self.assertEqual(task.automation_metadata, task_metadata)
        self.assertEqual(task.iterations.count(), 1)
        review_openai.assert_not_called()
        analysis_openai.assert_not_called()
        github.assert_not_called()

    def test_stuck_ai_review_is_left_byte_for_byte_unchanged(self):
        task = self.create_task(status=DevelopmentTask.STATUS_REVIEW)
        review = self.create_iteration(
            task,
            executor=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_WORKING,
            metadata={
                "purpose": "ai_review",
                "state": "launching",
                "operation_key": "task:5:codex:9:review",
                "launch_token": "historical-launch-token",
                "codex_iteration_id": 9,
            },
        )
        before = DevelopmentIteration.objects.values().get(pk=review.pk)

        result = finalize_development_task_after_audit(
            task.pk, self.owner.pk, "Human audit supersedes unfinished review"
        )

        after = DevelopmentIteration.objects.values().get(pk=review.pk)
        task.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertEqual(task.status, DevelopmentTask.STATUS_DONE)
        self.assertEqual(after, before)
        self.assertEqual(after["status"], DevelopmentIteration.STATUS_WORKING)
        self.assertEqual(after["automation_metadata"]["state"], "launching")
        self.assertEqual(
            after["automation_metadata"]["operation_key"],
            "task:5:codex:9:review",
        )

    def test_ui_action_visibility_and_form_contract(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("development_task_detail", args=[task.pk]))
        self.assertContains(response, "Завершить после аудита")
        self.assertContains(response, "Комментарий по аудиту")
        self.assertContains(response, 'maxlength="2000"')
        self.assertContains(response, "required")
        self.assertContains(response, "csrfmiddlewaretoken")

        for status in (
            DevelopmentTask.STATUS_DONE,
            DevelopmentTask.STATUS_CANCELLED,
            DevelopmentTask.STATUS_ANALYSIS,
            DevelopmentTask.STATUS_CODEX_WORKING,
        ):
            with self.subTest(status=status):
                hidden = self.create_task(status=status)
                page = self.client.get(
                    reverse("development_task_detail", args=[hidden.pk])
                )
                self.assertNotContains(page, "Завершить после аудита")
