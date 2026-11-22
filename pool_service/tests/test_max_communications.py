import json

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from pool_service.communication_models import ChannelConnection, CommunicationChannel, Conversation, ConversationMessage
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
