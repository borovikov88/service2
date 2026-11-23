import hashlib
import hmac
import json
import ssl
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from pool_service.communication_max_media import MaxMediaError, media_fingerprint, media_url
from pool_service.communication_max_outbound import MAX_CA, MaxApiError, dispatch_reply, max_api_ssl_context
from pool_service.communication_secrets import decrypt_secret, encrypt_secret
from pool_service.models import OrganizationAccess
from pool_service.communication_models import ChannelConnection, CommunicationAccess, CommunicationChannel, Conversation, ConversationMessage, MessageAttachment
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
        body["attachments"] = [{"type": "audio", "payload": {"url": "https://cdn.max.ru/temporary"}}]
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

        body["attachments"][0]["payload"]["url"] = "https://cdn.max.ru/replaced"
        with patch("pool_service.communication_max_outbound._request", return_value=self.payload["message"]), patch(
            "pool_service.communication_max_media._download",
        ) as transfer:
            self.assertEqual(self.client.get(url).status_code, 502)
            transfer.assert_not_called()
        body["attachments"][0]["payload"]["url"] = "https://cdn.max.ru/temporary?fresh=signature"

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
        body["mid"] = "mid.invalid-card"
        body["attachments"][0]["payload"]["max_info"]["user_id"] = 501
        self.assertEqual(self.post_update().status_code, 200)
        self.assertEqual(conversation.messages.get(external_id="mid.invalid-card").max_details, {})

    def test_max_media_only_accepts_https_from_provider_domains(self):
        self.assertEqual(media_url("https://cdn.max.ru/file?a=1"), ("cdn.max.ru", "/file?a=1"))
        self.assertEqual(
            media_fingerprint("image", {"photo_id": 42, "url": "https://cdn.max.ru/one"}),
            media_fingerprint("image", {"photo_id": 42, "url": "https://cdn.max.ru/renewed"}),
        )
        for url in ("http://cdn.max.ru/file", "https://foreign.example/file",
                    "https://cdn.max.ru.foreign.example/file", "https://someone@max.ru/file"):
            with self.subTest(url=url), self.assertRaises(MaxMediaError):
                media_url(url)
