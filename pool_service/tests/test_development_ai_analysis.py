import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
    Organization,
    OrganizationAccess,
)
from pool_service.services import development_ai
from service_site.settings import _env_float


AI_SETTINGS = override_settings(
    OPENAI_API_KEY="test-api-key-never-sent",
    OPENAI_DEVELOPMENT_MODEL="test-analysis-model",
    OPENAI_DEVELOPMENT_TIMEOUT_SECONDS=3,
    OPENAI_DEVELOPMENT_MAX_OUTPUT_TOKENS=1200,
)


def provider_response(response_id="resp_test_1", status="queued", output_text=""):
    return SimpleNamespace(id=response_id, status=status, output_text=output_text)


class DevelopmentAIAnalysisTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Организация AI-тестов",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = self.user_with_role("ai-owner", "owner")
        self.admin = self.user_with_role("ai-admin", "admin")
        self.manager = self.user_with_role("ai-manager", "manager")

    def user_with_role(self, username, role, organization=None):
        user = User.objects.create_user(username, password="test-password")
        OrganizationAccess.objects.create(
            user=user,
            organization=organization or self.organization,
            role=role,
        )
        return user

    def create_task(self, organization=None, status=DevelopmentTask.STATUS_NEW):
        return DevelopmentTask.objects.create(
            organization=organization or self.organization,
            initiator=self.owner,
            title="Безопасный AI-анализ",
            description="Проанализировать изменение Django-приложения.",
            business_goal="Подготовить реализацию без production-рисков.",
            definition_of_done="Есть проверяемый план и риски.",
            status=status,
            current_stage=DevelopmentTask.STAGE_ANALYSIS,
        )

    def create_analysis_iteration(self, task, metadata=None):
        task.status = DevelopmentTask.STATUS_ANALYSIS
        task.current_stage = DevelopmentTask.STAGE_ANALYSIS
        task.started_at = timezone.now()
        task.current_activity = "Выполняется первичный анализ задачи"
        task.save()
        return DevelopmentIteration.objects.create(
            task=task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt="Структурированный prompt",
            started_at=timezone.now(),
            automation_metadata=metadata or {},
        )

    def start_url(self, task):
        return reverse("development_task_start", args=[task.pk])

    def launch_url(self, task):
        return reverse("development_task_analysis_launch", args=[task.pk])

    def check_url(self, task):
        return reverse("development_task_analysis_check", args=[task.pk])

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_start_creates_one_background_response_and_saves_id(self, create_response):
        create_response.return_value = provider_response()
        task = self.create_task()
        self.client.force_login(self.owner)

        first = self.client.post(self.start_url(task))
        second = self.client.post(self.start_url(task))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(create_response.call_count, 1)
        iteration = task.iterations.get()
        self.assertEqual(iteration.automation_metadata["response_id"], "resp_test_1")
        self.assertEqual(iteration.automation_metadata["state"], "queued")
        self.assertNotIn("test-api-key-never-sent", str(iteration.automation_metadata))
        self.assertNotIn("test-api-key-never-sent", iteration.prompt)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_pending_check_keeps_task_in_analysis(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_pending", "state": "queued"},
        )
        retrieve.return_value = provider_response("resp_pending", "in_progress")
        self.client.force_login(self.admin)

        response = self.client.post(self.check_url(task))

        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_ANALYSIS)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_ANALYSIS)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_WORKING)
        self.assertEqual(iteration.automation_metadata["state"], "in_progress")

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_completed_check_saves_result_and_advances_task(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_done", "state": "in_progress"},
        )
        retrieve.return_value = provider_response(
            "resp_done",
            "completed",
            "Итоговый технический анализ. План реализации и риски проверены.",
        )
        self.client.force_login(self.owner)

        self.client.post(self.check_url(task))

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_ACCEPTED)
        self.assertEqual(iteration.response, retrieve.return_value.output_text)
        self.assertTrue(iteration.result_summary)
        self.assertIsNotNone(iteration.completed_at)
        self.assertEqual(task.status, DevelopmentTask.STATUS_READY_FOR_CODEX)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_DEVELOPMENT)
        self.assertIn("AI-анализ", task.completed_work)
        event = task.events.get(metadata__action="ai_analysis_completed")
        self.assertEqual(event.event_type, DevelopmentTaskEvent.TYPE_STATUS_CHANGED)
        self.assertEqual(event.metadata["iteration_id"], iteration.pk)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_repeated_completed_check_is_idempotent(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_once", "state": "in_progress"},
        )
        retrieve.return_value = provider_response("resp_once", "completed", "Готовый анализ")
        self.client.force_login(self.owner)

        self.client.post(self.check_url(task))
        self.client.post(self.check_url(task))

        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(task.events.filter(metadata__action="ai_analysis_completed").count(), 1)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_completed_check_does_not_overwrite_newer_manual_task_state(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_stale", "state": "in_progress"},
        )
        task.status = DevelopmentTask.STATUS_CANCELLED
        task.save(update_fields=["status", "updated_at"])
        retrieve.return_value = provider_response("resp_stale", "completed", "Устаревший ответ")

        result = development_ai.check_analysis(iteration.pk)

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(result.state, "task_state_changed")
        self.assertEqual(task.status, DevelopmentTask.STATUS_CANCELLED)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_WORKING)
        self.assertEqual(iteration.response, "")
        self.assertFalse(task.events.filter(metadata__action="ai_analysis_completed").exists())

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_failed_response_blocks_task_without_losing_it(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_failed", "state": "in_progress"},
        )
        retrieve.return_value = provider_response("resp_failed", "failed")
        self.client.force_login(self.owner)

        self.client.post(self.check_url(task))

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_ANALYSIS)
        self.assertTrue(task.blockers)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_FAILED)
        self.assertTrue(iteration.technical_errors)
        self.assertFalse(iteration.response)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_cancelled_response_is_recorded(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_cancel", "state": "queued"},
        )
        retrieve.return_value = provider_response("resp_cancel", "cancelled")
        self.client.force_login(self.owner)

        self.client.post(self.check_url(task))

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_CANCELLED)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_openai_launch_exception_never_sets_false_ready_state(self, create_response):
        create_response.side_effect = TimeoutError("provider timeout with secret-like details")
        task = self.create_task()
        self.client.force_login(self.owner)

        self.client.post(self.start_url(task))

        task.refresh_from_db()
        iteration = task.iterations.get()
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertNotEqual(task.status, DevelopmentTask.STATUS_READY_FOR_CODEX)
        self.assertEqual(iteration.automation_metadata["state"], "launch_unknown")
        self.assertNotIn("provider timeout", iteration.technical_errors)
        self.assertNotIn("test-api-key-never-sent", str(iteration.automation_metadata))

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_existing_analysis_task_can_launch_without_new_iteration(self, create_response):
        task = self.create_task()
        iteration = self.create_analysis_iteration(task)
        create_response.return_value = provider_response("resp_recovery")
        self.client.force_login(self.owner)

        response = self.client.post(self.launch_url(task))

        self.assertEqual(response.status_code, 302)
        iteration.refresh_from_db()
        self.assertEqual(task.iterations.count(), 1)
        self.assertEqual(iteration.automation_metadata["response_id"], "resp_recovery")

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_launch_is_idempotent_when_response_id_exists(self, create_response):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_existing", "state": "queued"},
        )

        first = development_ai.launch_analysis(iteration.pk)
        second = development_ai.launch_analysis(iteration.pk)

        self.assertFalse(first.changed)
        self.assertFalse(second.changed)
        create_response.assert_not_called()

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_launching_claim_blocks_duplicate_provider_request(self, create_response):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "state": "launching", "launch_token": "existing"},
        )

        result = development_ai.launch_analysis(iteration.pk)

        self.assertEqual(result.state, "launching")
        self.assertFalse(result.changed)
        create_response.assert_not_called()

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_check_transport_error_keeps_nonterminal_state(self, retrieve):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_network", "state": "queued"},
        )
        retrieve.side_effect = ConnectionError("private provider detail")

        result = development_ai.check_analysis(iteration.pk)

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(result.state, "check_failed")
        self.assertEqual(task.status, DevelopmentTask.STATUS_ANALYSIS)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_WORKING)
        self.assertNotIn("private provider detail", str(iteration.automation_metadata))

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_cross_tenant_check_returns_404_without_provider_call(self, retrieve):
        task = self.create_task()
        self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_tenant", "state": "queued"},
        )
        other_org = Organization.objects.create(
            name="Другая организация AI",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role("other-ai-admin", "admin", other_org)
        self.client.force_login(other_admin)

        response = self.client.post(self.check_url(task))

        self.assertEqual(response.status_code, 404)
        retrieve.assert_not_called()

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_unauthorized_role_cannot_check_or_launch(self, retrieve):
        task = self.create_task()
        self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_denied", "state": "queued"},
        )
        self.client.force_login(self.manager)

        check_response = self.client.post(self.check_url(task))
        launch_response = self.client.post(self.launch_url(task))

        self.assertEqual(check_response.status_code, 403)
        self.assertEqual(launch_response.status_code, 403)
        retrieve.assert_not_called()

    @AI_SETTINGS
    def test_analysis_mutation_endpoints_require_post(self):
        task = self.create_task()
        self.create_analysis_iteration(task)
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(self.launch_url(task)).status_code, 405)
        self.assertEqual(self.client.get(self.check_url(task)).status_code, 405)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai.DevelopmentTaskEvent.objects.create")
    @patch("pool_service.services.development_ai._retrieve_response")
    def test_completed_result_rolls_back_when_audit_event_fails(self, retrieve, event_create):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {"provider": "openai", "response_id": "resp_rollback", "state": "in_progress"},
        )
        retrieve.return_value = provider_response("resp_rollback", "completed", "Анализ")
        event_create.side_effect = RuntimeError("audit unavailable")

        with self.assertRaises(RuntimeError):
            development_ai.check_analysis(iteration.pk)

        task.refresh_from_db()
        iteration.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_ANALYSIS)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_WORKING)
        self.assertEqual(iteration.response, "")
        self.assertFalse(iteration.automation_metadata.get("applied"))

    @AI_SETTINGS
    @patch("pool_service.services.development_ai.OpenAI")
    def test_official_sdk_receives_background_request_without_key_in_payload(self, openai_class):
        task = self.create_task()
        iteration = self.create_analysis_iteration(task)
        sdk_client = openai_class.return_value
        sdk_client.responses.create.return_value = provider_response()

        response = development_ai._create_background_response(iteration, "launch-token")

        self.assertEqual(response.id, "resp_test_1")
        openai_class.assert_called_once_with(
            api_key="test-api-key-never-sent",
            timeout=3,
            max_retries=0,
        )
        kwargs = sdk_client.responses.create.call_args.kwargs
        self.assertTrue(kwargs["background"])
        self.assertEqual(kwargs["model"], "test-analysis-model")
        self.assertEqual(kwargs["input"], iteration.prompt)
        self.assertNotIn("test-api-key-never-sent", str(kwargs))
        self.assertIn("не раскрывай chain-of-thought", kwargs["instructions"].lower())

    @AI_SETTINGS
    @patch("pool_service.services.development_ai.OpenAI")
    def test_retrieve_uses_short_timeout_and_read_only_retry_policy(self, openai_class):
        sdk_client = openai_class.return_value
        sdk_client.responses.retrieve.return_value = provider_response("resp_read")

        result = development_ai._retrieve_response("resp_read")

        self.assertEqual(result.id, "resp_read")
        openai_class.assert_called_once_with(
            api_key="test-api-key-never-sent",
            timeout=3,
            max_retries=2,
        )
        sdk_client.responses.retrieve.assert_called_once_with("resp_read")

    @AI_SETTINGS
    @patch("pool_service.services.development_ai.OpenAI")
    def test_sdk_create_timeout_has_no_retry_and_becomes_launch_unknown(self, openai_class):
        sdk_client = openai_class.return_value
        sdk_client.responses.create.side_effect = TimeoutError("uncertain create")
        task = self.create_task()
        self.client.force_login(self.owner)

        with self.assertLogs("pool_service.services.development_ai", level="WARNING") as logs:
            self.client.post(self.start_url(task))

        iteration = task.iterations.get()
        task.refresh_from_db()
        openai_class.assert_called_once_with(
            api_key="test-api-key-never-sent",
            timeout=3,
            max_retries=0,
        )
        self.assertEqual(sdk_client.responses.create.call_count, 1)
        self.assertEqual(iteration.automation_metadata["state"], "launch_unknown")
        self.assertEqual(task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertNotIn("test-api-key-never-sent", str(iteration.automation_metadata))
        self.assertNotIn("test-api-key-never-sent", "\n".join(logs.output))

    def test_timeout_setting_rejects_invalid_or_unsafe_values(self):
        for value in ("not-a-number", "0", "61", "nan", "inf"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"OPENAI_TEST_TIMEOUT": value},
            ):
                with self.assertRaises(ImproperlyConfigured):
                    _env_float("OPENAI_TEST_TIMEOUT", 25, minimum=1, maximum=60)

    @override_settings(OPENAI_API_KEY="test-api-key-never-sent")
    def test_launch_unknown_ui_explains_lockout_without_retry_button(self):
        task = self.create_task()
        iteration = self.create_analysis_iteration(
            task,
            {
                "provider": "openai",
                "state": "launch_unknown",
                "launch_token": "unknown-launch",
            },
        )
        iteration.status = DevelopmentIteration.STATUS_FAILED
        iteration.save(update_fields=["status", "updated_at"])
        task.status = DevelopmentTask.STATUS_BLOCKED
        task.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.owner)

        response = self.client.get(reverse("development_task_detail", args=[task.pk]))

        self.assertContains(
            response,
            "Не удалось однозначно подтвердить запуск AI-анализа. "
            "Повторный запуск заблокирован для защиты от двойного запроса.",
        )
        self.assertNotContains(response, ">Запустить AI-анализ<", html=False)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_immediate_completed_response_is_applied_once(self, create_response):
        create_response.return_value = provider_response(
            "resp_immediate", "completed", "Анализ готов немедленно"
        )
        task = self.create_task()
        self.client.force_login(self.owner)

        self.client.post(self.start_url(task))

        task.refresh_from_db()
        iteration = task.iterations.get()
        self.assertEqual(task.status, DevelopmentTask.STATUS_READY_FOR_CODEX)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_ACCEPTED)
        self.assertTrue(iteration.automation_metadata["applied"])

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_structured_prompt_is_sent_to_provider(self, create_response):
        create_response.return_value = provider_response()
        task = self.create_task()
        self.client.force_login(self.owner)

        self.client.post(self.start_url(task))

        iteration = create_response.call_args.args[0]
        for expected in (
            "Понимание задачи",
            "технический контекст",
            "security",
            "Definition of Done",
            "рекомендации для следующего этапа Codex",
            "Не раскрывай chain-of-thought",
        ):
            self.assertIn(expected, iteration.prompt)

    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_detail_shows_safe_analysis_controls_without_response_id(self, create_response):
        create_response.return_value = provider_response("resp_not_exposed")
        task = self.create_task()
        self.client.force_login(self.owner)
        self.client.post(self.start_url(task))

        response = self.client.get(reverse("development_task_detail", args=[task.pk]))

        self.assertContains(response, "Проверить анализ")
        self.assertContains(response, "AI-анализ поставлен в очередь")
        self.assertNotContains(response, "resp_not_exposed")
        self.assertNotContains(response, "test-api-key-never-sent")


class DevelopmentAITransactionBoundaryTests(TransactionTestCase):
    @AI_SETTINGS
    @patch("pool_service.services.development_ai._create_background_response")
    def test_start_calls_provider_outside_database_transaction(self, create_response):
        organization = Organization.objects.create(
            name="Организация проверки transaction boundary",
            paid_until=timezone.now() + timedelta(days=30),
        )
        owner = User.objects.create_user("boundary-owner", password="test-password")
        OrganizationAccess.objects.create(
            user=owner,
            organization=organization,
            role="owner",
        )
        task = DevelopmentTask.objects.create(
            organization=organization,
            initiator=owner,
            title="Проверка границы транзакции",
            description="HTTP-вызов выполняется только после commit.",
        )

        def assert_outside_atomic(*_args, **_kwargs):
            self.assertFalse(connection.in_atomic_block)
            return provider_response()

        create_response.side_effect = assert_outside_atomic
        self.client.force_login(owner)

        response = self.client.post(reverse("development_task_start", args=[task.pk]))

        self.assertEqual(response.status_code, 302)
        create_response.assert_called_once()
