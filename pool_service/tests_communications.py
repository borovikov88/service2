from datetime import timedelta
import logging
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import AvitoCredential, CommunicationAccess, CommunicationChannel, ChannelConnection, Conversation, ConversationMessage, MessageAttachment, PhoneCall, TelephonyConnection, WebsiteRequest
from pool_service.communication_avito import AvitoRetryableError, access_token, send_message
from pool_service.communication_secrets import decrypt_secret, encrypt_secret
from pool_service.communication_services import receive_message, users_with_conversation_access
from pool_service.communication_services import conversation_capability
from pool_service.communication_api import _payload
from pool_service.management.commands.send_avito_outbox import claim_message
from pool_service.models import Notification, Organization, OrganizationAccess
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

    def test_owner_sees_communications_in_active_desktop_and_mobile_navigation(self):
        self.client.login(username="owner", password="test")
        response = self.client.get(reverse("clients_list"))
        self.assertEqual(response.status_code, 200)
        url = reverse("communications_conversations")
        html = response.content.decode("utf-8")
        self.assertIn(
            f'href="{url}" class="desktop-sidebar__link',
            html,
        )
        self.assertIn(
            f'href="{url}" class="list-group-item list-group-item-action',
            html,
        )

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

    def test_call_recording_requires_https_without_embedded_credentials(self):
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
            recording_ref="http://recordings.example.test/call.mp3",
        )
        self.client.login(username="worker", password="test")
        url = reverse("communication_call_recording", args=[call.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        call.recording_ref = "https://token:secret@recordings.example.test/call.mp3"
        call.save(update_fields=["recording_ref"])
        self.assertEqual(self.client.get(url).status_code, 404)
        for invalid_url in (
            "https://[broken",
            "https://recordings.example.test:notaport/call.mp3",
            "https://recordings.example.test/call mp3",
        ):
            call.recording_ref = invalid_url
            call.save(update_fields=["recording_ref"])
            self.assertEqual(self.client.get(url).status_code, 404)
        call.recording_ref = "https://recordings.example.test/call.mp3"
        call.save(update_fields=["recording_ref"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], call.recording_ref)
        call.recording_ref = "https://untrusted.example.test/call.mp3"
        call.save(update_fields=["recording_ref"])
        self.assertEqual(self.client.get(url).status_code, 404)
        call.recording_ref = "https://recordings.example.test:8443/call.mp3"
        call.save(update_fields=["recording_ref"])
        self.assertEqual(self.client.get(url).status_code, 404)
        TelephonyConnection.objects.filter(pk=telephony.pk).update(recording_allowed_hosts="not-a-list")
        self.assertEqual(self.client.get(url).status_code, 404)

        foreign_organization = Organization.objects.create(name="Foreign calls org")
        foreign_owner = User.objects.create_user("foreign-call-owner", password="test")
        OrganizationAccess.objects.create(
            user=foreign_owner, organization=foreign_organization, role="owner",
        )
        self.client.logout()
        self.client.login(username="foreign-call-owner", password="test")
        self.assertEqual(self.client.get(url).status_code, 404)

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
        self.assertEqual(first.status_code, 201)
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(ConversationMessage.objects.get().body, "Здравствуйте")

        wrong_account = {"payload": {"type": "message", "value": dict(payload["payload"]["value"], id="message-2", user_id=999)}}
        response = self.client.post(url, wrong_account, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConversationMessage.objects.count(), 1)

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
