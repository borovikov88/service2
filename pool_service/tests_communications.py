from datetime import timedelta
import io
from importlib import import_module
import logging
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import AvitoCredential, CommunicationAccess, CommunicationChannel, ChannelConnection, Conversation, ConversationMessage, MessageAttachment, PhoneCall, TelephonyConnection, TelephonyEmployeeIdentity, WebsiteRequest
from pool_service.communication_avito import (
    AvitoError,
    AvitoRetryableError,
    AvitoSyncResult,
    _json_list_request,
    access_token,
    authorized_account_id,
    send_message,
    subscribe_webhook,
    sync_recent_messages,
    unsubscribe_webhook,
    verify_messenger_access,
    webhook_subscriptions,
)
from pool_service.communication_recordings import download_call_recording
from pool_service.communication_secrets import decrypt_secret, encrypt_secret
from pool_service.communication_services import receive_message, users_with_conversation_access
from pool_service.communication_services import conversation_capability
from pool_service.services.employee_identity_sync import (
    EmployeeIdentitySyncError,
    auto_link_service2_user,
    map_employee_service2_user,
    map_telephony_identity,
    resolve_call_employee,
    sync_all_employee_identities,
    sync_megafon_employee_identities,
    sync_onec_employee_identities,
)
from pool_service.communication_api import _payload
from pool_service.finance_imports.odata_profit import ODataConfig, ODataPreviewError
from pool_service.management.commands.send_avito_outbox import claim_message
from pool_service.models import Client as ServiceClient, Employee, EmployeeOneCIdentity, Notification, Organization, OrganizationAccess
from service_site.logging_handlers import RedactCommunicationWebhookSecretFilter


class CommunicationsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test communications org")
        self.owner = User.objects.create_user("owner", password="test")
        self.worker = User.objects.create_user("worker", password="test")
        self.other = User.objects.create_user("other", password="test")
        self.accountant = User.objects.create_user("accountant", password="test")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        OrganizationAccess.objects.create(user=self.worker, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.other, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.accountant, organization=self.organization, role="accountant")
        self.channel = CommunicationChannel.objects.create(organization=self.organization, kind="avito", name="Авито")
        self.connection = ChannelConnection.objects.create(channel=self.channel, name="Аккаунт 1", external_id="one")
        account_id_patcher = patch(
            "pool_service.communication_views.avito_authorized_account_id",
            return_value=self.connection.external_id,
        )
        messenger_access_patcher = patch(
            "pool_service.communication_views.avito_verify_messenger_access",
            return_value=True,
        )
        self.avito_authorized_account_id = account_id_patcher.start()
        self.avito_verify_messenger_access = messenger_access_patcher.start()
        self.addCleanup(account_id_patcher.stop)
        self.addCleanup(messenger_access_patcher.stop)

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_incoming_message_creates_conversation_and_notifications(self, _send_push):
        message, created = receive_message(connection=self.connection, external_conversation_id="chat-1", participant_name="Иван", body="Здравствуйте", external_message_id="message-1")
        self.assertTrue(created)
        self.assertEqual(message.conversation.last_message_at, message.created_at)
        self.assertEqual(Notification.objects.filter(kind="communication").count(), 3)
        _, duplicate = receive_message(connection=self.connection, external_conversation_id="chat-1", participant_name="Иван", body="Здравствуйте", external_message_id="message-1")
        self.assertFalse(duplicate)

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_accountant_role_transition_revokes_notifications(self, send_push):
        access = OrganizationAccess.objects.get(user=self.worker, organization=self.organization)
        access.role = "accountant"
        access.save(update_fields=["role"])
        capabilities = CommunicationAccess.objects.get(user=self.worker, organization=self.organization)
        self.assertFalse(capabilities.can_view_conversations)
        self.assertNotIn(self.worker, users_with_conversation_access(self.organization))

        receive_message(
            connection=self.connection,
            external_conversation_id="private-chat",
            participant_name="Клиент",
            body="Конфиденциальное сообщение",
            external_message_id="private-message",
        )
        self.assertFalse(Notification.objects.filter(user=self.worker, kind="communication").exists())
        pushed_users = {user.pk for call in send_push.call_args_list for user in call.args[0]}
        self.assertNotIn(self.worker.pk, pushed_users)

    def test_service_role_save_preserves_explicit_capabilities(self):
        service_user = User.objects.create_user("service-capability", password="test")
        role = OrganizationAccess.objects.create(
            user=service_user,
            organization=self.organization,
            role="service",
        )
        capabilities = CommunicationAccess.objects.get(user=service_user, organization=self.organization)
        self.assertFalse(capabilities.can_view_conversations)
        capabilities.can_view_conversations = True
        capabilities.can_reply_conversations = True
        capabilities.save(update_fields=["can_view_conversations", "can_reply_conversations"])

        role.save()
        capabilities.refresh_from_db()
        self.assertTrue(capabilities.can_view_conversations)
        self.assertTrue(capabilities.can_reply_conversations)

    def test_admin_downgrade_recomputes_inherited_capabilities(self):
        user = User.objects.create_user("downgraded-admin", password="test")
        role = OrganizationAccess.objects.create(
            user=user,
            organization=self.organization,
            role="admin",
        )
        capabilities = CommunicationAccess.objects.get(user=user, organization=self.organization)
        self.assertTrue(capabilities.can_manage_channels)
        self.assertTrue(capabilities.can_assign_conversation)
        self.assertTrue(capabilities.can_view_all_calls)

        role.role = "manager"
        role.save(update_fields=["role"])
        capabilities.refresh_from_db()

        self.assertTrue(capabilities.can_view_conversations)
        self.assertTrue(capabilities.can_reply_conversations)
        self.assertTrue(capabilities.can_take_conversation)
        self.assertTrue(capabilities.can_view_own_calls)
        self.assertTrue(capabilities.can_listen_calls)
        self.assertFalse(capabilities.can_manage_channels)
        self.assertFalse(capabilities.can_assign_conversation)
        self.assertFalse(capabilities.can_view_all_calls)

    def test_database_idempotency_allows_null_ids_but_rejects_duplicates(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="db-idempotency",
            participant_name="Иван",
        )
        ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Первый без provider id",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Второй без provider id",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        ConversationMessage.objects.create(
            conversation=conversation,
            external_id="provider-duplicate",
            direction=ConversationMessage.DIRECTION_IN,
            body="Первый",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConversationMessage.objects.create(
                    conversation=conversation,
                    external_id="provider-duplicate",
                    direction=ConversationMessage.DIRECTION_IN,
                    body="Дубликат",
                )

    def test_request_log_filter_redacts_avito_webhook_secret(self):
        secret = "super-secret-webhook-token"
        record = logging.LogRecord(
            name="django.request",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Internal Server Error: %s",
            args=(
                f"/api/communications/avito/123e4567-e89b-12d3-a456-426614174000/{secret}/webhook/",
            ),
            exc_info=None,
        )
        self.assertTrue(RedactCommunicationWebhookSecretFilter().filter(record))
        rendered = record.getMessage()
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_owner_sees_communications_in_current_desktop_and_mobile_navigation(self):
        self.client.login(username="owner", password="test")
        response = self.client.get(reverse("pool_list"))
        communications_url = reverse("communications_conversations")
        self.assertContains(
            response,
            f'href="{communications_url}" class="desktop-sidebar__link',
        )
        self.assertContains(
            response,
            f'href="{communications_url}" class="list-group-item list-group-item-action',
        )

    def test_calls_page_handles_unmapped_employee_and_uses_shared_navigation(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="megafon-main",
        )
        PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="unmapped-call",
            employee=None,
            contact_name="Клиент без сопоставленного сотрудника",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            duration_seconds=42,
            result=PhoneCall.RESULT_ANSWERED,
        )

        self.client.login(username="owner", password="test")
        calls_page = self.client.get(reverse("communications_calls"))
        self.assertEqual(calls_page.status_code, 200)
        self.assertContains(calls_page, "Не сопоставлен")
        self.assertContains(calls_page, 'bi bi-gear')
        self.assertContains(calls_page, reverse("communications_channels"))
        self.assertContains(calls_page, "communications-tabs")

        dialogs_page = self.client.get(reverse("communications_conversations"))
        self.assertEqual(dialogs_page.status_code, 200)
        self.assertContains(dialogs_page, 'bi bi-gear')
        self.assertContains(dialogs_page, reverse("communications_channels"))
        self.assertContains(dialogs_page, "communications-tabs")

    def test_calls_page_embeds_private_recording_player_and_supports_ranges(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="megafon-player",
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="stored-call",
            employee=self.owner,
            contact_name="Клиент",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            duration_seconds=12,
            result=PhoneCall.RESULT_ANSWERED,
            recording_ref="https://records.megapbx.ru/stored-call.mp3",
            recording_status=PhoneCall.RECORDING_STORED,
        )
        payload = b"ID3" + b"recording-bytes"
        call.recording_file.save(
            "stored-call.mp3",
            ContentFile(payload),
            save=True,
        )

        self.client.login(username="owner", password="test")
        page = self.client.get(reverse("communications_calls"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "<audio", html=False)
        self.assertContains(page, 'data-call-player', html=False)
        self.assertContains(page, 'data-call-seek', html=False)
        self.assertContains(page, 'data-call-speed', html=False)
        self.assertContains(page, 'value="0.5"', html=False)
        self.assertContains(page, 'value="1.25"', html=False)
        self.assertContains(page, 'value="1.5"', html=False)
        self.assertContains(page, 'value="2"', html=False)
        recording_url = reverse("communication_call_recording", args=[call.pk])
        self.assertContains(page, recording_url)
        self.assertContains(page, f"{recording_url}?download=1")
        self.assertNotContains(page, call.recording_ref)

        full = self.client.get(recording_url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full["Content-Type"], "audio/mpeg")
        self.assertEqual(full["Accept-Ranges"], "bytes")
        self.assertIn("inline", full["Content-Disposition"])

        partial = self.client.get(recording_url, HTTP_RANGE="bytes=3-7")
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial["Content-Range"], f"bytes 3-7/{len(payload)}")
        self.assertEqual(b"".join(partial.streaming_content), payload[3:8])

        download = self.client.get(
            f"{recording_url}?download=1",
            HTTP_RANGE="bytes=3-7",
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "audio/mpeg")
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertEqual(b"".join(download.streaming_content), payload)

    @override_settings(
        COMMUNICATION_RECORDING_DOWNLOAD_TIMEOUT_SECONDS=2,
        COMMUNICATION_RECORDING_MAX_BYTES=1024 * 1024,
    )
    def test_recording_downloader_saves_mp3_to_private_storage(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="megafon-download",
            recording_allowed_hosts=["records.megapbx.ru"],
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="download-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            duration_seconds=30,
            result=PhoneCall.RESULT_ANSWERED,
            recording_ref="https://records.megapbx.ru/download-call.mp3",
            recording_status=PhoneCall.RECORDING_PENDING,
        )
        payload = b"ID3" + b"x" * 128

        class FakeResponse(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.headers = {
                    "Content-Type": "audio/mpeg",
                    "Content-Length": str(len(data)),
                }

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        with patch(
            "pool_service.communication_recordings._open_recording",
            return_value=FakeResponse(payload),
        ):
            self.assertTrue(download_call_recording(call.pk))

        call.refresh_from_db()
        self.assertEqual(call.recording_status, PhoneCall.RECORDING_STORED)
        self.assertTrue(call.recording_file.name)
        self.assertEqual(call.recording_error, "")
        self.assertIsNotNone(call.recording_downloaded_at)
        with call.recording_file.open("rb") as stored:
            self.assertEqual(stored.read(), payload)

    def test_recording_downloader_rejects_html_instead_of_storing_login_page(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="megafon-html",
            recording_allowed_hosts=["records.megapbx.ru"],
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="html-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            duration_seconds=30,
            result=PhoneCall.RESULT_ANSWERED,
            recording_ref="https://records.megapbx.ru/html-call.mp3",
            recording_status=PhoneCall.RECORDING_PENDING,
        )

        class FakeHtmlResponse(io.BytesIO):
            def __init__(self):
                super().__init__(b"<html>login</html>")
                self.headers = {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Length": "18",
                }

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        with patch(
            "pool_service.communication_recordings._open_recording",
            return_value=FakeHtmlResponse(),
        ):
            self.assertFalse(download_call_recording(call.pk))

        call.refresh_from_db()
        self.assertEqual(call.recording_status, PhoneCall.RECORDING_FAILED)
        self.assertFalse(call.recording_file)
        self.assertIn("unexpected_content_type", call.recording_error)

    def test_manager_does_not_see_communications_navigation_during_rollout(self):
        self.client.login(username="worker", password="test")
        response = self.client.get(reverse("pool_list"))
        communications_url = reverse("communications_conversations")
        self.assertNotContains(
            response,
            f'href="{communications_url}" class="desktop-sidebar__link',
        )
        self.assertNotContains(
            response,
            f'href="{communications_url}" class="list-group-item list-group-item-action',
        )
        self.assertTrue(response.context["can_access_communications"])
        self.assertFalse(response.context["show_communications_menu"])

    def test_legacy_role_backfill_creates_defaults_without_overwriting_explicit_access(self):
        CommunicationAccess.objects.filter(
            user=self.owner,
            organization=self.organization,
        ).delete()
        worker_access = CommunicationAccess.objects.get(
            user=self.worker,
            organization=self.organization,
        )
        worker_access.can_manage_channels = True
        worker_access.save(update_fields=["can_manage_channels"])

        migration = import_module(
            "pool_service.migrations.0116_backfill_communication_access"
        )
        migration.backfill_communication_access(apps, None)

        owner_access = CommunicationAccess.objects.get(
            user=self.owner,
            organization=self.organization,
        )
        self.assertTrue(owner_access.can_view_conversations)
        self.assertTrue(owner_access.can_reply_conversations)
        self.assertTrue(owner_access.can_assign_conversation)
        self.assertTrue(owner_access.can_view_all_calls)
        self.assertTrue(owner_access.can_manage_channels)

        worker_access.refresh_from_db()
        self.assertTrue(worker_access.can_manage_channels)

    def test_first_worker_takes_conversation_atomically(self):
        conversation = Conversation.objects.create(organization=self.organization, connection=self.connection, external_id="chat", participant_name="Иван")
        other_notification = Notification.objects.create(
            user=self.other,
            organization=self.organization,
            kind="communication",
            title="Новое сообщение",
            action_url=f"/communications/?conversation={conversation.uuid}",
        )
        self.client.login(username="worker", password="test")
        response = self.client.post(reverse("communication_take", args=[conversation.uuid]))
        self.assertEqual(response.status_code, 302)
        conversation.refresh_from_db()
        self.assertEqual(conversation.assignee, self.worker)
        self.client.logout(); self.client.login(username="other", password="test")
        reconciled = self.client.get(
            reverse("communication_notification_feed"), {"visible": str(other_notification.pk)},
        ).json()
        self.assertEqual(reconciled["resolved_ids"], [other_notification.pk])
        self.assertEqual(self.client.post(reverse("communication_take", args=[conversation.uuid])).status_code, 403)

    def test_accountant_cannot_open_communications(self):
        permission = Permission.objects.get(codename="can_view_conversations")
        self.accountant.user_permissions.add(permission)
        self.client.login(username="accountant", password="test")
        self.assertFalse(conversation_capability(self.accountant, "can_view_conversations", self.organization))
        self.assertNotEqual(self.client.get(reverse("communications_conversations")).status_code, 200)

    def test_explicit_capability_deny_and_superuser_only_communication_admin(self):
        access = CommunicationAccess.objects.get(user=self.worker, organization=self.organization)
        access.can_view_conversations = False
        access.save(update_fields=["can_view_conversations"])
        self.client.login(username="worker", password="test")
        self.assertEqual(self.client.get(reverse("communications_conversations")).status_code, 403)
        self.worker.is_staff = True
        self.worker.save(update_fields=["is_staff"])
        self.worker.user_permissions.add(Permission.objects.get(codename="view_communicationchannel"))
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("admin:pool_service_communicationchannel_changelist")).status_code, 403)

    def test_only_channel_manager_can_change_scoped_channel_state(self):
        channel_url = reverse("communication_channel_set_active", args=[self.channel.pk])
        connection_url = reverse("communication_connection_set_active", args=[self.connection.pk])
        self.client.login(username="worker", password="test")
        self.assertEqual(self.client.post(channel_url, {"active": "0"}).status_code, 403)
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.is_active)

        self.client.logout()
        self.client.login(username="owner", password="test")
        self.assertEqual(self.client.get(channel_url).status_code, 405)
        self.assertEqual(self.client.post(channel_url, {"active": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(channel_url, {"active": "0"}).status_code, 302)
        self.assertEqual(self.client.post(connection_url, {"active": "0"}).status_code, 302)
        self.channel.refresh_from_db()
        self.connection.refresh_from_db()
        self.assertFalse(self.channel.is_active)
        self.assertFalse(self.connection.is_active)
        page = self.client.get(reverse("communications_channels"))
        self.assertContains(page, "Включить канал")
        self.assertContains(page, "Включить")

        csrf_client = Client(enforce_csrf_checks=True)
        self.assertTrue(csrf_client.login(username="owner", password="test"))
        csrf_client.get(reverse("communications_channels"))
        self.assertEqual(csrf_client.post(connection_url, {"active": "1"}).status_code, 403)
        csrf_token = csrf_client.cookies["csrftoken"].value
        self.assertEqual(
            csrf_client.post(connection_url, {"active": "1"}, HTTP_X_CSRFTOKEN=csrf_token).status_code,
            302,
        )
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.is_active)

        foreign_organization = Organization.objects.create(name="Foreign channels org")
        foreign_channel = CommunicationChannel.objects.create(
            organization=foreign_organization, kind="website", name="Foreign site",
        )
        foreign_connection = ChannelConnection.objects.create(
            channel=foreign_channel, name="Foreign widget", external_id="foreign-widget",
        )
        self.assertEqual(
            self.client.post(reverse("communication_channel_set_active", args=[foreign_channel.pk]), {"active": "0"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("communication_connection_set_active", args=[foreign_connection.pk]), {"active": "0"}).status_code,
            404,
        )
        foreign_channel.refresh_from_db()
        foreign_connection.refresh_from_db()
        self.assertTrue(foreign_channel.is_active)
        self.assertTrue(foreign_connection.is_active)

    def test_owner_can_create_website_connection_and_token_is_shown_once(self):
        self.client.login(username="owner", password="test")
        with patch("pool_service.communication_views.secrets.token_urlsafe", return_value="website-one-time-token"):
            response = self.client.post(
                reverse("communication_connection_create", args=["website"]),
                {
                    "name": "Основной сайт",
                    "external_id": "aqualine22.ru",
                    "is_active": "on",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "website-one-time-token")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        connection = ChannelConnection.objects.get(
            channel__organization=self.organization,
            channel__kind="website",
            external_id="aqualine22.ru",
        )
        self.assertTrue(connection.check_api_token("website-one-time-token"))
        edit_page = self.client.get(
            reverse("communication_connection_edit", args=[connection.pk])
        )
        self.assertEqual(edit_page.status_code, 200)
        self.assertNotContains(edit_page, "website-one-time-token")

    def test_owner_can_create_avito_connection_with_encrypted_credentials(self):
        self.client.login(username="owner", password="test")
        response = self.client.post(
            reverse("communication_connection_create", args=["avito"]),
            {
                "name": "Основной Авито",
                "external_id": "123456789",
                "client_id": "avito-client-id",
                "client_secret": "avito-client-secret",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = ChannelConnection.objects.get(
            channel=self.channel,
            external_id="123456789",
        )
        credential = AvitoCredential.objects.get(connection=connection)
        self.assertNotEqual(credential.client_id_encrypted, "avito-client-id")
        self.assertNotEqual(credential.client_secret_encrypted, "avito-client-secret")
        self.assertEqual(decrypt_secret(credential.client_id_encrypted), "avito-client-id")
        self.assertEqual(
            decrypt_secret(credential.client_secret_encrypted),
            "avito-client-secret",
        )
        self.assertEqual(connection.api_token_hash, "")
        self.assertEqual(connection.settings["avito_webhook_status"], "not_connected")

    @patch("pool_service.communication_views.avito_unsubscribe_webhook")
    @patch("pool_service.communication_views.avito_subscribe_webhook")
    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_owner_can_connect_avito_webhook_automatically(
        self, subscriptions, subscribe, unsubscribe
    ):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        callback = (
            f"https://testserver/api/communications/avito/"
            f"{self.connection.public_id}/new-webhook-token/webhook/"
        )
        subscriptions.side_effect = [[], [callback]]
        self.avito_authorized_account_id.return_value = "123456789"
        self.client.login(username="owner", password="test")
        with patch(
            "pool_service.communication_views.secrets.token_urlsafe",
            return_value="new-webhook-token",
        ):
            response = self.client.post(
                reverse("communication_avito_connect", args=[self.connection.pk]),
                secure=True,
            )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.external_id, "123456789")
        self.assertTrue(self.connection.check_api_token("new-webhook-token"))
        self.assertEqual(
            self.connection.settings["avito_webhook_status"], "connected"
        )
        subscribe.assert_called_once_with(self.connection, callback)
        unsubscribe.assert_not_called()

    @patch("pool_service.communication_views.avito_subscribe_webhook")
    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_avito_connect_reuses_existing_valid_subscription(
        self, subscriptions, subscribe
    ):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        self.connection.set_api_token("current-webhook-token")
        self.connection.save(update_fields=["api_token_hash"])
        callback = (
            f"https://testserver/api/communications/avito/"
            f"{self.connection.public_id}/current-webhook-token/webhook/"
        )
        subscriptions.return_value = [callback]
        self.client.login(username="owner", password="test")
        response = self.client.post(
            reverse("communication_avito_connect", args=[self.connection.pk]),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.check_api_token("current-webhook-token"))
        self.assertEqual(
            self.connection.settings["avito_webhook_status"], "connected"
        )
        subscribe.assert_not_called()

    @patch("pool_service.communication_views.avito_subscribe_webhook")
    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_avito_connect_restores_previous_token_on_failure(
        self, subscriptions, subscribe
    ):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        self.connection.set_api_token("old-webhook-token")
        self.connection.save(update_fields=["api_token_hash"])
        subscriptions.return_value = []
        subscribe.side_effect = AvitoError("provider_http_403")
        self.client.login(username="owner", password="test")
        with patch(
            "pool_service.communication_views.secrets.token_urlsafe",
            return_value="failed-webhook-token",
        ):
            response = self.client.post(
                reverse("communication_avito_connect", args=[self.connection.pk]),
                secure=True,
            )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.check_api_token("old-webhook-token"))
        self.assertFalse(self.connection.check_api_token("failed-webhook-token"))
        self.assertEqual(self.connection.settings["avito_webhook_status"], "error")

    @patch("pool_service.communication_views.avito_subscribe_webhook")
    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_avito_connect_keeps_new_token_when_subscribe_response_is_lost_but_subscription_exists(
        self, subscriptions, subscribe
    ):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        callback = (
            f"https://testserver/api/communications/avito/"
            f"{self.connection.public_id}/ambiguous-webhook-token/webhook/"
        )
        subscriptions.side_effect = [[], [callback]]
        subscribe.side_effect = AvitoError("provider_unavailable")
        self.client.login(username="owner", password="test")
        with patch(
            "pool_service.communication_views.secrets.token_urlsafe",
            return_value="ambiguous-webhook-token",
        ):
            response = self.client.post(
                reverse("communication_avito_connect", args=[self.connection.pk]),
                secure=True,
            )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.check_api_token("ambiguous-webhook-token"))
        self.assertEqual(
            self.connection.settings["avito_webhook_status"], "connected"
        )

    @patch("pool_service.communication_views.avito_unsubscribe_webhook")
    @patch("pool_service.communication_views.avito_subscribe_webhook")
    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_explicit_avito_reconnect_rotates_token_and_removes_stale_subscription(
        self, subscriptions, subscribe, unsubscribe
    ):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        self.connection.set_api_token("old-webhook-token")
        self.connection.settings = {"avito_webhook_status": "connected"}
        self.connection.save(update_fields=["api_token_hash", "settings"])
        old_callback = (
            f"https://testserver/api/communications/avito/"
            f"{self.connection.public_id}/old-webhook-token/webhook/"
        )
        new_callback = (
            f"https://testserver/api/communications/avito/"
            f"{self.connection.public_id}/new-webhook-token/webhook/"
        )
        subscriptions.side_effect = [[old_callback], [new_callback]]
        self.client.login(username="owner", password="test")
        with patch(
            "pool_service.communication_views.secrets.token_urlsafe",
            return_value="new-webhook-token",
        ):
            response = self.client.post(
                reverse("communication_avito_connect", args=[self.connection.pk]),
                {"force": "1"},
                secure=True,
            )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.check_api_token("old-webhook-token"))
        self.assertTrue(self.connection.check_api_token("new-webhook-token"))
        subscribe.assert_called_once_with(self.connection, new_callback)
        unsubscribe.assert_called_once_with(self.connection, old_callback)

    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_owner_can_check_avito_webhook(self, subscriptions):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        self.connection.set_api_token("current-webhook-token")
        self.connection.save(update_fields=["api_token_hash"])
        callback = (
            f"https://testserver/api/communications/avito/"
            f"{self.connection.public_id}/current-webhook-token/webhook/"
        )
        subscriptions.return_value = [callback]
        self.client.login(username="owner", password="test")
        response = self.client.post(
            reverse("communication_avito_check", args=[self.connection.pk]),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(
            self.connection.settings["avito_webhook_status"], "connected"
        )

    @patch("pool_service.communication_views.avito_webhook_subscriptions")
    def test_avito_check_surfaces_missing_messenger_access(self, subscriptions):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        self.avito_verify_messenger_access.side_effect = AvitoError("provider_http_402")
        self.client.login(username="owner", password="test")
        response = self.client.post(
            reverse("communication_avito_check", args=[self.connection.pk]),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings["avito_webhook_status"], "error")
        self.assertEqual(
            self.connection.settings["avito_webhook_error"], "provider_http_402"
        )
        subscriptions.assert_not_called()

    @patch("pool_service.communication_views.avito_sync_recent_messages")
    def test_owner_can_run_avito_pull_sync(self, syncer):
        syncer.return_value = AvitoSyncResult(
            chats_checked=2,
            messages_checked=4,
            messages_created=1,
            messages_existing=2,
            messages_skipped=1,
        )
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client"),
            client_secret_encrypted=encrypt_secret("secret"),
        )
        self.client.login(username="owner", password="test")
        response = self.client.post(
            reverse("communication_avito_sync", args=[self.connection.pk]),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        syncer.assert_called_once_with(self.connection)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.settings["avito_pull_last_checked_at"])
        self.assertEqual(self.connection.settings["avito_pull_last_created"], 1)
        self.assertEqual(self.connection.settings["avito_pull_last_existing"], 2)

    def test_manager_cannot_open_channel_setup_pages(self):
        self.client.login(username="worker", password="test")
        self.assertEqual(
            self.client.get(
                reverse("communication_connection_create", args=["website"])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("communication_connection_edit", args=[self.connection.pk])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("communication_telephony_create")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("communication_avito_connect", args=[self.connection.pk]),
                secure=True,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("communication_avito_check", args=[self.connection.pk]),
                secure=True,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("communication_avito_sync", args=[self.connection.pk]),
                secure=True,
            ).status_code,
            403,
        )

    def test_owner_can_create_and_edit_megafon_line_with_safe_recording_hosts(self):
        self.client.login(username="owner", password="test")
        created = self.client.post(
            reverse("communication_telephony_create"),
            {
                "name": "Мегафон офис",
                "external_id": "line-1",
                "ats_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "ats_api_key": "megafon-ats-secret",
                "recording_allowed_hosts": "records.megafon.example\nmedia.megafon.example",
                "is_active": "on",
            },
        )
        self.assertEqual(created.status_code, 302)
        line = TelephonyConnection.objects.get(
            organization=self.organization, external_id="line-1"
        )
        self.assertEqual(
            line.recording_allowed_hosts,
            ["records.megafon.example", "media.megafon.example"],
        )
        provider_connection = ChannelConnection.objects.get(
            channel__organization=self.organization,
            channel__kind=CommunicationChannel.KIND_MEGAFON,
            external_id="line-1",
        )
        self.assertEqual(
            provider_connection.settings["megafon_api_base_url"],
            "https://aqualine22.megapbx.ru/crmapi/v1",
        )
        encrypted_key = provider_connection.settings["megafon_api_key_encrypted"]
        self.assertNotEqual(encrypted_key, "megafon-ats-secret")
        self.assertEqual(decrypt_secret(encrypted_key), "megafon-ats-secret")

        invalid = self.client.post(
            reverse("communication_telephony_edit", args=[line.pk]),
            {
                "name": "Мегафон офис",
                "external_id": "line-1",
                "ats_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "ats_api_key": "",
                "recording_allowed_hosts": "https://records.example/path",
                "is_active": "on",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "только доменное имя")

    def test_owner_can_connect_megafon_vats_and_receive_call_history(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон офис",
            external_id="megafon-office",
        )
        ServiceClient.objects.create(
            organization=self.organization,
            name="Тестовый клиент",
            phone="+7 (900) 111-22-33",
        )
        megafon_channel = CommunicationChannel.objects.create(
            organization=self.organization,
            kind=CommunicationChannel.KIND_MEGAFON,
            name="МегаФон",
        )
        ChannelConnection.objects.create(
            channel=megafon_channel,
            name="МегаФон офис",
            external_id="megafon-office",
            settings={
                "megafon_api_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "megafon_api_key_encrypted": encrypt_secret("megafon-ats-secret"),
            },
        )
        self.client.login(username="owner", password="test")
        with patch(
            "pool_service.communication_views.secrets.token_urlsafe",
            return_value="megafon-crm-token",
        ):
            setup = self.client.post(
                reverse("communication_telephony_connect", args=[telephony.pk]),
                secure=True,
            )
        self.assertEqual(setup.status_code, 200)
        self.assertContains(setup, "megafon-crm-token")
        self.assertEqual(setup["Cache-Control"], "no-store")

        provider_connection = ChannelConnection.objects.get(
            channel__organization=self.organization,
            channel__kind=CommunicationChannel.KIND_MEGAFON,
            external_id="megafon-office",
        )
        self.assertTrue(provider_connection.check_api_token("megafon-crm-token"))

        webhook_url = reverse("megafon_webhook", args=[provider_connection.public_id])
        webhook_client = Client()
        with patch(
            "pool_service.communication_api.resolve_call_employee",
            wraps=resolve_call_employee,
        ) as locked_resolver:
            response = webhook_client.post(
                webhook_url,
                {
                    "cmd": "history",
                    "crm_token": "megafon-crm-token",
                    "callid": "call-123",
                    "phone": "+79001112233",
                    "type": "in",
                    "start": "2026-10-03 16:00:00",
                    "duration": "91",
                    "status": "Success",
                    "user": "worker",
                    "link": "https://records.megapbx.ru/call-123.mp3",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(locked_resolver.call_args.kwargs["lock_identity"])
        call = PhoneCall.objects.get(connection=telephony, external_id="call-123")
        self.assertEqual(call.employee, self.worker)
        self.assertEqual(call.contact_name, "Тестовый клиент")
        self.assertEqual(call.result, PhoneCall.RESULT_ANSWERED)
        self.assertEqual(call.duration_seconds, 91)
        self.assertEqual(call.started_at.isoformat(), "2026-10-03T09:00:00+00:00")
        self.assertEqual(call.recording_ref, "https://records.megapbx.ru/call-123.mp3")
        self.assertEqual(call.recording_status, PhoneCall.RECORDING_PENDING)
        telephony.refresh_from_db()
        self.assertIn("records.megapbx.ru", telephony.recording_allowed_hosts)
        provider_connection.refresh_from_db()
        self.assertTrue(provider_connection.settings["megafon_last_received_at"])

        live_event = webhook_client.post(
            webhook_url,
            {
                "cmd": "event",
                "crm_token": "megafon-crm-token",
                "callid": "event-123",
                "phone": "+79001112233",
                "type": "INCOMING",
                "ext": "601",
                "user": "worker",
                "direction": "in",
            },
        )
        self.assertEqual(live_event.status_code, 200)
        self.assertEqual(live_event.json()["contact_name"], "Тестовый клиент")
        provider_connection.refresh_from_db()
        self.assertEqual(
            provider_connection.settings["megafon_last_event_type"],
            "INCOMING",
        )
        self.assertEqual(
            provider_connection.settings["megafon_last_event_phone"],
            "+79001112233",
        )
        self.assertEqual(
            provider_connection.settings["megafon_last_event_user"],
            "worker",
        )
        self.assertEqual(
            provider_connection.settings["megafon_last_event_ext"],
            "601",
        )

        transferred = webhook_client.post(
            webhook_url,
            {
                "cmd": "event",
                "crm_token": "megafon-crm-token",
                "callid": "event-transfer-123",
                "phone": "+79001112233",
                "type": "TRANSFERRED",
                "ext": "602",
                "user": "worker",
                "direction": "in",
                "second_callid": "event-transfer-456",
            },
        )
        self.assertEqual(transferred.status_code, 200)
        provider_connection.refresh_from_db()
        self.assertEqual(
            provider_connection.settings["megafon_last_event_type"],
            "TRANSFERRED",
        )

        duplicate = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "call-123",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "92",
                "status": "Success",
                "user": "worker",
            },
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(
            PhoneCall.objects.filter(connection=telephony, external_id="call-123").count(),
            1,
        )
        call.refresh_from_db()
        self.assertEqual(call.duration_seconds, 92)
        self.assertEqual(
            call.recording_ref,
            "https://records.megapbx.ru/call-123.mp3",
        )

        old_profile = Employee.objects.create(
            organization=self.organization,
            display_name="Старый владелец звонка",
            is_active=True,
            user=self.worker,
        )
        new_profile = Employee.objects.create(
            organization=self.organization,
            display_name="Новый владелец extension",
            is_active=True,
            user=self.other,
        )
        call.employee_profile = old_profile
        call.employee = self.worker
        call.provider_extension = "999"
        call.provider_user = "worker"
        call.save(
            update_fields=[
                "employee_profile",
                "employee",
                "provider_extension",
                "provider_user",
            ]
        )
        current_identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=new_profile,
            raw_name="Новый владелец extension",
            normalized_name="новый владелец extension",
            extension="999",
            external_user="other",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )
        historical_event = webhook_client.post(
            webhook_url,
            {
                "cmd": "event",
                "crm_token": "megafon-crm-token",
                "callid": "call-123",
                "phone": "+79001112233",
                "type": "COMPLETED",
                "ext": "999",
                "user": "worker",
                "direction": "in",
            },
        )
        self.assertEqual(historical_event.status_code, 200)
        current_identity.refresh_from_db()
        self.assertEqual(current_identity.external_user, "other")
        self.assertFalse(current_identity.requires_manual_confirmation)
        replay = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "call-123",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "93",
                "status": "Success",
                "user": "worker",
                "ext": "999",
            },
        )
        self.assertEqual(replay.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.duration_seconds, 93)
        self.assertEqual(call.employee_profile, old_profile)
        self.assertEqual(call.employee, self.worker)
        current_identity.refresh_from_db()
        self.assertEqual(current_identity.external_user, "other")
        self.assertFalse(current_identity.requires_manual_confirmation)
        self.assertEqual(
            current_identity.status,
            TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
        )

        replay_without_provider_keys = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "call-123",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "95",
                "status": "Success",
            },
        )
        self.assertEqual(replay_without_provider_keys.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.provider_extension, "999")
        self.assertEqual(call.provider_user, "worker")
        self.assertEqual(call.employee_profile, old_profile)
        self.assertEqual(call.employee, self.worker)

        legacy_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="legacy-replayed-call",
            employee=self.worker,
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )
        legacy_replay = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "legacy-replayed-call",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "94",
                "status": "Success",
                "user": "other",
                "ext": "999",
            },
        )
        self.assertEqual(legacy_replay.status_code, 200)
        legacy_call.refresh_from_db()
        self.assertEqual(legacy_call.employee_profile, new_profile)
        self.assertEqual(legacy_call.employee, self.other)
        self.assertEqual(legacy_call.provider_extension, "999")
        self.assertEqual(legacy_call.provider_user, "other")

        current_identity.is_active = False
        current_identity.save(update_fields=["is_active", "updated_at"])
        inactive_legacy_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="inactive-identity-legacy-replay",
            employee=self.worker,
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )
        inactive_replay = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "inactive-identity-legacy-replay",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "30",
                "status": "Success",
                "user": "other",
                "ext": "999",
            },
        )
        self.assertEqual(inactive_replay.status_code, 200)
        inactive_legacy_call.refresh_from_db()
        self.assertEqual(inactive_legacy_call.employee_profile, new_profile)
        self.assertEqual(inactive_legacy_call.employee, self.other)
        current_identity.is_active = True
        current_identity.save(update_fields=["is_active", "updated_at"])

        unassigned_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="unassigned-historical-call",
            provider_extension="999",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now() - timedelta(days=1),
            result=PhoneCall.RESULT_ANSWERED,
        )
        unassigned_replay = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "unassigned-historical-call",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-02 16:00:00",
                "duration": "60",
                "status": "Success",
                "ext": "999",
            },
        )
        self.assertEqual(unassigned_replay.status_code, 200)
        unassigned_call.refresh_from_db()
        self.assertIsNone(unassigned_call.employee_profile)
        self.assertIsNone(unassigned_call.employee)

        pending_profile = Employee.objects.create(
            organization=self.organization,
            display_name="Ожидает подтверждения",
            is_active=True,
            user=self.accountant,
        )
        TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=pending_profile,
            raw_name="Ожидает подтверждения",
            normalized_name="ожидает подтверждения",
            extension="998",
            external_user="accountant",
            is_active=True,
            requires_manual_confirmation=True,
            status=TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
            match_method=TelephonyEmployeeIdentity.MATCH_NONE,
        )
        pending_history = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "pending-extensionless-call",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:05:00",
                "duration": "10",
                "status": "Success",
                "user": "accountant",
            },
        )
        self.assertEqual(pending_history.status_code, 200)
        pending_call = PhoneCall.objects.get(
            connection=telephony,
            external_id="pending-extensionless-call",
        )
        self.assertIsNone(pending_call.employee_profile)
        self.assertIsNone(pending_call.employee)

        unauthorized = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "wrong-token",
                "callid": "call-unauthorized",
                "phone": "+79001112233",
                "type": "in",
            },
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertFalse(
            PhoneCall.objects.filter(connection=telephony, external_id="call-unauthorized").exists()
        )

        oversized_user = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "call-long-user",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "1",
                "status": "Success",
                "user": "u" * 256,
                "ext": "601",
            },
        )
        self.assertEqual(oversized_user.status_code, 400)
        self.assertEqual(oversized_user.json()["error"], "invalid_user")
        self.assertFalse(
            PhoneCall.objects.filter(
                connection=telephony,
                external_id="call-long-user",
            ).exists()
        )

        oversized_ext = webhook_client.post(
            webhook_url,
            {
                "cmd": "history",
                "crm_token": "megafon-crm-token",
                "callid": "call-long-ext",
                "phone": "+79001112233",
                "type": "in",
                "start": "2026-10-03 16:00:00",
                "duration": "1",
                "status": "Success",
                "user": "worker",
                "ext": "6" * 65,
            },
        )
        self.assertEqual(oversized_ext.status_code, 400)
        self.assertEqual(oversized_ext.json()["error"], "invalid_ext")
        self.assertFalse(
            PhoneCall.objects.filter(
                connection=telephony,
                external_id="call-long-ext",
            ).exists()
        )

    def test_megafon_account_sync_auto_matches_employee_and_backfills_calls(self):
        self.worker.first_name = "Дарья"
        self.worker.last_name = "Крафт"
        self.worker.save(update_fields=["first_name", "last_name"])
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Крафт Дарья Валерьевна",
            first_name="Дарья",
            last_name="Крафт",
            middle_name="Валерьевна",
            is_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон офис",
            external_id="megafon-sync",
        )
        channel = CommunicationChannel.objects.create(
            organization=self.organization,
            kind=CommunicationChannel.KIND_MEGAFON,
            name="МегаФон sync",
        )
        provider = ChannelConnection.objects.create(
            channel=channel,
            name="МегаФон офис",
            external_id="megafon-sync",
            settings={
                "megafon_api_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "megafon_api_key_encrypted": encrypt_secret("secret"),
            },
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="sync-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
            provider_extension="601",
        )

        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            return_value=(provider, [{"name": "Крафт Дарья Валерьевна", "ext": "601"}]),
        ):
            result = sync_megafon_employee_identities(telephony, actor=self.owner)

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["auto_matched"], 1)
        identity = TelephonyEmployeeIdentity.objects.get(
            connection=telephony,
            extension="601",
        )
        self.assertEqual(identity.employee, employee)
        self.assertEqual(
            identity.status,
            TelephonyEmployeeIdentity.STATUS_AUTO_MATCHED,
        )
        employee.refresh_from_db()
        self.assertEqual(employee.user, self.worker)
        call.refresh_from_db()
        self.assertEqual(call.employee_profile, employee)
        self.assertEqual(call.employee, self.worker)

    def test_service2_auto_link_requires_employee_side_uniqueness(self):
        self.worker.first_name = "Иван"
        self.worker.last_name = "Иванов"
        self.worker.save(update_fields=["first_name", "last_name"])
        first = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван Петрович",
            first_name="Иван",
            last_name="Иванов",
            middle_name="Петрович",
            is_active=True,
        )
        second = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван Сергеевич",
            first_name="Иван",
            last_name="Иванов",
            middle_name="Сергеевич",
            is_active=True,
        )

        self.assertFalse(auto_link_service2_user(first, actor=self.owner))
        self.assertFalse(auto_link_service2_user(second, actor=self.owner))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.user)
        self.assertIsNone(second.user)

    def test_service2_auto_link_skips_a_unique_constraint_race(self):
        self.worker.first_name = "Дарья"
        self.worker.last_name = "Крафт"
        self.worker.save(update_fields=["first_name", "last_name"])
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Крафт Дарья Валерьевна",
            first_name="Дарья",
            last_name="Крафт",
            middle_name="Валерьевна",
            is_active=True,
        )

        with patch.object(
            Employee,
            "save",
            side_effect=IntegrityError("unique_employee_user_per_org"),
        ):
            linked = auto_link_service2_user(employee, actor=self.owner)

        self.assertFalse(linked)
        employee.refresh_from_db()
        self.assertIsNone(employee.user)

    def test_onec_employee_sync_uses_stable_ids_and_source_activity(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Крафт Дарья Валерьевна",
            is_active=True,
        )
        config = ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="user",
            password="pass",
            organization_guids=("11111111-1111-1111-1111-111111111111",),
            timeout_seconds=5,
            max_pages=10,
            max_rows=100,
        )
        legacy_identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name="Сотрудник только из старого импорта",
            normalized_name="сотрудник только из старого импорта",
            source_identity_key="legacy-unseen-employee",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            source_active=True,
        )
        rows = [
            {
                "Ref_Key": "22222222-2222-2222-2222-222222222222",
                "Code": "000000017",
                "Description": "Крафт Дарья Валерьевна",
                "DeletionMark": False,
                "ВАрхиве": False,
                "Недействителен": False,
                "ГоловнаяОрганизация_Key": "11111111-1111-1111-1111-111111111111",
            },
            {
                "Ref_Key": "33333333-3333-3333-3333-333333333333",
                "Code": "000000099",
                "Description": "Старый Сотрудник",
                "DeletionMark": False,
                "ВАрхиве": False,
                "Недействителен": True,
                "ГоловнаяОрганизация_Key": "11111111-1111-1111-1111-111111111111",
            },
        ]

        with patch(
            "pool_service.services.employee_identity_sync.is_odata_target_organization",
            return_value=True,
        ), patch(
            "pool_service.services.employee_identity_sync.config_from_settings",
            return_value=config,
        ), patch(
            "pool_service.services.employee_identity_sync.read_odata_pages",
            return_value=iter([(rows, 1)]),
        ):
            result = sync_onec_employee_identities(
                self.organization,
                actor=self.owner,
            )

        self.assertEqual(result["synced"], 2)
        self.assertEqual(result["active"], 1)
        self.assertEqual(result["inactive"], 1)
        active_identity = EmployeeOneCIdentity.objects.get(
            onec_employee_id="22222222-2222-2222-2222-222222222222"
        )
        self.assertEqual(active_identity.employee, employee)
        self.assertTrue(active_identity.source_active)
        self.assertEqual(active_identity.personnel_number, "000000017")
        inactive_identity = EmployeeOneCIdentity.objects.get(
            onec_employee_id="33333333-3333-3333-3333-333333333333"
        )
        self.assertFalse(inactive_identity.source_active)
        self.assertIsNotNone(inactive_identity.last_seen_at)
        legacy_identity.refresh_from_db()
        self.assertFalse(legacy_identity.source_active)
        self.assertIsNone(legacy_identity.last_seen_at)

    def test_reused_inactive_extension_requires_manual_remapping(self):
        old_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Старый Сотрудник",
            is_active=True,
            user=self.worker,
        )
        Employee.objects.create(
            organization=self.organization,
            display_name="Новый Сотрудник",
            is_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="reused-ext",
        )
        channel = CommunicationChannel.objects.create(
            organization=self.organization,
            kind=CommunicationChannel.KIND_MEGAFON,
            name="МегаФон reused",
        )
        provider = ChannelConnection.objects.create(
            channel=channel,
            name="МегаФон reused",
            external_id="reused-ext",
            settings={
                "megafon_api_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "megafon_api_key_encrypted": encrypt_secret("secret"),
            },
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=old_employee,
            raw_name="Старый Сотрудник",
            normalized_name="старый сотрудник",
            extension="880",
            is_active=False,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
            confirmed_by=self.owner,
            confirmed_at=timezone.now(),
        )
        old_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="old-holder-call",
            employee=self.worker,
            employee_profile=old_employee,
            provider_extension="880",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now() - timedelta(days=30),
            result=PhoneCall.RESULT_ANSWERED,
        )
        new_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="new-holder-call",
            provider_extension="880",
            phone_number="+79001112234",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )

        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            return_value=(provider, [{"name": "Новый Сотрудник", "ext": "880"}]),
        ):
            result = sync_megafon_employee_identities(telephony, actor=self.owner)

        identity.refresh_from_db()
        self.assertEqual(identity.employee, old_employee)
        self.assertTrue(identity.requires_manual_confirmation)
        self.assertIsNotNone(identity.reassignment_detected_at)
        self.assertEqual(
            identity.status,
            TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
        )
        self.assertEqual(result["auto_matched"], 0)
        self.assertEqual(result["needs_mapping"], 1)

        # A later hourly sync must not silently auto-match the new holder.
        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            return_value=(provider, [{"name": "Новый Сотрудник", "ext": "880"}]),
        ):
            second_result = sync_megafon_employee_identities(
                telephony,
                actor=self.owner,
            )
        identity.refresh_from_db()
        self.assertEqual(identity.employee, old_employee)
        self.assertTrue(identity.requires_manual_confirmation)
        self.assertEqual(second_result["auto_matched"], 0)
        self.assertEqual(second_result["needs_mapping"], 1)

        old_call.refresh_from_db()
        new_call.refresh_from_db()
        self.assertEqual(old_call.employee_profile, old_employee)
        self.assertEqual(old_call.employee, self.worker)
        self.assertIsNone(new_call.employee_profile)
        self.assertIsNone(new_call.employee)

    def test_active_extension_name_change_requires_manual_remapping(self):
        old_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Старый Владелец",
            is_active=True,
            user=self.worker,
        )
        Employee.objects.create(
            organization=self.organization,
            display_name="Новый Владелец",
            is_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="active-reassigned-ext",
        )
        channel = CommunicationChannel.objects.create(
            organization=self.organization,
            kind=CommunicationChannel.KIND_MEGAFON,
            name="МегаФон active reassigned",
        )
        provider = ChannelConnection.objects.create(
            channel=channel,
            name="МегаФон active reassigned",
            external_id="active-reassigned-ext",
            settings={
                "megafon_api_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "megafon_api_key_encrypted": encrypt_secret("secret"),
            },
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=old_employee,
            raw_name="Старый Владелец",
            normalized_name="старый владелец",
            extension="882",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )

        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            return_value=(provider, [{"name": "Новый Владелец", "ext": "882"}]),
        ):
            result = sync_megafon_employee_identities(telephony, actor=self.owner)

        identity.refresh_from_db()
        self.assertEqual(identity.employee, old_employee)
        self.assertTrue(identity.requires_manual_confirmation)
        self.assertIsNotNone(identity.reassignment_detected_at)
        self.assertEqual(
            identity.status,
            TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
        )
        self.assertEqual(result["auto_matched"], 0)
        self.assertEqual(result["needs_mapping"], 1)

    def test_accounts_snapshot_preserves_identity_seen_after_snapshot_started(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон snapshot boundary",
            external_id="snapshot-boundary",
        )
        channel = CommunicationChannel.objects.create(
            organization=self.organization,
            kind=CommunicationChannel.KIND_MEGAFON,
            name="МегаФон snapshot boundary",
        )
        provider = ChannelConnection.objects.create(
            channel=channel,
            name="МегаФон snapshot boundary",
            external_id="snapshot-boundary",
            settings={
                "megafon_api_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "megafon_api_key_encrypted": encrypt_secret("secret"),
            },
        )
        old_seen_at = timezone.now() - timedelta(days=1)
        recently_seen = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            raw_name="Поздний webhook",
            normalized_name="поздний webhook",
            extension="885",
            is_active=True,
            last_seen_at=old_seen_at,
        )
        stale = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            raw_name="Старая строка",
            normalized_name="старая строка",
            extension="886",
            is_active=True,
            last_seen_at=old_seen_at,
        )

        def accounts_response_after_webhook(_telephony):
            TelephonyEmployeeIdentity.objects.filter(pk=recently_seen.pk).update(
                is_active=True,
                last_seen_at=timezone.now(),
            )
            return provider, []

        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            side_effect=accounts_response_after_webhook,
        ):
            sync_megafon_employee_identities(telephony, actor=self.owner)

        recently_seen.refresh_from_db()
        stale.refresh_from_db()
        self.assertTrue(recently_seen.is_active)
        self.assertFalse(stale.is_active)

    def test_inactive_extension_webhook_waits_for_accounts_revalidation(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван Иванович",
            is_active=True,
            user=self.worker,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="reactivated-ext",
        )
        channel = CommunicationChannel.objects.create(
            organization=self.organization,
            kind=CommunicationChannel.KIND_MEGAFON,
            name="МегаФон reactivated",
        )
        provider = ChannelConnection.objects.create(
            channel=channel,
            name="МегаФон reactivated",
            external_id="reactivated-ext",
            settings={
                "megafon_api_base_url": "https://aqualine22.megapbx.ru/crmapi/v1",
                "megafon_api_key_encrypted": encrypt_secret("secret"),
            },
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Иванов Иван Иванович",
            normalized_name="иванов иван иванович",
            extension="881",
            is_active=False,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="reactivated-call",
            provider_extension="881",
            phone_number="+79001112235",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )

        profile, user = resolve_call_employee(
            self.organization,
            telephony,
            "881",
            "worker",
        )
        self.assertIsNone(profile)
        self.assertIsNone(user)
        identity.refresh_from_db()
        self.assertTrue(identity.is_active)
        self.assertTrue(identity.requires_manual_confirmation)
        self.assertEqual(
            TelephonyEmployeeIdentity.objects.filter(
                connection=telephony,
                extension="881",
            ).count(),
            1,
        )

        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            return_value=(
                provider,
                [{"name": "Иванов Иван Иванович", "ext": "881"}],
            ),
        ):
            result = sync_megafon_employee_identities(telephony, actor=self.owner)

        identity.refresh_from_db()
        call.refresh_from_db()
        self.assertFalse(identity.requires_manual_confirmation)
        self.assertEqual(identity.employee, employee)
        self.assertEqual(result["auto_matched"], 1)
        self.assertEqual(call.employee_profile, employee)
        self.assertEqual(call.employee, self.worker)

        changed_identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Иванов Иван Иванович",
            normalized_name="иванов иван иванович",
            extension="884",
            external_user="old-provider-user",
            is_active=False,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )

        changed_profile, changed_user = resolve_call_employee(
            self.organization,
            telephony,
            "884",
            "new-provider-user",
        )
        self.assertIsNone(changed_profile)
        self.assertIsNone(changed_user)
        changed_identity.refresh_from_db()
        self.assertTrue(changed_identity.requires_manual_confirmation)
        self.assertEqual(
            changed_identity.status,
            TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
        )

        with patch(
            "pool_service.services.employee_identity_sync._read_megafon_accounts",
            return_value=(
                provider,
                [{"name": "Иванов Иван Иванович", "ext": "884"}],
            ),
        ):
            changed_result = sync_megafon_employee_identities(
                telephony,
                actor=self.owner,
            )

        changed_identity.refresh_from_db()
        self.assertTrue(changed_identity.requires_manual_confirmation)
        self.assertEqual(
            changed_identity.status,
            TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
        )
        self.assertEqual(changed_identity.external_user, "new-provider-user")
        self.assertEqual(changed_result["auto_matched"], 0)
        self.assertEqual(changed_result["needs_mapping"], 1)

    def test_full_employee_sync_isolates_unconfigured_telephony_lines(self):
        configured = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Настроенная линия",
            external_id="configured-line",
        )
        broken = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Старая линия без ключа",
            external_id="broken-line",
        )

        def fake_megafon_sync(telephony, actor=None):
            if telephony.pk == broken.pk:
                raise EmployeeIdentitySyncError("Ключ АТС не настроен.")
            return {
                "synced": 3,
                "auto_matched": 2,
                "needs_mapping": 1,
                "synced_at": timezone.now(),
            }

        with patch(
            "pool_service.services.employee_identity_sync.sync_onec_employee_identities",
            return_value={
                "synced": 5,
                "active": 4,
                "inactive": 1,
                "auto_linked_users": 0,
                "synced_at": timezone.now(),
            },
        ), patch(
            "pool_service.services.employee_identity_sync.sync_megafon_employee_identities",
            side_effect=fake_megafon_sync,
        ):
            result = sync_all_employee_identities(
                self.organization,
                actor=self.owner,
            )

        by_id = {
            item["connection_id"]: item for item in result["telephony"]
        }
        self.assertEqual(by_id[configured.pk]["synced"], 3)
        self.assertEqual(by_id[configured.pk]["error"], "")
        self.assertEqual(by_id[broken.pk]["synced"], 0)
        self.assertIn("Ключ АТС", by_id[broken.pk]["error"])

    def test_employee_identity_command_fails_after_reporting_line_errors(self):
        result = {
            "onec": {"synced": 3},
            "telephony": [
                {
                    "name": "Недоступная линия",
                    "synced": 0,
                    "needs_mapping": 0,
                    "error": "МегаФон не ответил",
                }
            ],
        }
        output = io.StringIO()
        errors = io.StringIO()

        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ), patch(
            "pool_service.management.commands.sync_employee_identities.sync_all_employee_identities",
            return_value=result,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "sync_employee_identities",
                    stdout=output,
                    stderr=errors,
                )

        self.assertIn("MegaFon_errors=1", output.getvalue())
        self.assertIn("Недоступная линия", errors.getvalue())

    def test_full_employee_sync_continues_megafon_after_onec_failure(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Линия после сбоя 1С",
            external_id="line-after-onec-failure",
        )
        with patch(
            "pool_service.services.employee_identity_sync.sync_onec_employee_identities",
            side_effect=EmployeeIdentitySyncError("1С временно недоступна"),
        ), patch(
            "pool_service.services.employee_identity_sync.sync_megafon_employee_identities",
            return_value={
                "synced": 2,
                "auto_matched": 1,
                "needs_mapping": 1,
                "synced_at": timezone.now(),
            },
        ) as megafon_sync:
            result = sync_all_employee_identities(
                self.organization,
                actor=self.owner,
            )

        megafon_sync.assert_called_once_with(telephony, actor=self.owner)
        self.assertEqual(result["onec"]["synced"], 0)
        self.assertIn("1С временно недоступна", result["onec"]["error"])
        self.assertEqual(result["telephony"][0]["synced"], 2)

    def test_employee_identity_command_reports_onec_error_after_other_sources(self):
        result = {
            "onec": {"synced": 0, "error": "1С временно недоступна"},
            "telephony": [
                {
                    "name": "Рабочая линия",
                    "synced": 2,
                    "needs_mapping": 0,
                    "error": "",
                }
            ],
        }
        output = io.StringIO()
        errors = io.StringIO()

        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ), patch(
            "pool_service.management.commands.sync_employee_identities.sync_all_employee_identities",
            return_value=result,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "sync_employee_identities",
                    stdout=output,
                    stderr=errors,
                )

        self.assertIn("1C_errors=1", output.getvalue())
        self.assertIn("MegaFon=2", output.getvalue())
        self.assertIn("1С временно недоступна", errors.getvalue())

    def test_manual_megafon_sync_isolates_failed_lines(self):
        configured = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Рабочая линия",
            external_id="manual-configured-line",
        )
        broken = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Линия без ключа",
            external_id="manual-broken-line",
        )
        self.organization.paid_until = timezone.now() + timedelta(days=30)
        self.organization.save(update_fields=["paid_until"])
        self.client.login(username="owner", password="test")

        def fake_sync(telephony, actor=None):
            if telephony.pk == broken.pk:
                raise EmployeeIdentitySyncError("Ключ АТС не настроен.")
            return {
                "synced": 4,
                "auto_matched": 3,
                "needs_mapping": 1,
                "synced_at": timezone.now(),
            }

        with patch(
            "pool_service.finance_views.sync_megafon_employee_identities",
            side_effect=fake_sync,
        ) as sync_mock:
            response = self.client.post(
                reverse("finance_employee_identity_sync"),
                {"source": "megafon"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sync_mock.call_count, 2)
        self.assertContains(response, "МегаФон: получено 4 сотрудников")
        self.assertContains(response, "Линия без ключа")
        self.assertContains(response, "Ключ АТС не настроен")

    def test_onec_employee_sync_rolls_back_when_later_page_fails(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Крафт Дарья Валерьевна",
            is_active=True,
        )
        existing = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=employee,
            raw_name="Старая запись",
            normalized_name="старая запись",
            source_identity_key="atomic-existing",
            status=EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
            match_method=EmployeeOneCIdentity.MATCH_MANUAL,
            source_active=True,
        )
        config = ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="user",
            password="pass",
            organization_guids=("11111111-1111-1111-1111-111111111111",),
            timeout_seconds=5,
            max_pages=10,
            max_rows=100,
        )
        first_page = [{
            "Ref_Key": "44444444-4444-4444-4444-444444444444",
            "Code": "000000018",
            "Description": "Новая Запись",
            "DeletionMark": False,
            "ВАрхиве": False,
            "Недействителен": False,
            "ГоловнаяОрганизация_Key": "11111111-1111-1111-1111-111111111111",
        }]

        def broken_pages(*_args, **_kwargs):
            yield first_page, 1
            raise ODataPreviewError("late page failure")

        with patch(
            "pool_service.services.employee_identity_sync.is_odata_target_organization",
            return_value=True,
        ), patch(
            "pool_service.services.employee_identity_sync.config_from_settings",
            return_value=config,
        ), patch(
            "pool_service.services.employee_identity_sync.read_odata_pages",
            side_effect=broken_pages,
        ):
            with self.assertRaises(EmployeeIdentitySyncError):
                sync_onec_employee_identities(self.organization, actor=self.owner)

        self.assertFalse(
            EmployeeOneCIdentity.objects.filter(
                onec_employee_id="44444444-4444-4444-4444-444444444444"
            ).exists()
        )
        existing.refresh_from_db()
        self.assertTrue(existing.source_active)

    def test_onec_employee_sync_rejects_malformed_deletion_flags(self):
        existing = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name="Существующий сотрудник",
            normalized_name="существующий сотрудник",
            source_identity_key="malformed-deletion-existing",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            source_active=True,
        )
        config = ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="user",
            password="pass",
            organization_guids=("11111111-1111-1111-1111-111111111111",),
            timeout_seconds=5,
            max_pages=10,
            max_rows=100,
        )
        base_row = {
            "Ref_Key": "55555555-5555-5555-5555-555555555555",
            "Code": "000000019",
            "Description": "Некорректная Запись",
            "ВАрхиве": False,
            "Недействителен": False,
            "ГоловнаяОрганизация_Key": "11111111-1111-1111-1111-111111111111",
        }

        for deletion_mark in (None, "False"):
            row = dict(base_row)
            if deletion_mark is not None:
                row["DeletionMark"] = deletion_mark
            with self.subTest(deletion_mark=deletion_mark), patch(
                "pool_service.services.employee_identity_sync.is_odata_target_organization",
                return_value=True,
            ), patch(
                "pool_service.services.employee_identity_sync.config_from_settings",
                return_value=config,
            ), patch(
                "pool_service.services.employee_identity_sync.read_odata_pages",
                return_value=iter([([row], 1)]),
            ):
                with self.assertRaises(EmployeeIdentitySyncError):
                    sync_onec_employee_identities(
                        self.organization,
                        actor=self.owner,
                    )

            existing.refresh_from_db()
            self.assertTrue(existing.source_active)
            self.assertFalse(
                EmployeeOneCIdentity.objects.filter(
                    onec_employee_id="55555555-5555-5555-5555-555555555555"
                ).exists()
            )

    def test_onec_employee_sync_rejects_out_of_scope_organization(self):
        existing = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name="Существующий сотрудник",
            normalized_name="существующий сотрудник",
            source_identity_key="foreign-organization-existing",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            source_active=True,
        )
        config = ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="user",
            password="pass",
            organization_guids=("11111111-1111-1111-1111-111111111111",),
            timeout_seconds=5,
            max_pages=10,
            max_rows=100,
        )
        row = {
            "Ref_Key": "66666666-6666-6666-6666-666666666666",
            "Code": "000000020",
            "Description": "Чужая Организация",
            "DeletionMark": False,
            "ВАрхиве": False,
            "Недействителен": False,
            "ГоловнаяОрганизация_Key": "22222222-2222-2222-2222-222222222222",
        }

        with patch(
            "pool_service.services.employee_identity_sync.is_odata_target_organization",
            return_value=True,
        ), patch(
            "pool_service.services.employee_identity_sync.config_from_settings",
            return_value=config,
        ), patch(
            "pool_service.services.employee_identity_sync.read_odata_pages",
            return_value=iter([([row], 1)]),
        ):
            with self.assertRaises(EmployeeIdentitySyncError):
                sync_onec_employee_identities(
                    self.organization,
                    actor=self.owner,
                )

        existing.refresh_from_db()
        self.assertTrue(existing.source_active)
        self.assertFalse(
            EmployeeOneCIdentity.objects.filter(
                onec_employee_id="66666666-6666-6666-6666-666666666666"
            ).exists()
        )

    def test_onec_employee_sync_enforces_configured_row_limit(self):
        existing = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name="Существующий сотрудник",
            normalized_name="существующий сотрудник",
            source_identity_key="row-limit-existing",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            source_active=True,
        )
        config = ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="user",
            password="pass",
            organization_guids=("11111111-1111-1111-1111-111111111111",),
            timeout_seconds=5,
            max_pages=10,
            max_rows=1,
        )
        rows = [
            {
                "Ref_Key": employee_guid,
                "Code": code,
                "Description": name,
                "DeletionMark": False,
                "ВАрхиве": False,
                "Недействителен": False,
                "ГоловнаяОрганизация_Key": "11111111-1111-1111-1111-111111111111",
            }
            for employee_guid, code, name in (
                (
                    "77777777-7777-7777-7777-777777777777",
                    "000000021",
                    "Первая Запись",
                ),
                (
                    "88888888-8888-8888-8888-888888888888",
                    "000000022",
                    "Вторая Запись",
                ),
            )
        ]

        with patch(
            "pool_service.services.employee_identity_sync.is_odata_target_organization",
            return_value=True,
        ), patch(
            "pool_service.services.employee_identity_sync.config_from_settings",
            return_value=config,
        ), patch(
            "pool_service.services.employee_identity_sync.read_odata_pages",
            return_value=iter([(rows, 1)]),
        ):
            with self.assertRaises(EmployeeIdentitySyncError):
                sync_onec_employee_identities(
                    self.organization,
                    actor=self.owner,
                )

        existing.refresh_from_db()
        self.assertTrue(existing.source_active)
        self.assertFalse(
            EmployeeOneCIdentity.objects.filter(
                onec_employee_id__in={
                    "77777777-7777-7777-7777-777777777777",
                    "88888888-8888-8888-8888-888888888888",
                }
            ).exists()
        )

    def test_active_extension_provider_user_change_blocks_new_call_assignment(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван Иванович",
            is_active=True,
            user=self.worker,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="changed-provider-user",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Иванов Иван Иванович",
            normalized_name="иванов иван иванович",
            extension="883",
            external_user="old-user",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )

        profile, user = resolve_call_employee(
            self.organization,
            telephony,
            "883",
            "new-user",
        )

        self.assertIsNone(profile)
        self.assertIsNone(user)
        identity.refresh_from_db()
        self.assertEqual(identity.employee, employee)
        self.assertEqual(identity.external_user, "new-user")
        self.assertTrue(identity.requires_manual_confirmation)
        self.assertEqual(
            identity.status,
            TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
        )

    def test_active_extension_accepts_initial_provider_user(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван Иванович",
            is_active=True,
            user=self.worker,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="initial-provider-user",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Иванов Иван Иванович",
            normalized_name="иванов иван иванович",
            extension="889",
            external_user="",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_AUTO_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_EXACT_NAME,
        )

        with transaction.atomic():
            profile, user = resolve_call_employee(
                self.organization,
                telephony,
                "889",
                "initial-user",
                lock_identity=True,
            )

        self.assertEqual(profile, employee)
        self.assertEqual(user, self.worker)
        identity.refresh_from_db()
        self.assertEqual(identity.external_user, "initial-user")
        self.assertFalse(identity.requires_manual_confirmation)
        self.assertEqual(
            identity.status,
            TelephonyEmployeeIdentity.STATUS_AUTO_MATCHED,
        )

    def test_inactive_employee_blocks_live_call_resolution(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Бывший сотрудник",
            is_active=False,
            user=self.worker,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон inactive employee",
            external_id="inactive-employee",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Бывший сотрудник",
            normalized_name="бывший сотрудник",
            extension="887",
            external_user="former-user",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )

        with transaction.atomic():
            profile, user = resolve_call_employee(
                self.organization,
                telephony,
                "887",
                "former-user",
                lock_identity=True,
            )

        self.assertIsNone(profile)
        self.assertIsNone(user)
        identity.refresh_from_db()
        self.assertTrue(identity.requires_manual_confirmation)
        self.assertEqual(
            identity.status,
            TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
        )
        self.assertEqual(identity.match_method, TelephonyEmployeeIdentity.MATCH_NONE)
        self.assertIsNotNone(identity.reassignment_detected_at)

    def test_auto_linked_service2_user_backfills_existing_profile_calls(self):
        self.worker.first_name = "Дарья"
        self.worker.last_name = "Крафт"
        self.worker.save(update_fields=["first_name", "last_name"])
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Крафт Дарья Валерьевна",
            first_name="Дарья",
            last_name="Крафт",
            middle_name="Валерьевна",
            is_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="late-service2-link",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Крафт Дарья Валерьевна",
            normalized_name="крафт дарья валерьевна",
            extension="884",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="late-link-call",
            employee_profile=employee,
            provider_extension="884",
            phone_number="+79001112236",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )

        self.assertTrue(auto_link_service2_user(employee, actor=self.owner))

        employee.refresh_from_db()
        call.refresh_from_db()
        identity.refresh_from_db()
        self.assertEqual(employee.user, self.worker)
        self.assertEqual(identity.employee, employee)
        self.assertEqual(call.employee_profile, employee)
        self.assertEqual(call.employee, self.worker)

    def test_manual_telephony_mapping_updates_existing_calls(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван Иванович",
            is_active=True,
            user=self.worker,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="manual-map",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            raw_name="Иван",
            normalized_name="иван",
            extension="777",
            status=TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
            match_method=TelephonyEmployeeIdentity.MATCH_NONE,
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="manual-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_OUT,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
            provider_extension="777",
        )

        mapped = map_telephony_identity(identity, employee, self.owner)

        self.assertEqual(mapped.employee, employee)
        self.assertEqual(
            mapped.status,
            TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
        )
        call.refresh_from_db()
        self.assertEqual(call.employee_profile, employee)
        self.assertEqual(call.employee, self.worker)

    def test_manual_mapping_correction_repairs_calls_from_previous_wrong_employee(self):
        old_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Ошибочно назначенный",
            is_active=True,
            user=self.worker,
        )
        new_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Правильный сотрудник",
            is_active=True,
            user=self.other,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="manual-correction",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=old_employee,
            raw_name="Правильный сотрудник",
            normalized_name="правильный сотрудник",
            extension="885",
            is_active=True,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="wrong-owner-call",
            employee=self.worker,
            employee_profile=old_employee,
            provider_extension="885",
            phone_number="+79001112237",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )

        map_telephony_identity(identity, new_employee, self.owner)

        call.refresh_from_db()
        self.assertEqual(call.employee_profile, new_employee)
        self.assertEqual(call.employee, self.other)

    def test_shared_provider_user_does_not_cross_assign_extensions(self):
        first_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Первый сотрудник",
            is_active=True,
            user=self.worker,
        )
        second_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Второй сотрудник",
            is_active=True,
            user=self.other,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="shared-provider-user",
        )
        first_identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            raw_name="Первый сотрудник",
            normalized_name="первый сотрудник",
            extension="891",
            external_user="shared-user",
            status=TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
            match_method=TelephonyEmployeeIdentity.MATCH_NONE,
        )
        second_identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            raw_name="Второй сотрудник",
            normalized_name="второй сотрудник",
            extension="892",
            external_user="shared-user",
            status=TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
            match_method=TelephonyEmployeeIdentity.MATCH_NONE,
        )
        first_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="shared-user-first",
            provider_extension="891",
            provider_user="shared-user",
            phone_number="+79001112240",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )
        second_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="shared-user-second",
            provider_extension="892",
            provider_user="shared-user",
            phone_number="+79001112241",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )
        extensionless_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="shared-user-extensionless",
            provider_user="shared-user",
            phone_number="+79001112242",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )

        map_telephony_identity(first_identity, first_employee, self.owner)

        first_call.refresh_from_db()
        second_call.refresh_from_db()
        extensionless_call.refresh_from_db()
        self.assertEqual(first_call.employee_profile, first_employee)
        self.assertEqual(first_call.employee, self.worker)
        self.assertIsNone(second_call.employee_profile)
        self.assertIsNone(second_call.employee)
        self.assertIsNone(extensionless_call.employee_profile)
        self.assertIsNone(extensionless_call.employee)

        map_telephony_identity(second_identity, second_employee, self.owner)

        first_call.refresh_from_db()
        second_call.refresh_from_db()
        extensionless_call.refresh_from_db()
        self.assertEqual(first_call.employee_profile, first_employee)
        self.assertEqual(first_call.employee, self.worker)
        self.assertEqual(second_call.employee_profile, second_employee)
        self.assertEqual(second_call.employee, self.other)
        self.assertIsNone(extensionless_call.employee_profile)
        self.assertIsNone(extensionless_call.employee)

    def test_reassignment_mapping_preserves_calls_before_detected_boundary(self):
        self.other.first_name = "Новый"
        self.other.last_name = "Владелец"
        self.other.save(update_fields=["first_name", "last_name"])
        old_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Старый владелец номера",
            is_active=True,
            user=self.worker,
        )
        new_employee = Employee.objects.create(
            organization=self.organization,
            display_name="Новый владелец номера",
            first_name="Новый",
            last_name="Владелец",
            is_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="reassignment-boundary",
        )
        boundary = timezone.now() - timedelta(minutes=5)
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=old_employee,
            raw_name="Новый владелец номера",
            normalized_name="новый владелец номера",
            extension="886",
            external_user="new-provider-user",
            is_active=True,
            requires_manual_confirmation=True,
            reassignment_detected_at=boundary,
            status=TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
            match_method=TelephonyEmployeeIdentity.MATCH_NONE,
        )
        old_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="before-reassignment",
            employee=self.worker,
            employee_profile=old_employee,
            provider_extension="886",
            phone_number="+79001112238",
            direction=PhoneCall.DIRECTION_IN,
            started_at=boundary - timedelta(days=1),
            result=PhoneCall.RESULT_ANSWERED,
        )
        recent_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="after-reassignment",
            employee=self.worker,
            employee_profile=old_employee,
            provider_extension="886",
            phone_number="+79001112239",
            direction=PhoneCall.DIRECTION_IN,
            started_at=boundary + timedelta(minutes=1),
            result=PhoneCall.RESULT_ANSWERED,
        )
        late_received_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="late-received-reassignment",
            employee=self.worker,
            employee_profile=old_employee,
            provider_extension="886",
            phone_number="+79001112240",
            direction=PhoneCall.DIRECTION_IN,
            started_at=boundary - timedelta(hours=1),
            result=PhoneCall.RESULT_ANSWERED,
        )
        known_new_holder_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="known-new-holder-before-detection",
            employee=self.worker,
            employee_profile=old_employee,
            provider_extension="886",
            provider_user="new-provider-user",
            phone_number="+79001112241",
            direction=PhoneCall.DIRECTION_IN,
            started_at=boundary - timedelta(hours=2),
            result=PhoneCall.RESULT_ANSWERED,
        )
        PhoneCall.objects.filter(pk=old_call.pk).update(
            created_at=boundary - timedelta(days=1)
        )
        PhoneCall.objects.filter(pk=known_new_holder_call.pk).update(
            created_at=boundary - timedelta(hours=2)
        )

        mapped = map_telephony_identity(identity, new_employee, self.owner)

        mapped.refresh_from_db()
        new_employee.refresh_from_db()
        old_call.refresh_from_db()
        recent_call.refresh_from_db()
        late_received_call.refresh_from_db()
        known_new_holder_call.refresh_from_db()
        self.assertFalse(mapped.requires_manual_confirmation)
        self.assertIsNone(mapped.reassignment_detected_at)
        self.assertEqual(new_employee.user, self.other)
        self.assertEqual(old_call.employee_profile, old_employee)
        self.assertEqual(old_call.employee, self.worker)
        self.assertEqual(recent_call.employee_profile, new_employee)
        self.assertEqual(recent_call.employee, self.other)
        self.assertEqual(late_received_call.employee_profile, new_employee)
        self.assertEqual(late_received_call.employee, self.other)
        self.assertEqual(known_new_holder_call.employee_profile, new_employee)
        self.assertEqual(known_new_holder_call.employee, self.other)

    def test_service2_account_mapping_is_unique_and_backfills_calls(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Петров Петр Петрович",
            is_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            external_id="service-user-map",
        )
        identity = TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Петров Петр Петрович",
            normalized_name="петров петр петрович",
            extension="778",
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="service-user-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
            provider_extension="778",
            employee_profile=employee,
        )

        map_employee_service2_user(employee, self.worker, self.owner)

        employee.refresh_from_db()
        self.assertEqual(employee.user, self.worker)
        call.refresh_from_db()
        self.assertEqual(call.employee, self.worker)
        self.assertEqual(identity.employee, employee)

    def test_service2_account_change_claims_legacy_calls(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Петров Петр Петрович",
            is_active=True,
            user=self.worker,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            external_id="legacy-service-user-map",
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="legacy-service-user-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
            employee=self.worker,
        )

        map_employee_service2_user(employee, self.other, self.owner)

        employee.refresh_from_db()
        call.refresh_from_db()
        self.assertEqual(employee.user, self.other)
        self.assertEqual(call.employee_profile, employee)
        self.assertEqual(call.employee, self.other)

    def test_unified_employee_mapping_page_lists_1c_and_telephony_sources(self):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name="Сидоров Сидор Сидорович",
            is_active=True,
            user=self.worker,
        )
        EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=employee,
            raw_name="Сидоров Сидор Сидорович",
            normalized_name="сидоров сидор сидорович",
            onec_employee_id="11111111-1111-1111-1111-111111111111",
            personnel_number="0001",
            status=EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
            match_method=EmployeeOneCIdentity.MATCH_MANUAL,
            source_active=True,
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="mapping-page",
        )
        TelephonyEmployeeIdentity.objects.create(
            organization=self.organization,
            connection=telephony,
            employee=employee,
            raw_name="Сидоров Сидор Сидорович",
            normalized_name="сидоров сидор сидорович",
            extension="779",
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED,
            match_method=TelephonyEmployeeIdentity.MATCH_MANUAL,
        )

        self.organization.paid_until = timezone.now() + timedelta(days=30)
        self.organization.save(update_fields=["paid_until"])
        self.client.login(username="owner", password="test")
        page = self.client.get(reverse("finance_payroll_employee_mapping"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Сопоставление сотрудников")
        self.assertContains(page, "Синхронизировать всё")
        self.assertContains(page, "Сидоров Сидор Сидорович")
        self.assertContains(page, "ext 779")
        self.assertContains(page, "0001")
        self.assertContains(page, 'id="identity-live-search"')
        self.assertContains(page, "data-identity-search-row")
        self.assertContains(page, 'url.searchParams.set("q", input.value.trim())')
        self.assertContains(page, "}, 300);")

    def test_channel_settings_are_organization_scoped(self):
        foreign_organization = Organization.objects.create(name="Foreign setup org")
        foreign_channel = CommunicationChannel.objects.create(
            organization=foreign_organization,
            kind="website",
            name="Foreign site",
        )
        foreign_connection = ChannelConnection.objects.create(
            channel=foreign_channel,
            name="Foreign connection",
            external_id="foreign",
        )
        foreign_avito_channel = CommunicationChannel.objects.create(
            organization=foreign_organization,
            kind="avito",
            name="Foreign Avito",
        )
        foreign_avito_connection = ChannelConnection.objects.create(
            channel=foreign_avito_channel,
            name="Foreign Avito account",
            external_id="987654321",
        )
        foreign_line = TelephonyConnection.objects.create(
            organization=foreign_organization,
            name="Foreign line",
            external_id="foreign-line",
        )
        self.client.login(username="owner", password="test")
        self.assertEqual(
            self.client.get(
                reverse("communication_connection_edit", args=[foreign_connection.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("communication_telephony_edit", args=[foreign_line.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("communication_avito_connect", args=[foreign_avito_connection.pk]),
                secure=True,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("communication_avito_check", args=[foreign_avito_connection.pk]),
                secure=True,
            ).status_code,
            404,
        )

    def test_communication_notification_feed_is_persistent_and_user_scoped(self):
        own_notification = Notification.objects.create(
            user=self.worker,
            organization=self.organization,
            kind="communication",
            title="Новое сообщение",
            message="Авито · Иван: Здравствуйте",
            action_url="/communications/?conversation=test",
        )
        other_notification = Notification.objects.create(
            user=self.other,
            organization=self.organization,
            kind="communication",
            title="Чужое сообщение",
        )
        self.client.login(username="worker", password="test")
        feed = self.client.get(reverse("communication_notification_feed"))
        self.assertEqual(feed.status_code, 200)
        self.assertEqual([item["id"] for item in feed.json()["notifications"]], [own_notification.pk])
        self.assertFalse(Notification.objects.get(pk=own_notification.pk).is_resolved)
        self.assertEqual(
            self.client.post(reverse("communication_notification_resolve", args=[other_notification.pk])).status_code,
            404,
        )
        other_notification.refresh_from_db()
        self.assertFalse(other_notification.is_resolved)
        page = self.client.get(reverse("communications_calls"))
        self.assertContains(page, 'id="communication-alerts"')
        self.assertContains(page, "window.setInterval(pollCommunicationAlerts, 15000)", html=False)
        self.assertContains(page, "visibleIds.slice(index * 20, (index + 1) * 20)", html=False)

        self.assertEqual(
            self.client.get(reverse("communication_notification_resolve", args=[own_notification.pk])).status_code,
            405,
        )
        resolved = self.client.post(reverse("communication_notification_resolve", args=[own_notification.pk]))
        self.assertEqual(resolved.status_code, 200)
        own_notification.refresh_from_db()
        self.assertTrue(own_notification.is_read)
        self.assertTrue(own_notification.is_resolved)
        resolved_feed = self.client.get(
            reverse("communication_notification_feed"), {"visible": str(own_notification.pk)},
        ).json()
        self.assertEqual(resolved_feed["notifications"], [])
        self.assertEqual(resolved_feed["resolved_ids"], [own_notification.pk])
        other_notification.is_resolved = True
        other_notification.save(update_fields=["is_resolved"])
        scoped_resolved = self.client.get(
            reverse("communication_notification_feed"), {"visible": f"{own_notification.pk},{other_notification.pk}"},
        ).json()
        self.assertEqual(scoped_resolved["resolved_ids"], [own_notification.pk])
        foreign_organization = Organization.objects.create(name="Foreign notification org")
        foreign_notification = Notification.objects.create(
            user=self.worker,
            organization=foreign_organization,
            kind="communication",
            title="Другая организация",
            is_resolved=True,
        )
        other_kind = Notification.objects.create(
            user=self.worker,
            organization=self.organization,
            kind="finance",
            title="Другой тип",
            is_resolved=True,
        )
        isolated = self.client.get(
            reverse("communication_notification_feed"),
            {"visible": f"{foreign_notification.pk},{other_kind.pk}"},
        ).json()
        self.assertEqual(isolated["resolved_ids"], [])
        self.assertEqual(
            self.client.get(reverse("communication_notification_feed"), {"visible": "1,true"}).status_code,
            400,
        )

        generated = [Notification.objects.create(
            user=self.worker,
            organization=self.organization,
            kind="communication",
            title=f"Сообщение {index}",
        ) for index in range(11)]
        feed_ids = [item["id"] for item in self.client.get(reverse("communication_notification_feed")).json()["notifications"]]
        self.assertNotIn(generated[0].pk, feed_ids)
        self.assertEqual(feed_ids[-1], generated[-1].pk)

        csrf_notification = Notification.objects.create(
            user=self.worker,
            organization=self.organization,
            kind="communication",
            title="CSRF check",
        )
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertTrue(csrf_client.login(username="worker", password="test"))
        csrf_client.get(reverse("communication_notification_feed"))
        resolve_url = reverse("communication_notification_resolve", args=[csrf_notification.pk])
        self.assertEqual(csrf_client.post(resolve_url).status_code, 403)
        csrf_token = csrf_client.cookies["csrftoken"].value
        self.assertEqual(csrf_client.post(resolve_url, HTTP_X_CSRFTOKEN=csrf_token).status_code, 200)

        self.client.logout()
        self.client.login(username="accountant", password="test")
        self.assertNotEqual(self.client.get(reverse("communication_notification_feed")).status_code, 200)

    def test_view_only_capability_cannot_change_conversation_status(self):
        viewer = User.objects.create_user("viewer", password="test")
        OrganizationAccess.objects.create(user=viewer, organization=self.organization, role="viewer")
        CommunicationAccess.objects.filter(user=viewer, organization=self.organization).update(can_view_conversations=True)
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="view-only-chat",
            participant_name="Иван",
        )
        self.client.login(username="viewer", password="test")
        page = self.client.get(
            reverse("communications_conversations"), {"conversation": conversation.uuid},
        )
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="status"')
        response = self.client.post(
            reverse("communication_update", args=[conversation.uuid]),
            {"status": Conversation.STATUS_DONE},
        )
        self.assertEqual(response.status_code, 403)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.STATUS_NEW)

    def test_worker_sees_only_own_calls_and_owner_sees_all(self):
        telephony = TelephonyConnection.objects.create(organization=self.organization, external_id="megafon")
        for index, employee in enumerate((self.worker, self.other)):
            PhoneCall.objects.create(organization=self.organization, connection=telephony, external_id=str(index), employee=employee, phone_number=f"7000000000{index}", direction="in", started_at=timezone.now() - timedelta(minutes=index), result="answered")
        self.client.login(username="worker", password="test")
        response = self.client.get(reverse("communications_calls"))
        self.assertContains(response, "70000000000")
        self.assertNotContains(response, "70000000001")
        self.assertNotContains(response, 'name="employee"')
        scoped = self.client.get(reverse("communications_calls"), {"employee": self.other.pk})
        self.assertContains(scoped, "70000000000")
        self.assertNotContains(scoped, "70000000001")
        self.client.logout(); self.client.login(username="owner", password="test")
        response = self.client.get(reverse("communications_calls"))
        self.assertContains(response, "70000000000")
        self.assertContains(response, "70000000001")
        self.assertContains(response, 'name="employee"')
        self.assertEqual(self.client.get(reverse("communications_calls"), {"date_from": "not-a-date"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("communications_calls"), {"date_from": "2026-02-02", "date_to": "2026-02-01"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("communications_calls"), {"employee": "not-an-id"}).status_code, 400)

    def test_call_recording_requires_stored_file_and_scopes_access(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            external_id="recordings",
            recording_allowed_hosts=["recordings.example.test"],
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="call-recording",
            employee=self.worker,
            phone_number="70000000000",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
            recording_ref="https://recordings.example.test/call.mp3",
            recording_status=PhoneCall.RECORDING_PENDING,
        )
        self.client.login(username="worker", password="test")
        url = reverse("communication_call_recording", args=[call.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

        payload = b"ID3" + b"private-recording"
        call.recording_file.save(
            "private-call.mp3",
            ContentFile(payload),
            save=True,
        )
        call.recording_status = PhoneCall.RECORDING_STORED
        call.save(update_fields=["recording_status"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/mpeg")
        self.assertNotIn("recordings.example.test", response.get("Location", ""))

        foreign_organization = Organization.objects.create(name="Foreign calls org")
        foreign_owner = User.objects.create_user("foreign-call-owner", password="test")
        OrganizationAccess.objects.create(
            user=foreign_owner, organization=foreign_organization, role="owner",
        )
        self.client.logout()
        self.client.login(username="foreign-call-owner", password="test")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_recording_downloader_rejects_untrusted_recording_host(self):
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            external_id="untrusted-recording",
            recording_allowed_hosts=["records.megapbx.ru"],
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="untrusted-call",
            phone_number="+79001112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
            recording_ref="https://example.invalid/call.mp3",
            recording_status=PhoneCall.RECORDING_PENDING,
        )
        self.assertFalse(download_call_recording(call.pk))
        call.refresh_from_db()
        self.assertEqual(call.recording_status, PhoneCall.RECORDING_FAILED)
        self.assertEqual(call.recording_error, "untrusted_recording_url")
        self.assertFalse(call.recording_file)

    def test_avito_dialog_shows_delivery_state_and_disables_attachments(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="chat-ui",
            participant_name="Иван",
            last_message_at=timezone.now(),
        )
        ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Не доставлено",
            delivery_status=ConversationMessage.DELIVERY_FAILED,
        )
        self.client.login(username="worker", password="test")
        response = self.client.get(reverse("communications_conversations"), {"conversation": conversation.uuid})
        self.assertContains(response, "Ошибка")
        self.assertContains(response, "Для Авито сейчас доступна отправка только текста")
        self.assertNotContains(response, 'type="file"')

    def test_avito_attachment_is_rejected_on_server(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="chat-server-validation",
            participant_name="Иван",
        )
        self.client.login(username="worker", password="test")
        response = self.client.post(
            reverse("communication_reply", args=[conversation.uuid]),
            {"body": "Файл", "attachments": SimpleUploadedFile("note.txt", b"data", content_type="text/plain")},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(conversation.messages.exists())

        self.connection.is_active = False
        self.connection.save(update_fields=["is_active"])
        inactive = self.client.post(
            reverse("communication_reply", args=[conversation.uuid]),
            {"body": "Не ставить в очередь"},
        )
        self.assertEqual(inactive.status_code, 302)
        self.assertFalse(conversation.messages.exists())

        too_long = self.client.post(
            reverse("communication_reply", args=[conversation.uuid]),
            {"body": "x" * 10001},
        )
        self.assertEqual(too_long.status_code, 302)
        self.assertFalse(conversation.messages.exists())

    def test_invalid_image_rejects_entire_website_reply_and_download_is_hardened(self):
        website_channel = CommunicationChannel.objects.create(
            organization=self.organization, kind="website", name="Сайт",
        )
        website_connection = ChannelConnection.objects.create(
            channel=website_channel, name="Чат", external_id="widget",
        )
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=website_connection,
            external_id="chat-file",
            participant_name="Анна",
        )
        self.client.login(username="worker", password="test")
        response = self.client.post(
            reverse("communication_reply", args=[conversation.uuid]),
            {"body": "Фото", "attachments": SimpleUploadedFile("broken.png", b"not-an-image", content_type="image/png")},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(conversation.messages.exists())

        message = ConversationMessage.objects.create(
            conversation=conversation, direction="out", body="Документ",
        )
        attachment = MessageAttachment.objects.create(
            message=message,
            original=SimpleUploadedFile("page.html", b"<script>alert(1)</script>", content_type="text/html"),
            original_name="page.html",
            content_type="text/html",
            original_size=25,
        )
        download = self.client.get(reverse("communication_attachment", args=[attachment.pk]))
        self.assertEqual(download["Content-Type"], "application/octet-stream")
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment", download["Content-Disposition"])

        foreign_organization = Organization.objects.create(name="Foreign communications org")
        foreign_user = User.objects.create_user("foreign-worker", password="test")
        OrganizationAccess.objects.create(
            user=foreign_user, organization=foreign_organization, role="manager",
        )
        self.client.logout()
        self.client.login(username="foreign-worker", password="test")
        self.assertEqual(
            self.client.get(reverse("communication_attachment", args=[attachment.pk])).status_code,
            404,
        )


class WebsiteCommunicationApiTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Website API org")
        owner = User.objects.create_user("website-owner", password="test")
        OrganizationAccess.objects.create(user=owner, organization=self.organization, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.organization, kind="website", name="Основной сайт")
        self.connection = ChannelConnection(channel=channel, name="Виджет", external_id="widget")
        self.connection.set_api_token("secret-token")
        self.connection.save()
        self.headers = {"HTTP_AUTHORIZATION": "Bearer secret-token"}

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_chat_ingestion_is_authenticated_and_idempotent(self, _send_push):
        url = reverse("website_chat_message", args=[self.connection.public_id])
        payload = {"session_id": "browser-1", "message_id": "client-1", "name": "Иван", "phone": "+70000000000", "body": "Здравствуйте"}
        self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 401)
        first = self.client.post(url, payload, content_type="application/json", **self.headers)
        duplicate = self.client.post(url, payload, content_type="application/json", **self.headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(ConversationMessage.objects.filter(direction="in").count(), 1)

        boundary_payload = dict(payload, session_id="s" * 250, message_id="client-2")
        boundary = self.client.post(url, boundary_payload, content_type="application/json", **self.headers)
        self.assertEqual(boundary.status_code, 201)
        self.assertEqual(len(Conversation.objects.get(external_id__startswith="chat:ss").external_id), 255)
        too_long_session = self.client.post(
            url,
            dict(payload, session_id="s" * 251, message_id="client-3"),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(too_long_session.status_code, 400)
        self.assertEqual(too_long_session.json()["error"], "invalid_session_id")

        oversized = b'{"body":"' + (b"x" * (64 * 1024)) + b'"}'
        oversized_request = RequestFactory().post(url, b"{}", content_type="application/json")
        oversized_request.META.pop("CONTENT_LENGTH", None)
        oversized_request._body = oversized
        with self.assertRaisesMessage(ValueError, "payload_too_large"):
            _payload(oversized_request)

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_website_request_and_reply_delivery_ack(self, _send_push):
        request_url = reverse("website_request_create", args=[self.connection.public_id])
        response = self.client.post(request_url, {"request_id": "lead-1", "name": "Анна", "phone": "+71111111111", "service": "Бассейн", "delivery": "Самовывоз", "address": "Барнаул", "comment": "Перезвоните"}, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(WebsiteRequest.objects.get().service, "Бассейн")
        self.client.login(username="website-owner", password="test")
        request_card = self.client.get(
            reverse("communications_conversations"),
            {"conversation": response.json()["conversation_id"]},
        )
        self.assertContains(request_card, "Заявка с сайта")
        self.assertContains(request_card, "+71111111111")
        self.assertContains(request_card, "Бассейн")
        self.assertContains(request_card, "Самовывоз")
        self.assertContains(request_card, "Барнаул")
        self.assertContains(request_card, "Перезвоните")
        self.client.logout()

        conversation = Conversation.objects.create(organization=self.organization, connection=self.connection, external_id="chat:browser-2", participant_name="Анна")
        outgoing = ConversationMessage.objects.create(conversation=conversation, direction="out", body="Добрый день", delivery_status="pending")
        outbox_url = reverse("website_chat_outbox", args=[self.connection.public_id, "browser-2"])
        outbox = self.client.get(outbox_url, **self.headers)
        self.assertEqual(outbox.json()["messages"][0]["body"], "Добрый день")
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.delivery_status, ConversationMessage.DELIVERY_SENDING)
        ack_url = reverse("website_chat_outbox_ack", args=[self.connection.public_id, "browser-2"])
        ack = self.client.post(ack_url, {"message_ids": [outgoing.pk]}, content_type="application/json", **self.headers)
        self.assertEqual(ack.json()["acknowledged"], 1)
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.delivery_status, "delivered")
        delivered_at = outgoing.delivered_at
        repeated_ack = self.client.post(ack_url, {"message_ids": [outgoing.pk]}, content_type="application/json", **self.headers)
        self.assertEqual(repeated_ack.json()["acknowledged"], 0)
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.delivered_at, delivered_at)
        self.assertEqual(self.client.get(outbox_url, **self.headers).json()["messages"], [])

        failed = ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Не отправлять",
            delivery_status=ConversationMessage.DELIVERY_FAILED,
        )
        self.assertEqual(self.client.get(outbox_url, **self.headers).json()["messages"], [])
        failed_ack = self.client.post(
            ack_url, {"message_ids": [failed.pk]}, content_type="application/json", **self.headers,
        )
        self.assertEqual(failed_ack.json()["acknowledged"], 0)
        failed.refresh_from_db()
        self.assertEqual(failed.delivery_status, ConversationMessage.DELIVERY_FAILED)
        invalid_bool = self.client.post(
            ack_url, {"message_ids": [True]}, content_type="application/json", **self.headers,
        )
        self.assertEqual(invalid_bool.status_code, 400)
        never_claimed = ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Ещё не получено сайтом",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        premature_ack = self.client.post(
            ack_url, {"message_ids": [never_claimed.pk]}, content_type="application/json", **self.headers,
        )
        self.assertEqual(premature_ack.json()["acknowledged"], 0)
        never_claimed.refresh_from_db()
        self.assertEqual(never_claimed.delivery_status, ConversationMessage.DELIVERY_PENDING)

        other_conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="chat:other-browser",
            participant_name="Олег",
        )
        other_message = ConversationMessage.objects.create(
            conversation=other_conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Другой диалог",
            delivery_status=ConversationMessage.DELIVERY_SENDING,
        )
        cross_session_ack = self.client.post(
            ack_url, {"message_ids": [other_message.pk]}, content_type="application/json", **self.headers,
        )
        self.assertEqual(cross_session_ack.json()["acknowledged"], 0)
        other_message.refresh_from_db()
        self.assertEqual(other_message.delivery_status, ConversationMessage.DELIVERY_SENDING)


class AvitoCommunicationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Avito API org")
        owner = User.objects.create_user("avito-owner", password="test")
        OrganizationAccess.objects.create(user=owner, organization=self.organization, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.organization, kind="avito", name="Авито")
        self.connection = ChannelConnection(channel=channel, name="Аккаунт", external_id="12345")
        self.connection.set_api_token("webhook-secret")
        self.connection.save()
        self.headers = {"HTTP_AUTHORIZATION": "Bearer webhook-secret"}

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_webhook_is_authenticated_scoped_and_idempotent(self, _send_push):
        url = reverse("avito_webhook", args=[self.connection.public_id, "webhook-secret"])
        bad_url = reverse("avito_webhook", args=[self.connection.public_id, "wrong-secret"])
        payload = {"payload": {"type": "message", "value": {
            "id": "message-1", "chat_id": "chat-1", "user_id": 12345,
            "author_id": 67890, "author_name": "Иван", "type": "text",
            "content": {"text": "Здравствуйте"},
        }}}
        self.assertEqual(self.client.post(bad_url, payload, content_type="application/json").status_code, 401)
        first = self.client.post(url, payload, content_type="application/json")
        duplicate = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(ConversationMessage.objects.get().body, "Здравствуйте")
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.settings["avito_webhook_last_received_at"])
        self.assertEqual(self.connection.settings["avito_webhook_last_result"], "duplicate")

        wrong_account = {"payload": {"type": "message", "value": dict(payload["payload"]["value"], id="message-2", user_id=999)}}
        response = self.client.post(url, wrong_account, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConversationMessage.objects.count(), 1)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings["avito_webhook_last_result"], "ignored")

        invalid = {"payload": {"type": "message", "value": dict(
            payload["payload"]["value"], id="message-3", type="unsupported"
        )}}
        invalid_response = self.client.post(url, invalid, content_type="application/json")
        self.assertEqual(invalid_response.status_code, 200)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings["avito_webhook_last_result"], "error")
        self.assertEqual(
            self.connection.settings["avito_webhook_last_error"],
            "unsupported_message_type",
        )

    @patch("pool_service.communication_avito._json_request")
    def test_credentials_are_encrypted_and_outgoing_message_is_delivered(self, request):
        credential = AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client-id"),
            client_secret_encrypted=encrypt_secret("client-secret"),
        )
        self.assertNotIn("client-secret", credential.client_secret_encrypted)
        request.side_effect = [
            {"access_token": "short-lived-token", "expires_in": 3600},
            {"id": "provider-message-1"},
        ]
        conversation = Conversation.objects.create(
            organization=self.organization, connection=self.connection,
            external_id="chat-1", participant_name="Иван",
        )
        message = ConversationMessage.objects.create(
            conversation=conversation, direction="out", body="Добрый день",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        send_message(message)
        message.refresh_from_db()
        credential.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_DELIVERED)
        self.assertEqual(message.external_id, "provider-message-1")
        self.assertEqual(decrypt_secret(credential.access_token_encrypted), "short-lived-token")
        self.assertEqual(access_token(self.connection), "short-lived-token")
        self.assertEqual(request.call_count, 2)

    @patch("pool_service.communication_avito._json_request")
    def test_authorized_account_and_messenger_access_helpers(self, request):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client-id"),
            client_secret_encrypted=encrypt_secret("client-secret"),
            access_token_encrypted=encrypt_secret("cached-token"),
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        request.side_effect = [
            {"id": 7986565},
            {"chats": []},
        ]
        self.assertEqual(authorized_account_id(self.connection), "7986565")
        self.assertTrue(verify_messenger_access(self.connection, "7986565"))
        self.assertIn("/core/v1/accounts/self", request.call_args_list[0].args[0])
        self.assertIn(
            "/messenger/v2/accounts/7986565/chats?limit=1&offset=0",
            request.call_args_list[1].args[0],
        )

    @patch("pool_service.services.notifications.send_push_to_users")
    @patch("pool_service.communication_avito._json_list_request")
    @patch("pool_service.communication_avito._json_request")
    def test_pull_sync_recovers_recent_inbound_messages(
        self, request, list_request, _send_push
    ):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client-id"),
            client_secret_encrypted=encrypt_secret("client-secret"),
            access_token_encrypted=encrypt_secret("cached-token"),
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        now_ts = int(timezone.now().timestamp())
        request.side_effect = [
            {"id": 12345},
            {
                "chats": [
                    {
                        "id": "chat-recovery",
                        "users": [
                            {
                                "name": "Клиент",
                                "public_user_profile": {"user_id": 67890},
                            }
                        ],
                    }
                ]
            },
            {"id": 12345},
            {
                "chats": [
                    {
                        "id": "chat-recovery",
                        "users": [
                            {
                                "name": "Клиент",
                                "public_user_profile": {"user_id": 67890},
                            }
                        ],
                    }
                ]
            },
        ]
        list_request.return_value = [
            {
                "id": "out-1",
                "author_id": 12345,
                "created": now_ts,
                "direction": "out",
                "type": "text",
                "content": {"text": "Наш ответ"},
            },
            {
                "id": "in-1",
                "author_id": 67890,
                "created": now_ts,
                "direction": "in",
                "type": "text",
                "content": {"text": "Тестовое входящее"},
            },
        ]
        result = sync_recent_messages(self.connection)
        self.assertEqual(result.chats_checked, 1)
        self.assertEqual(result.messages_created, 1)
        self.assertEqual(result.messages_skipped, 1)
        message = ConversationMessage.objects.get(external_id="in-1")
        self.assertEqual(message.body, "Тестовое входящее")
        self.assertEqual(message.conversation.external_id, "chat-recovery")
        self.assertEqual(message.conversation.participant_name, "Клиент")

        second = sync_recent_messages(self.connection)
        self.assertEqual(second.messages_created, 0)
        self.assertEqual(second.messages_existing, 1)

    def test_message_list_request_accepts_wrapped_messages_payload(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"messages":[{"id":"m1","direction":"in","type":"text","content":{"text":"hello"}}]}'
        )
        with patch("pool_service.communication_avito.urlopen", return_value=response):
            messages = _json_list_request("https://api.avito.ru/test")
        self.assertEqual(messages[0]["id"], "m1")

    def test_message_list_request_rejects_unknown_object_payload(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch("pool_service.communication_avito.urlopen", return_value=response):
            with self.assertRaisesMessage(
                AvitoError, "provider_messages_invalid_response"
            ):
                _json_list_request("https://api.avito.ru/test")

    @patch("pool_service.communication_avito._json_request")
    def test_webhook_provider_helpers_validate_contract(self, request):
        credential = AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client-id"),
            client_secret_encrypted=encrypt_secret("client-secret"),
            access_token_encrypted=encrypt_secret("cached-token"),
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        callback = "https://service2.example/api/communications/avito/test/token/webhook/"

        request.side_effect = [
            {"ok": True},
            {"subscriptions": [{"url": callback, "version": "3"}]},
            {"ok": True},
        ]
        self.assertEqual(subscribe_webhook(self.connection, callback), {"ok": True})
        self.assertEqual(webhook_subscriptions(self.connection), [callback])
        self.assertEqual(unsubscribe_webhook(self.connection, callback), {"ok": True})
        self.assertEqual(request.call_count, 3)
        credential.refresh_from_db()
        self.assertEqual(
            decrypt_secret(credential.access_token_encrypted),
            "cached-token",
        )

        request.reset_mock()
        request.side_effect = None
        request.return_value = {"ok": False}
        with self.assertRaisesMessage(AvitoError, "provider_webhook_rejected"):
            subscribe_webhook(self.connection, callback)

    @patch("pool_service.communication_avito._json_request")
    def test_corrupt_cached_token_is_replaced(self, request):
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted=encrypt_secret("client-id"),
            client_secret_encrypted=encrypt_secret("client-secret"),
            access_token_encrypted="not-a-fernet-token",
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        request.return_value = {"access_token": "replacement-token", "expires_in": 3600}

        self.assertEqual(access_token(self.connection), "replacement-token")
        self.assertEqual(request.call_count, 1)

    @patch("pool_service.management.commands.send_avito_outbox.send_message")
    def test_outbox_claim_prevents_a_second_worker_from_sending(self, sender):
        conversation = Conversation.objects.create(
            organization=self.organization, connection=self.connection,
            external_id="chat-claim", participant_name="Иван",
        )
        message = ConversationMessage.objects.create(
            conversation=conversation, direction="out", body="Один раз",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        call_command("send_avito_outbox")
        call_command("send_avito_outbox")
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_SENDING)
        sender.assert_called_once()

    def test_outbox_stale_selection_is_not_claimed_after_deactivation(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="chat-disabled-after-selection",
            participant_name="Иван",
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Не отправлять после отключения",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        selected_id = message.pk
        self.connection.is_active = False
        self.connection.save(update_fields=["is_active"])
        self.assertIsNone(claim_message(selected_id))
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_PENDING)

        self.connection.is_active = True
        self.connection.save(update_fields=["is_active"])
        self.connection.channel.is_active = False
        self.connection.channel.save(update_fields=["is_active"])
        self.assertIsNone(claim_message(selected_id))
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_PENDING)

    @patch("pool_service.management.commands.send_avito_outbox.send_message")
    def test_transient_presend_failure_retries_are_bounded(self, sender):
        sender.side_effect = AvitoRetryableError("provider_http_503")
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="chat-transient",
            participant_name="Иван",
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Повторить после временной ошибки",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )

        call_command("send_avito_outbox")
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_PENDING)
        self.assertEqual(message.delivery_attempts, 1)

        call_command("send_avito_outbox")
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_PENDING)
        self.assertEqual(message.delivery_attempts, 2)

        call_command("send_avito_outbox")
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_FAILED)
        self.assertEqual(message.delivery_attempts, 3)
        self.assertEqual(message.delivery_error, "provider_http_503")
        self.assertEqual(sender.call_count, 3)

    def test_missing_credentials_fail_message_without_crashing_batch(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_id="chat-no-credentials",
            participant_name="Иван",
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            direction=ConversationMessage.DIRECTION_OUT,
            body="Сообщение без настройки",
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        call_command("send_avito_outbox")
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, ConversationMessage.DELIVERY_FAILED)
        self.assertEqual(message.delivery_error, "provider_credentials_missing")
