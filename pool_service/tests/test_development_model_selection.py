from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import DevelopmentIteration, DevelopmentTask, Organization, OrganizationAccess
from pool_service.services import development_codex
from pool_service.services.development_model_selection import (
    ModelSelectionError,
    effective_model,
    selection_metadata,
    validate_model,
)


class DevelopmentModelSelectionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Model selection", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user("model-owner")

    def task(self, description="Обычная Django CRUD задача"):
        return DevelopmentTask.objects.create(organization=self.organization, initiator=self.user,
                                              title="Выбор модели", description=description)

    def test_ai_complexity_mapping(self):
        expected = {"simple": "gpt-5.6-luna", "standard": "gpt-5.6-terra", "complex": "gpt-5.6-sol"}
        for complexity, model in expected.items():
            task = self.task()
            metadata = selection_metadata(task, f"Анализ\nAUTO_COMPLEXITY: {complexity}\nAUTO_REASON: Проверяемая причина")
            self.assertEqual(metadata["auto_complexity"], complexity)
            self.assertEqual(metadata["auto_selected_model"], model)
            self.assertEqual(metadata["effective_model"], model)

    def test_overrides_and_invalid_model(self):
        self.assertEqual(effective_model("economy", "gpt-5.6-sol"), "gpt-5.6-luna")
        self.assertEqual(effective_model("standard", "gpt-5.6-luna"), "gpt-5.6-terra")
        self.assertEqual(effective_model("maximum", "gpt-5.6-luna"), "gpt-5.6-sol")
        with self.assertRaises(ModelSelectionError):
            validate_model("user-controlled-model")

    def test_selection_is_idempotent_and_preserves_auto_result(self):
        task = self.task()
        task.automation_metadata = selection_metadata(task, "AUTO_COMPLEXITY: simple\nAUTO_REASON: Локальная правка")
        task.automation_metadata["model_selection_mode"] = "maximum"
        second = selection_metadata(task, "AUTO_COMPLEXITY: complex\nAUTO_REASON: Другая оценка")
        self.assertEqual(second["auto_complexity"], "simple")
        self.assertEqual(second["auto_selected_model"], "gpt-5.6-luna")
        self.assertEqual(second["effective_model"], "gpt-5.6-sol")

    def test_legacy_metadata_is_safe(self):
        task = self.task("Исправить текст в небольшом шаблоне")
        task.automation_metadata = None
        metadata = selection_metadata(task, "")
        self.assertEqual(metadata["effective_model"], "gpt-5.6-luna")


@override_settings(GITHUB_DEVELOPMENT_TOKEN="token", GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
                   GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml", GITHUB_DEVELOPMENT_TIMEOUT_SECONDS=3,
                   GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=40000)
class DevelopmentModelDispatchTests(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Dispatch model", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user("dispatch-model-owner")
        OrganizationAccess.objects.create(user=self.user, organization=organization, role="owner")
        self.task = DevelopmentTask.objects.create(
            organization=organization, initiator=self.user, title="CRUD", description="Обычная CRUD задача",
            status=DevelopmentTask.STATUS_READY_FOR_CODEX, current_stage=DevelopmentTask.STAGE_DEVELOPMENT,
            automation_metadata={"auto_complexity": "standard", "auto_selected_model": "gpt-5.6-terra",
                                 "classification_reason": "Обычная задача", "classifier_version": "v1",
                                 "model_selection_mode": "auto", "effective_model": "gpt-5.6-terra"},
        )
        DevelopmentIteration.objects.create(task=self.task, iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM, status=DevelopmentIteration.STATUS_ACCEPTED,
            response="Анализ", automation_metadata={"purpose": "primary_analysis", "applied": True})

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_dispatch_contains_server_controlled_model(self, dispatch):
        development_codex.dispatch_codex(self.task.pk, self.user.pk)
        self.assertEqual(dispatch.call_args.args[0]["inputs"]["codex_model"], "gpt-5.6-terra")

    @patch("pool_service.services.development_codex._dispatch_workflow")
    def test_corrupt_model_fails_closed(self, dispatch):
        metadata = dict(self.task.automation_metadata)
        metadata["auto_selected_model"] = "unknown"
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])
        result = development_codex.dispatch_codex(self.task.pk, self.user.pk)
        self.assertEqual(result.state, "invalid_model")
        dispatch.assert_not_called()

    def test_ui_displays_effective_model(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("development_task_detail", args=[self.task.pk]))
        self.assertContains(response, "GPT-5.6 Terra")
        self.assertContains(response, "Сложность:")
        self.assertContains(response, "Режим:")
