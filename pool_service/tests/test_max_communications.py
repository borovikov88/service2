import hashlib
import hmac
import json
import ssl
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as TestClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_max_media import MaxMediaError, media_fingerprints, media_url
from pool_service.communication_max_outbound import MAX_CA, MaxApiError, dispatch_reply, max_api_ssl_context
from pool_service.communication_secrets import decrypt_secret, encrypt_secret
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.communication_models import (
    ChannelConnection, CommunicationAccess, CommunicationChannel, Conversation, ConversationMessage,
    ConversationReadState, MessageAttachment,
)
from pool_service.crm_commercial_models import CrmLead
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.services.crm_lead_access import leads_for_user
from pool_service.tests.test_crm_lead_intake import LeadIntakeFixture


@override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class MaxWebhookTests(LeadIntakeFixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="max", name="MAX")
        self.connection = ChannelConnection.objects.create(channel=self.channel, external_id="12345", name="Test MAX")
        self.connection.set_api_token("test-webhook-secret")
        self.connection.save(update_fields=["api_token_hash"])
        self.url = reverse("max_webhook", args=[self.connection.public_id])
        self.payload = {
            "update_type": "message_created",
            "message": {
                "recipient": {"chat_type": "dialog", "user_id": 12345},
                "sender": {"user_id": 500, "first_name": "Тест", "is_bot": False},
                "timestamp": int(self.now.timestamp() * 1000),
                "body": {"mid": "test-mid", "text": "Вопрос о бассейне"},
            },
        }

    def post_update(self, secret="test-webhook-secret"):
        return self.client.post(
            self.url, json.dumps(self.payload), content_type="application/json",
            HTTP_X_MAX_BOT_API_SECRET=secret,
        )

    def test_same_message_creates_one_conversation_and_lead(self):
        self.assertEqual(self.post_update().status_code, 200)
        self.assertEqual(self.post_update().status_code, 200)
        conversation = Conversation.objects.get(connection=self.connection)
        self.assertEqual(conversation.participant_external_id, "500")
        self.assertEqual(conversation.organization_id, self.org.pk)
        self.assertEqual(ConversationMessage.objects.get(conversation=conversation).body, "Вопрос о бассейне")
        self.assertEqual(CrmInboxEvent.objects.count(), 1)
        self.assertEqual(self.notify.call_count, 1)
        self.deliver()
        lead = CrmLead.objects.get()
        self.assertEqual(lead.first_contact_channel, "max")
        self.assertTrue(leads_for_user(self.user, self.org).filter(pk=lead.pk).exists())

    def test_wrong_secret_and_inactive_connection_store_nothing(self):
        self.assertEqual(self.post_update("wrong-secret").status_code, 403)
        self.connection.is_active = False
        self.connection.save(update_fields=["is_active"])
        self.assertEqual(self.post_update().status_code, 404)
        self.assertFalse(ConversationMessage.objects.exists())

    def test_ignores_groups_bot_echoes_and_messages_for_other_bots(self):
        msg = self.payload["message"]
        msg["recipient"]["chat_type"] = "chat"
        self.assertEqual(self.post_update().status_code, 200)
        msg["recipient"]["chat_type"] = "dialog"
        msg["recipient"]["user_id"] = 12346
        self.assertEqual(self.post_update().status_code, 200)
        msg["recipient"]["user_id"] = 12345
        msg["sender"]["is_bot"] = True
        self.assertEqual(self.post_update().status_code, 200)
        self.assertFalse(ConversationMessage.objects.exists())

    def test_malformed_message_is_rejected_before_saving(self):
        self.payload["message"]["body"]["mid"] = ""
        self.assertEqual(self.post_update().status_code, 400)
        self.assertFalse(ConversationMessage.objects.exists())

    def _reply_form(self):
        self.assertEqual(self.post_update().status_code, 200)
        conversation = Conversation.objects.get(connection=self.connection)
        self.client.force_login(self.user)
        return conversation, reverse("communication_reply", args=[conversation.uuid])

    def test_navigation_counts_unread_active_dialogs_per_user(self):
        conversation, _ = self._reply_form()
        ConversationMessage.objects.create(conversation=conversation, direction="in", body="Ещё один вопрос")
        closed = Conversation.objects.create(
            organization=self.org, connection=self.connection, external_id="closed",
            status=Conversation.STATUS_DONE,
        )
        ConversationMessage.objects.create(conversation=closed, direction="in", body="Из архива")
        outbound_only = Conversation.objects.create(
            organization=self.org, connection=self.connection, external_id="outbound-only",
        )
        ConversationMessage.objects.create(conversation=outbound_only, direction="out", body="Ответ")
        feed = reverse("communication_notification_feed")
        self.assertEqual(self.client.get(feed).json()["unread_dialogs"], 1)
        chat = reverse("communications_conversations")
        self.assertContains(self.client.get(f"{chat}?conversation={conversation.uuid}"), 'data-communication-unanswered>')
        self.assertEqual(self.client.get(feed).json()["unread_dialogs"], 0)

        other = Organization.objects.create(name="Synthetic second communications organization")
        OrganizationAccess.objects.create(organization=other, user=self.user, role="manager")
        CommunicationAccess.objects.update_or_create(
            organization=other, user=self.user, defaults={"can_view_conversations": True},
        )
        other_channel = CommunicationChannel.objects.create(organization=other, kind="max", name="MAX")
        other_connection = ChannelConnection.objects.create(channel=other_channel, external_id="999", name="Second MAX")
        other_conversation = Conversation.objects.create(
            organization=other, connection=other_connection, external_id="other",
        )
        ConversationMessage.objects.create(conversation=other_conversation, direction="in", body="Другая организация")
        self.assertEqual(self.client.get(feed, {"org_id": self.org.pk}).json()["unread_dialogs"], 0)
        self.assertEqual(self.client.get(feed, {"org_id": other.pk}).json()["unread_dialogs"], 1)
        self.assertEqual(self.client.get(feed, {"org_id": other.pk}).json()["unanswered_dialogs"], 1)
        scoped = self.client.get(f"{chat}?conversation={other_conversation.uuid}&org_id={other.pk}")
        self.assertContains(scoped, f'href="{chat}?org_id={other.pk}"')
        self.assertContains(scoped, f'{feed}?org_id={other.pk}')
        scoped_feed = self.client.get(feed, {"org_id": other.pk}).json()
        self.assertEqual(scoped_feed["unread_dialogs"], 0)
        self.assertEqual(scoped_feed["unanswered_dialogs"], 1)

        manager = get_user_model().objects.create_user("second-synthetic-dialog-reader")
        OrganizationAccess.objects.create(organization=self.org, user=manager, role="manager")
        CommunicationAccess.objects.update_or_create(
            organization=self.org, user=manager, defaults={"can_view_conversations": True},
        )
        self.client.force_login(manager)
        manager_feed = self.client.get(feed).json()
        self.assertEqual(manager_feed["unread_dialogs"], 1)
        self.assertEqual(manager_feed["unanswered_dialogs"], 1)
        self.assertEqual(self.client.get(feed, {"org_id": other.pk}).status_code, 403)
        CommunicationAccess.objects.filter(organization=self.org, user=manager).update(can_view_conversations=False)
        self.assertEqual(self.client.get(feed).status_code, 403)

    def test_mobile_inbox_loads_history_and_marks_read_only_on_open(self):
        conversation, _ = self._reply_form()
        index = reverse("communications_conversations")
        for headers in ({"sec-ch-ua-mobile": "?1"}, {"user-agent": "iPhone Mobile Safari"}):
            page = self.client.get(index, headers=headers)
            self.assertIsNone(page.context["selected"])
            self.assertEqual(page.context["selected_messages"], [])
            self.assertContains(page, 'data-pane="list"')
            self.assertEqual(page.context["conversations"][0].unread_count, 1)
        opened = self.client.get(index, {"conversation": conversation.uuid}, headers={"sec-ch-ua-mobile": "?1"})
        self.assertContains(opened, 'data-pane="chat"')
        self.assertTrue(opened.context["selected_messages"])
        self.assertEqual(opened.context["conversations"][0].unread_count, 0)

    def test_live_dialog_updates_keep_list_preview_and_read_receipts_separate(self):
        conversation, _ = self._reply_form()
        url = reverse("communication_dialog_updates")
        args = {"org_id": self.org.pk}
        page = self.client.get(url, args)
        self.assertEqual(page.status_code, 200)
        self.assertIn("no-store", page["Cache-Control"])
        self.assertNotIn("messages_html", page.json())
        self.assertIn('data-unread="1"', page.json()["rows_html"])
        args["list_version"] = page.json()["list_version"]
        self.assertEqual(self.client.get(url, args).json(), {"list_version": args["list_version"]})
        self.assertFalse(ConversationReadState.objects.filter(user=self.user, conversation=conversation).exists())

        first = conversation.messages.get()
        args.update(conversation=str(conversation.uuid), after=first.pk, count=1)
        second = ConversationMessage.objects.create(conversation=conversation, direction="in", body="Second <script>synthetic</script>")
        response = self.client.get(url, args).json()
        self.assertIn("&lt;script&gt;synthetic&lt;/script&gt;", response["messages_html"])
        self.assertNotIn(f'data-message-id="{first.pk}"', response["messages_html"])
        self.assertIn('data-unread="2"', response["rows_html"])
        following = ConversationMessage.objects.create(conversation=conversation, direction="in", body="Still unread")
        args.update(after=second.pk, count=2, list_version=response["list_version"])
        self.client.get(url, {**args, "seen": second.pk})
        self.assertFalse(ConversationReadState.objects.filter(user=self.user, conversation=conversation).exists())
        protected = TestClient(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(url, {"seen": second.pk}, query_params=args).status_code, 403)
        payload = self.client.post(url, {"seen": second.pk}, query_params=args).json()
        self.assertIn(f'data-message-id="{following.pk}"', payload["messages_html"])
        self.assertIn('data-unread="1"', payload["rows_html"])
        state = ConversationReadState.objects.get(user=self.user, conversation=conversation)
        self.assertEqual(state.last_read_at, second.created_at)
        self.client.post(url, {"seen": first.pk}, query_params=args)
        state.refresh_from_db()
        self.assertEqual(state.last_read_at, second.created_at)

    def test_open_dialog_acknowledges_only_rendered_history(self):
        from pool_service.avito_outbox_display import conversation_context

        conversation, _ = self._reply_form()
        first = conversation.messages.get()

        def new_message_after_snapshot(*args, **kwargs):
            context = conversation_context(*args, **kwargs)
            ConversationMessage.objects.create(conversation=conversation, direction="in", body="Arrived after snapshot")
            return context

        with patch("pool_service.communication_views.conversation_context", side_effect=new_message_after_snapshot):
            page = self.client.get(reverse("communications_conversations"), {"conversation": conversation.uuid})
        self.assertNotContains(page, "Arrived after snapshot")
        self.assertEqual(ConversationReadState.objects.get(user=self.user, conversation=conversation).last_read_at,
                         first.created_at)
        rows = self.client.get(reverse("communication_dialog_updates"), {"org_id": self.org.pk}).json()["rows_html"]
        self.assertIn('data-unread="1"', rows)

    def test_live_dialog_refreshes_only_requested_outgoing_delivery_states(self):
        conversation, _ = self._reply_form()
        sent = ConversationMessage.objects.create(conversation=conversation, direction="out", body="Synthetic reply",
                                                 delivery_status="pending")
        ConversationMessage.objects.filter(pk=sent.pk).update(delivery_status="delivered", delivered_at=timezone.now())
        args = {"org_id": self.org.pk, "conversation": conversation.uuid, "after": sent.pk, "count": 2,
                "pending": f"{sent.pk},{conversation.messages.first().pk}"}
        payload = self.client.get(reverse("communication_dialog_updates"), args).json()
        self.assertEqual(set(payload["delivery"]), {str(sent.pk)})
        self.assertIn("Отправлено в MAX", payload["delivery"][str(sent.pk)])
        self.assertNotIn("messages_html", payload)

    def test_live_dialog_updates_are_scoped_and_recover_a_missing_message(self):
        conversation, _ = self._reply_form()
        url = reverse("communication_dialog_updates")
        args = {"org_id": self.org.pk, "conversation": conversation.uuid}
        original = conversation.messages.get()
        response = self.client.post(url, {"seen": original.pk}, query_params={**args, "after": original.pk, "count": 0}).json()
        self.assertTrue(response["reset"])
        self.assertFalse(ConversationReadState.objects.filter(user=self.user, conversation=conversation).exists())
        self.assertIn(f'data-message-id="{original.pk}"', response["messages_html"])
        self.assertEqual(self.client.get(url, {**args, "after": "invalid"}).status_code, 400)
        other = Organization.objects.create(name="Synthetic other inbox")
        channel = CommunicationChannel.objects.create(organization=other, kind="website", name="Other inbox")
        connection = ChannelConnection.objects.create(channel=channel, name="Other")
        foreign = Conversation.objects.create(organization=other, connection=connection, external_id="foreign")
        self.assertEqual(self.client.get(url, {**args, "conversation": foreign.uuid}).status_code, 404)
        self.assertEqual(self.client.get(url, {**args, "org_id": other.pk}).status_code, 403)
        self.assertFalse(ConversationReadState.objects.filter(user=self.user, conversation=foreign).exists())
        stale_client = Client.objects.create(organization=other, name="Legacy foreign client")
        Conversation.objects.filter(pk=conversation.pk).update(client=stale_client)
        before = self.client.get(url, args).json()
        self.assertNotIn(stale_client.name, before["rows_html"])
        Client.objects.filter(pk=stale_client.pk).update(name="Renamed foreign client")
        self.assertEqual(self.client.get(url, args).json()["list_version"], before["list_version"])
        CommunicationAccess.objects.filter(organization=self.org, user=self.user).update(can_view_conversations=False)
        self.assertEqual(self.client.get(url, args).status_code, 403)

    def test_unanswered_queue_only_clears_after_a_reply_or_completion(self):
        conversation, _ = self._reply_form()
        index = reverse("communications_conversations")
        unanswered = {"dialogs": "unanswered", "org_id": self.org.pk}
        feed = reverse("communication_notification_feed")

        def count():
            return self.client.get(feed).json()["unanswered_dialogs"]

        def listed():
            page = self.client.get(index, unanswered)
            self.assertTrue(page.context["show_unanswered_dialogs"])
            return [item.pk for item in page.context["conversations"]]

        self.assertContains(self.client.get(index), "Без ответа")
        self.assertEqual(listed(), [conversation.pk])
        self.assertEqual(self.client.get(feed).json()["unread_dialogs"], 1)
        self.client.get(index, {**unanswered, "conversation": conversation.uuid})
        self.assertEqual(self.client.get(feed).json()["unread_dialogs"], 0)
        self.assertEqual(count(), 1)

        ConversationMessage.objects.create(
            conversation=conversation, direction=ConversationMessage.DIRECTION_OUT,
            delivery_status=ConversationMessage.DELIVERY_DELIVERED, max_details={"contact_request": True},
        )
        reply = ConversationMessage.objects.create(
            conversation=conversation, direction=ConversationMessage.DIRECTION_OUT,
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        )
        for status in (
            ConversationMessage.DELIVERY_PENDING, ConversationMessage.DELIVERY_SENDING,
            ConversationMessage.DELIVERY_FAILED,
        ):
            ConversationMessage.objects.filter(pk=reply.pk).update(delivery_status=status)
            self.assertEqual(count(), 1)

        # A reply that leaves the filter is still visible in the active list.
        answered_at = timezone.now()
        ConversationMessage.objects.filter(pk=reply.pk).update(
            delivery_status=ConversationMessage.DELIVERY_DELIVERED, intake_occurred_at=answered_at,
        )
        self.assertEqual(count(), 0)
        self.assertEqual(listed(), [])
        self.assertContains(self.client.get(index), str(conversation.uuid))
        self.assertIsNone(self.client.get(index, {**unanswered, "conversation": conversation.uuid}).context["selected"])

        # A delayed, older incoming event does not create a fresh obligation.
        ConversationMessage.objects.create(
            conversation=conversation, direction=ConversationMessage.DIRECTION_IN,
            intake_occurred_at=answered_at - timedelta(minutes=2),
        )
        self.assertEqual(count(), 0)
        ConversationMessage.objects.create(
            conversation=conversation, direction=ConversationMessage.DIRECTION_IN,
            intake_occurred_at=answered_at + timedelta(seconds=1),
        )
        self.assertEqual(count(), 1)
        finish = reverse("communication_update", args=[conversation.uuid])
        destination = index + f"?org_id={self.org.pk}&dialogs=unanswered"
        self.assertRedirects(
            self.client.post(finish + f"?org_id={self.org.pk}&dialogs=unanswered", {"status": "done"}),
            destination,
        )
        self.assertEqual(count(), 0)
        self.assertEqual(listed(), [])

    def test_replies_to_existing_dialog_once_and_shows_max_icon(self):
        conversation, url = self._reply_form()
        dialog_url = reverse("communications_conversations") + f"?conversation={conversation.uuid}&org_id={self.org.pk}"
        self.assertContains(self.client.get(dialog_url), "max-channel.svg", count=2)
        self.assertNotContains(self.client.get(dialog_url), 'name="max_reply_id"')
        self.connection.max_bot_token_encrypted = encrypt_secret("synthetic-bot-token")
        self.connection.save(update_fields=["max_bot_token_encrypted"])
        self.assertContains(self.client.get(dialog_url), 'name="max_reply_id"')

        form = {"max_reply_id": uuid4().hex, "body": "Ответ о бассейне"}
        with patch("pool_service.communication_max_outbound._request", return_value={
            "message": {"body": {"mid": "provider-out-id"}},
        }) as provider, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url, form).status_code, 302)
            self.assertEqual(self.client.post(url, form).status_code, 302)
        method, path, token, body = provider.call_args.args
        self.assertEqual((method, path, token), ("POST", "/messages?user_id=500", "synthetic-bot-token"))
        self.assertEqual(json.loads(body), {"text": form["body"]})
        sent = conversation.messages.get(direction=ConversationMessage.DIRECTION_OUT)
        self.assertEqual(sent.external_id, "provider-out-id")
        self.assertEqual(sent.delivery_attempts, 1)
        self.assertIsNotNone(sent.delivered_at)
        self.assertContains(self.client.get(dialog_url), "Отправлено в MAX")
        provider.assert_called_once()

    def test_rejects_reply_without_a_verified_token_and_does_not_retry_uncertain_delivery(self):
        conversation, url = self._reply_form()
        form = {"max_reply_id": uuid4().hex, "body": "Ответ"}
        self.assertEqual(self.client.post(url, form).status_code, 302)
        self.assertFalse(conversation.messages.filter(direction=ConversationMessage.DIRECTION_OUT).exists())

        self.connection.max_bot_token_encrypted = encrypt_secret("synthetic-bot-token")
        self.connection.save(update_fields=["max_bot_token_encrypted"])
        with patch("pool_service.communication_max_outbound._request",
                   side_effect=MaxApiError("max_delivery_unknown")) as provider:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.client.post(url, form).status_code, 302)
            sent = conversation.messages.get(direction=ConversationMessage.DIRECTION_OUT)
            dispatch_reply(self.connection.pk, sent.pk)
        provider.assert_called_once()
        self.assertEqual(sent.delivery_status, ConversationMessage.DELIVERY_FAILED)
        self.assertEqual(sent.delivery_error, "max_delivery_unknown")
        self.assertIsNone(sent.delivered_at)

    def test_only_channel_managers_can_store_token_and_it_must_match_bot(self):
        self.client.force_login(self.user)
        url = reverse("communication_max_configure", args=[self.connection.pk])
        token = "synthetic-bot-token"
        webhook_hash = self.connection.api_token_hash
        with patch("pool_service.communication_max_outbound._request") as provider:
            self.assertEqual(self.client.post(url, {"max_bot_token": token}).status_code, 403)
            provider.assert_not_called()
            OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
            CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_manage_channels=True)
            provider.return_value = {"is_bot": True, "user_id": 12346}
            self.assertEqual(self.client.post(url, {"max_bot_token": token}).status_code, 302)
            self.connection.refresh_from_db()
            self.assertFalse(self.connection.max_bot_token_encrypted)

            provider.return_value = {"is_bot": True, "user_id": 12345}
            self.assertEqual(self.client.post(url, {"max_bot_token": token}).status_code, 302)
        self.connection.refresh_from_db()
        self.assertNotEqual(self.connection.max_bot_token_encrypted, token)
        self.assertEqual(decrypt_secret(self.connection.max_bot_token_encrypted), token)
        self.assertEqual(self.connection.api_token_hash, webhook_hash)
        self.assertEqual(self.post_update().status_code, 200)

    def test_max_tls_uses_verified_provider_root_and_checks_hostname(self):
        der = ssl.PEM_cert_to_DER_cert(MAX_CA.read_text(encoding="ascii"))
        self.assertEqual(
            hashlib.sha256(der).hexdigest(),
            "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31",
        )
        context = max_api_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIn(der, context.get_ca_certs(binary_form=True))


    def test_max_voice_is_cached_privately_and_webhook_redelivery_is_idempotent(self):
        body = self.payload["message"]["body"]
        body["attachments"] = [{
            "type": "audio", "payload": {"url": "https://cdn.max.ru/temporary", "token": "synthetic-voice", "id": 901},
        }]
        body["mid"] = "mid.synthetic-audio"
        self.connection.max_bot_token_encrypted = encrypt_secret("synthetic-token")
        self.connection.save(update_fields=["max_bot_token_encrypted"])
        self.assertEqual(self.post_update().status_code, 200)
        self.assertEqual(self.post_update().status_code, 200)
        attachment = MessageAttachment.objects.get(message__conversation__connection=self.connection)
        self.assertEqual(attachment.max_media_kind, "audio")
        self.assertFalse(attachment.original)
        self.client.force_login(self.user)
        url = reverse("communication_attachment", args=[attachment.pk]) + f"?org_id={self.org.pk}&inline=1"
        audio = b"synthetic-audio-bytes"

        body["attachments"][0]["payload"].update(url="https://cdn.max.ru/temporary", token="replaced-voice", id=902)
        with patch("pool_service.communication_max_outbound._request", return_value=self.payload["message"]), patch(
            "pool_service.communication_max_media._download",
        ) as transfer:
            self.assertEqual(self.client.get(url).status_code, 502)
            transfer.assert_not_called()
        body["attachments"][0]["payload"].update(url="https://cdn.max.ru/temporary?fresh=signature", token="different-signed-token", id=901)

        def download(remote, destination, kind):
            self.assertEqual((remote, kind), ("https://cdn.max.ru/temporary?fresh=signature", "audio"))
            destination.write(audio)
            return len(audio), "audio/ogg"

        field = MessageAttachment._meta.get_field("original")
        with TemporaryDirectory() as directory, patch.object(field, "storage", FileSystemStorage(location=directory)):
            with patch("pool_service.communication_max_outbound._request",
                       return_value=self.payload["message"]) as api, patch(
                           "pool_service.communication_max_media._download", side_effect=download) as transfer:
                first, second = self.client.get(url), self.client.get(url)
                for response in (first, second):
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Content-Type"], "audio/ogg")
                    self.assertEqual(response["Cache-Control"], "private, no-store")
                    self.assertEqual(b"".join(response.streaming_content), audio)
                    response.close()
                ranged = self.client.get(url, HTTP_RANGE="bytes=0-1")
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged["Accept-Ranges"], "bytes")
                self.assertEqual(ranged["Content-Range"], f"bytes 0-1/{len(audio)}")
                self.assertEqual(b"".join(ranged.streaming_content), audio[:2])
                ranged.close()
                suffix = self.client.get(url, HTTP_RANGE="bytes=-2")
                self.assertEqual(suffix.status_code, 206)
                self.assertEqual(b"".join(suffix.streaming_content), audio[-2:])
                suffix.close()
                invalid = self.client.get(url, HTTP_RANGE="bytes=100-")
                self.assertEqual(invalid.status_code, 416)
                self.assertEqual(invalid["Content-Range"], f"bytes */{len(audio)}")
                api.assert_called_once_with("GET", "/messages/mid.synthetic-audio", "synthetic-token")
                transfer.assert_called_once()

    def test_max_photo_upload_and_attachment_message_are_dispatched_once(self):
        conversation, url = self._reply_form()
        self.connection.max_bot_token_encrypted = encrypt_secret("synthetic-token")
        self.connection.save(update_fields=["max_bot_token_encrypted"])
        field = MessageAttachment._meta.get_field("original")
        # Small valid GIF. The upload endpoint response uses the MAX photo-token map.
        gif = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
        key = uuid4().hex
        with TemporaryDirectory() as directory, patch.object(field, "storage", FileSystemStorage(location=directory)):
            with patch("pool_service.communication_max_outbound._request", side_effect=[
                {"url": "https://upload.max.ru/synthetic"},
                MaxApiError("max_attachment_processing"),
                {"message": {"body": {"mid": "mid.photo-out"}}},
            ]) as api, patch("pool_service.communication_max_media._upload",
                            return_value=b'{"photos":{"one":{"token":"synthetic-photo"}}}') as upload, patch(
                                "pool_service.communication_max_outbound.sleep",
                            ) as delay, self.captureOnCommitCallbacks(execute=True):
                for _ in range(2):
                    form = {"max_reply_id": key, "attachments": SimpleUploadedFile("p.gif", gif, "image/gif")}
                    self.assertEqual(self.client.post(url, form).status_code, 302)
            self.assertEqual(api.call_args_list[0].args, ("POST", "/uploads?type=image", "synthetic-token"))
            upload.assert_called_once()
            delay.assert_called_once()
            final = api.call_args_list[-1].args
            self.assertEqual(final[:3], ("POST", "/messages?user_id=500", "synthetic-token"))
            self.assertEqual(json.loads(final[3]), {"attachments": [{"type": "image", "payload": {"token": "synthetic-photo"}}]})
            sent = conversation.messages.get(direction=ConversationMessage.DIRECTION_OUT)
            self.assertEqual(sent.delivery_status, ConversationMessage.DELIVERY_DELIVERED)
            self.assertTrue(sent.attachments.get().original)

    def test_max_contact_request_and_signed_number_stay_in_current_dialog(self):
        conversation, url = self._reply_form()
        token = "synthetic-token"
        self.connection.max_bot_token_encrypted = encrypt_secret(token)
        self.connection.save(update_fields=["max_bot_token_encrypted"])
        with patch("pool_service.communication_max_outbound._request", return_value={
            "message": {"body": {"mid": "mid.request-phone"}},
        }) as api, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url, {
                "max_reply_id": uuid4().hex, "request_phone": "1",
            }).status_code, 302)
        keyboard = json.loads(api.call_args.args[3])["attachments"][0]
        self.assertEqual(keyboard["payload"]["buttons"][0][0]["type"], "request_contact")

        card = "BEGIN:VCARD\r\nVERSION:3.0\r\nTEL;TYPE=cell:79990000000\r\nFN:Test\r\nEND:VCARD\r\n"
        signature = hmac.digest(token.encode("utf-8"), card.encode("utf-8"), "sha256").hex()
        body = self.payload["message"]["body"]
        body.update(mid="mid.shared-phone", text="", attachments=[{
            "type": "contact", "payload": {"vcf_info": card, "hash": signature, "max_info": {"user_id": 500}},
        }])
        self.assertEqual(self.post_update().status_code, 200)
        shared = conversation.messages.get(external_id="mid.shared-phone")
        self.assertEqual(shared.max_details, {"shared_phone": "+79990000000"})
        self.assertNotIn("+79990000000", shared.body)
        conversation.refresh_from_db()
        self.assertFalse(conversation.provider_phone)
        card_url = reverse("client_detail", args=[conversation.client_id])
        self.assertContains(self.client.get(card_url), "Телефон из MAX")
        self.assertContains(self.client.get(card_url), "+79990000000")
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_conversations=False)
        self.assertNotContains(self.client.get(card_url), "+79990000000")
        body["mid"] = "mid.invalid-card"
        body["attachments"][0]["payload"]["max_info"]["user_id"] = 501
        self.assertEqual(self.post_update().status_code, 200)
        self.assertEqual(conversation.messages.get(external_id="mid.invalid-card").max_details, {})

    def test_max_media_only_accepts_https_from_provider_domains(self):
        self.assertEqual(media_url("https://cdn.max.ru/file?a=1"), ("cdn.max.ru", "/file?a=1"))
        original = {"photo_id": 42, "url": "https://cdn.max.ru/one"}
        renewed = {"photo_id": 42, "url": "https://cdn.max.ru/renewed"}
        self.assertIn(media_fingerprints("image", original)[0], media_fingerprints("image", renewed))
        first = {"url": "https://cdn.max.ru/download?id=111"}
        second = {"url": "https://cdn.max.ru/download?id=222"}
        self.assertNotEqual(media_fingerprints("audio", first), media_fingerprints("audio", second))
        self.assertIn(
            media_fingerprints("audio", first)[0],
            media_fingerprints("audio", {**first, "token": "included-only-on-refresh"}),
        )
        for url in ("http://cdn.max.ru/file", "https://foreign.example/file",
                    "https://cdn.max.ru.foreign.example/file", "https://someone@max.ru/file"):
            with self.subTest(url=url), self.assertRaises(MaxMediaError):
                media_url(url)


    def test_auto_contact_request_is_queued_once_per_dialog_even_on_webhook_replay(self):
        self.connection.max_bot_token_encrypted = encrypt_secret("synthetic-token")
        self.connection.settings = {"max_auto_request_phone": True}
        self.connection.save(update_fields=["max_bot_token_encrypted", "settings"])
        with patch("pool_service.communication_max_outbound._request", return_value={
            "message": {"body": {"mid": "mid.auto-phone"}},
        }) as api, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post_update().status_code, 200)
            self.assertEqual(self.post_update().status_code, 200)
            self.payload["message"]["body"]["mid"] = "mid.another-incoming"
            self.assertEqual(self.post_update().status_code, 200)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[:3], ("POST", "/messages?user_id=500", "synthetic-token"))
        keyboard = json.loads(api.call_args.args[3])["attachments"][0]
        self.assertEqual(keyboard["payload"]["buttons"][0][0]["type"], "request_contact")
        request = ConversationMessage.objects.get(direction=ConversationMessage.DIRECTION_OUT)
        self.assertTrue(request.max_details["automatic"])
        self.assertIsNone(request.sent_by_id)
        self.assertEqual(request.delivery_status, ConversationMessage.DELIVERY_DELIVERED)

    def test_completion_separates_lists_and_new_incoming_reopens_the_dialog(self):
        conversation, _ = self._reply_form()
        index = reverse("communications_conversations")
        finish = reverse("communication_update", args=[conversation.uuid])
        self.assertContains(self.client.get(index), "Завершить диалог")
        self.assertRedirects(self.client.post(finish, {"status": "done"}), index + f"?org_id={self.org.pk}")
        self.assertNotContains(self.client.get(index), str(conversation.uuid))
        self.assertContains(self.client.get(index + "?dialogs=closed"), str(conversation.uuid))
        direct = self.client.get(index + f"?conversation={conversation.uuid}")
        self.assertTrue(direct.context["show_closed_dialogs"])
        self.assertNotContains(direct, "Завершить диалог")
        self.payload["message"]["body"]["mid"] = "mid.reopened"
        self.assertEqual(self.post_update().status_code, 200)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.STATUS_NEW)
        self.assertContains(self.client.get(index), str(conversation.uuid))
