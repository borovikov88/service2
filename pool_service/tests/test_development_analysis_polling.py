from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    Notification,
    Organization,
    OrganizationAccess,
    Profile,
)
from pool_service.services.development_notifications import notify_ready_for_deploy


AI_SETTINGS = override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_DEVELOPMENT_MODEL="test-model",
    OPENAI_DEVELOPMENT_TIMEOUT_SECONDS=3,
)


class DevelopmentAnalysisPollingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Организация фонового анализа",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user("poll-owner", password="test")
        OrganizationAccess.objects.create(
            user=self.owner, organization=self.organization, role="owner"
        )

    def create_analysis(self, response_id="resp_poll"):
        task = DevelopmentTask.objects.create(
            organization=self.organization,
            initiator=self.owner,
            title="Фоновая проверка",
            description="Проверить анализ без браузера.",
            status=DevelopmentTask.STATUS_ANALYSIS,
            current_stage=DevelopmentTask.STAGE_ANALYSIS,
        )
        iteration = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            status=DevelopmentIteration.STATUS_WORKING,
            automation_metadata={
                "purpose": "primary_analysis",
                "provider": "openai",
                "response_id": response_id,
                "state": "in_progress",
            },
        )
        return task, iteration

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    @patch("pool_service.services.development_notifications.send_push_to_users")
    def test_command_completes_once_and_notifies_once(self, send_push, retrieve):
        task, iteration = self.create_analysis()
        retrieve.return_value = SimpleNamespace(
            id="resp_poll", status="completed", output_text="Готовый анализ"
        )

        with self.captureOnCommitCallbacks(execute=True):
            call_command("poll_development_analyses", stdout=StringIO())
        call_command("poll_development_analyses", stdout=StringIO())

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_READY_FOR_CODEX)
        self.assertEqual(iteration.response, "Готовый анализ")
        self.assertEqual(retrieve.call_count, 1)
        notification = Notification.objects.get(kind="development", user=self.owner)
        self.assertIn(task.reference, notification.title)
        self.assertIn(task.title, notification.message)
        self.assertEqual(notification.action_url, f"/development/tasks/{task.pk}/")
        send_push.assert_called_once()

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_temporary_error_is_retried_by_next_command(self, retrieve):
        task, iteration = self.create_analysis("resp_retry")
        retrieve.side_effect = [TimeoutError("temporary"), SimpleNamespace(
            id="resp_retry", status="completed", output_text="Анализ после повтора"
        )]

        call_command("poll_development_analyses", stdout=StringIO())
        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_ANALYSIS)
        call_command("poll_development_analyses", stdout=StringIO())

        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_READY_FOR_CODEX)
        self.assertEqual(retrieve.call_count, 2)

    def test_ready_for_deploy_notification_is_idempotent(self):
        task, _iteration = self.create_analysis()

        notify_ready_for_deploy(task)
        notify_ready_for_deploy(task)

        notifications = Notification.objects.filter(
            user=self.owner,
            dedupe_key=f"development-task:{task.pk}:ready-for-deploy",
        )
        self.assertEqual(notifications.count(), 1)
        self.assertIn("готова к деплою", notifications.get().title)

    @patch("pool_service.services.development_notifications.send_push_to_users")
    def test_push_preference_is_respected(self, send_push):
        Profile.objects.update_or_create(
            user=self.owner, defaults={"push_notifications_enabled": False}
        )
        task, _iteration = self.create_analysis()

        with self.captureOnCommitCallbacks(execute=True):
            notify_ready_for_deploy(task)

        self.assertTrue(Notification.objects.filter(user=self.owner).exists())
        send_push.assert_not_called()

    def test_does_not_notify_user_from_another_tenant(self):
        outsider = User.objects.create_user("poll-outsider", password="test")
        other = Organization.objects.create(name="Другая организация")
        OrganizationAccess.objects.create(user=outsider, organization=other, role="owner")
        task, _iteration = self.create_analysis()

        notify_ready_for_deploy(task)

        self.assertFalse(Notification.objects.filter(user=outsider).exists())
