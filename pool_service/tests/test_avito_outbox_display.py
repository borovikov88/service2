from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.avito_outbox_display import conversation_context, error_label, for_connection, message_display
from pool_service.communication_models import ChannelConnection, CommunicationChannel, Conversation, ConversationMessage
from pool_service.models import Organization, OrganizationAccess


class AvitoOutboxDisplayTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Outbox UI organization")
        self.other_organization = Organization.objects.create(name="Other outbox UI organization")
        self.owner = User.objects.create_user("outbox-ui-owner", password="test")
        OrganizationAccess.objects.create(organization=self.organization, user=self.owner, role="owner")
        self.channel = CommunicationChannel.objects.create(organization=self.organization, kind="avito", name="Services")
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel, name="Services", external_id="ui-account")
        self.conversation = Conversation.objects.create(
            organization=self.organization, connection=self.connection,
            external_id="ui-chat", participant_name="Test participant")
        self.queue = {
            "worker_state": "never_run", "periodic_state": "never_run", "immediate_state": "never_run", "last_started_at": None, "last_success_at": None,
            "last_error_code": "", "pending_count": 1, "oldest_pending_at": timezone.now().isoformat(),
            "oldest_pending_age_seconds": 780, "unknown_count": 0, "failed_count": 0, "legacy_held_count": 1,
        }

    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_scope_mismatch_never_loads_queue_or_messages(self, diagnostics):
        self.assertEqual(conversation_context(self.conversation, self.other_organization),
                         {"selected_messages": [], "avito_outbox_state": None})
        diagnostics.assert_not_called()
        # A forged conversation whose connection belongs to another organization
        # is rejected even when its own organization value matches the caller.
        foreign_channel = CommunicationChannel.objects.create(
            organization=self.other_organization, kind="avito", name="Foreign")
        self.conversation.connection = ChannelConnection.objects.create(
            channel=foreign_channel, name="Foreign", external_id="foreign-ui")
        self.assertEqual(conversation_context(self.conversation, self.organization),
                         {"selected_messages": [], "avito_outbox_state": None})
        diagnostics.assert_not_called()

    def test_provider_id_means_accepted_not_read_and_missing_id_is_unconfirmed(self):
        message = ConversationMessage(
            direction="out", delivery_status="delivered", external_id="provider-confirmed",
            delivery_attempts=1, delivered_at=timezone.now())
        state = message_display(message)
        self.assertEqual(state["label"], "Принято Авито")
        self.assertIn("Прочтение клиентом не подтверждено", state["detail"])
        message.external_id = None
        self.assertEqual(message_display(message)["label"], "Отправка не подтверждена")
        message.delivery_status = "sending"
        self.assertEqual(message_display(message)["label"], "Результат отправки неизвестен")

    def test_error_mapping_preserves_http_distinctions_without_assuming_tariff(self):
        self.assertIn("авторизации", error_label("provider_http_401"))
        self.assertIn("HTTP 402", error_label("provider_http_402"))
        self.assertIn("пока не подтверждена", error_label("provider_http_403"))
        self.assertNotEqual(error_label("provider_http_402"), error_label("provider_http_403"))
        self.assertNotIn("SECRET", error_label("<script>SECRET</script>"))

    @patch("pool_service.avito_webhook_chain.display")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_inbound_proof_and_read_access_do_not_assert_outbox_health(self, diagnostics, chain):
        diagnostics.return_value = self.queue
        chain.return_value = {
            "state": "accepted_persisted",
            "state_label": "Входящее событие принято, диалог и сообщение подтверждены.",
            "owner_notification_label": "Утреннее уведомление записано в Service2.",
            "owner_notification_recorded": True, "received_at": timezone.now().isoformat(),
        }
        self.connection.settings = {
            "avito_api_account_id": self.connection.external_id,
            "avito_api_checked_at": timezone.now().isoformat(),
            "avito_api_results": [{"key": "messenger", "status": "ok"}],
            "avito_webhook_status": "connected",
        }
        state = for_connection(self.connection)
        self.assertEqual(state["worker_state"], "never_run")
        self.assertTrue(state["worker_warning"])
        self.assertIn("подтверждено", state["read_label"])
        self.assertEqual(state["webhook_label"], "Подписка зарегистрирована на текущей привязке")
        content = render_to_string("pool_service/communications/_avito_outbox.html", {"outbox": state})
        self.assertIn("Резервная обработка: ещё не подтверждена", content)
        self.assertIn("13 мин.", content)
        self.assertIn("Чтение чатов и приём webhook не подтверждают отправку", content)
        self.assertIn("Доставка push на устройство отдельно не подтверждена", content)

    @patch("pool_service.avito_webhook_chain.display")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_cached_registration_does_not_confirm_changed_binding_but_awaiting_event_can_be_registered(self, diagnostics, chain):
        diagnostics.return_value = self.queue
        self.connection.settings = {"avito_webhook_status": "connected"}
        base = {"state_label": "Цепочка не подтверждена.", "owner_notification_label": "Не проверено."}
        chain.return_value = {**base, "state": "binding_unverified"}
        state = for_connection(self.connection)
        self.assertIn("текущая привязка не подтверждена", state["webhook_label"])
        chain.return_value = {**base, "state": "awaiting_first"}
        state = for_connection(self.connection)
        self.assertEqual(state["webhook_label"], "Подписка зарегистрирована на текущей привязке")

    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_unknown_values_remain_unknown_and_provider_text_is_not_rendered(self, diagnostics):
        diagnostics.return_value = {
            **self.queue, "worker_state": {"invalid": "state"}, "last_error_code": "<script>SECRET</script>",
            "pending_count": None, "oldest_pending_at": "2026-99-99", "oldest_pending_age_seconds": None,
            "unknown_count": False,
        }
        state = for_connection(self.connection)
        content = render_to_string("pool_service/communications/_avito_outbox.html", {"outbox": state})
        self.assertIn("неизвестно", content)
        self.assertNotIn("SECRET", content)
        self.assertEqual(state["worker_state"], "unknown")
        self.assertEqual(state["oldest_pending_at"], "")

    @patch("scripts.avito_outbox_cron.panel_command")
    @patch("pool_service.avito_webhook_chain.display")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_panel_command_requires_current_binding_and_manage_channels_in_same_org(self, diagnostics, chain, panel):
        diagnostics.return_value = {**self.queue, "enabled": True, "binding_valid": True}
        chain.return_value = {"state": "awaiting_first", "state_label": "Ожидается событие.",
                              "owner_notification_label": "Не проверено."}
        self.connection.pk = 4
        self.connection.external_id = "446312402"
        panel.return_value = "SAFE-PANEL-COMMAND"
        manager = User.objects.create_user("outbox-ui-manager")
        OrganizationAccess.objects.create(organization=self.organization, user=manager, role="manager")
        foreign_owner = User.objects.create_user("outbox-ui-foreign-owner")
        OrganizationAccess.objects.create(organization=self.other_organization, user=foreign_owner, role="owner")
        self.assertIsNone(for_connection(self.connection, user=manager)["configuration"])
        self.assertIsNone(for_connection(self.connection, user=foreign_owner)["configuration"])
        panel.assert_not_called()
        state = for_connection(self.connection, user=self.owner)
        self.assertEqual(state["configuration"]["command"], "SAFE-PANEL-COMMAND")
        content = render_to_string("pool_service/communications/_avito_outbox.html", {"outbox": state})
        self.assertIn("* * * * *", content)
        self.assertIn("не означает, что задание уже установлено", content)
        panel.assert_called_once()
        panel.reset_mock()
        diagnostics.return_value = {**self.queue, "enabled": True, "binding_valid": False}
        self.assertIsNone(for_connection(self.connection, user=self.owner)["configuration"])
        panel.assert_not_called()

    @patch("scripts.avito_outbox_cron.panel_command", side_effect=RuntimeError("SECRET"))
    @patch("pool_service.avito_webhook_chain.display")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_panel_command_failure_is_safe_and_primary_success_does_not_prove_periodic_job(self, diagnostics, chain, panel):
        diagnostics.return_value = {
            **self.queue, "worker_state": "healthy", "enabled": True, "binding_valid": True,
            "last_success_at": timezone.now().isoformat(), "last_periodic_success_at": None}
        chain.return_value = {"state": "awaiting_first", "state_label": "Ожидается событие.",
                              "owner_notification_label": "Не проверено."}
        self.connection.pk = 4
        self.connection.external_id = "446312402"
        state = for_connection(self.connection, user=self.owner)
        self.assertTrue(state["post_commit_enabled"])
        self.assertFalse(state["last_periodic_success_at"])
        content = render_to_string("pool_service/communications/_avito_outbox.html", {"outbox": state})
        self.assertIn("Периодический запуск: ещё не подтверждён", content)
        self.assertIn("Не удалось подготовить команду", content)
        self.assertNotIn("SECRET", content)

    @patch("scripts.avito_outbox_cron.panel_command", return_value="SAFE-OWNER-PANEL-COMMAND")
    @patch("pool_service.avito_webhook_chain.display")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_request_user_is_required_for_owner_command_and_managers_never_receive_it(self, diagnostics, chain, panel):
        diagnostics.return_value = {**self.queue, "enabled": True, "binding_valid": True}
        chain.return_value = {"state": "awaiting_first", "state_label": "Ожидается событие.",
                              "owner_notification_label": "Не проверено."}
        self.connection.external_id = "446312402"
        self.connection.save(update_fields=["external_id"])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("communications_conversations"), {"conversation": self.conversation.uuid})
        self.assertContains(response, "SAFE-OWNER-PANEL-COMMAND")
        manager = User.objects.create_user("outbox-ui-command-manager")
        OrganizationAccess.objects.create(organization=self.organization, user=manager, role="manager")
        self.client.force_login(manager)
        response = self.client.get(reverse("communications_conversations"), {"conversation": self.conversation.uuid})
        self.assertContains(response, "Исходящие Авито")
        self.assertNotContains(response, "SAFE-OWNER-PANEL-COMMAND")
        self.assertNotContains(response, "Команда для текущего Service2")

    @patch("pool_service.communication_avito._json_request")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_conversation_get_shows_attempts_and_safe_failure_without_provider_requests(self, diagnostics, request):
        diagnostics.return_value = self.queue
        ConversationMessage.objects.create(
            conversation=self.conversation, direction="out", body="Test reply",
            delivery_status="failed", delivery_attempts=2, delivery_error="<script>SECRET</script>")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("communications_conversations"), {"conversation": self.conversation.uuid})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Исходящие Авито")
        self.assertContains(response, "Ошибка отправки")
        self.assertContains(response, "Попыток: 2")
        self.assertNotContains(response, "SECRET")
        request.assert_not_called()

    @patch("pool_service.avito_webhook_chain.display")
    @patch("pool_service.avito_outbox.outbox_diagnostics")
    def test_stale_warning_names_periodic_mechanism_and_does_not_invalidate_immediate_success(self, diagnostics, chain):
        diagnostics.return_value = {
            **self.queue, "periodic_state": "stale", "immediate_state": "healthy",
            "last_immediate_success_at": "2026-10-25T03:23:11+00:00",
            "last_periodic_success_at": None, "periodic_runs": [],
        }
        chain.return_value = {"state": "awaiting_first", "state_label": "Ожидается событие.",
                              "owner_notification_label": "Не проверено."}
        state = for_connection(self.connection)
        content = render_to_string("pool_service/communications/_avito_outbox.html", {"outbox": state})
        self.assertIn("Нет свежего подтверждения периодической обработки очереди", content)
        self.assertIn("Отправка после сохранения — последний запуск успешен", content)
        self.assertIn("25.10.2026 10:23:11", content)
        self.assertNotIn("Нет свежего подтверждения работы обработчика", content)
        self.assertNotIn("очередь пуста до и после", content)
