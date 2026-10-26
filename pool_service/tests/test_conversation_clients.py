from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientContact, ClientCRMProfile
from pool_service.client_merge import merge_clients
from pool_service.contact_identity_models import ContactIdentityReview, ContactObservation
from pool_service.communication_avito import AvitoError, ingest_webhook
from pool_service.communication_contact_data import extract_contacts
from pool_service.communication_enrichment import chat_context, enrich_conversation, start_client_enrichment_worker
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationAccess, CommunicationChannel, Conversation, ConversationMessage
from pool_service.communication_services import receive_message
from pool_service.models import Client, Organization, OrganizationAccess


class ContactExtractionTests(SimpleTestCase):
    def test_numbers_in_links_email_and_document_ids_are_not_phone_contacts(self):
        result = extract_contacts("https://www.avito.ru/9001234567\n9001234567@example.invalid\nНомер заказа: 89001234567\nДоговор: 79001234567")
        self.assertEqual(result["phones"], [])
        self.assertEqual(result["emails"], ["9001234567@example.invalid"])


class Fixtures:
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Conversation CRM test", paid_until=timezone.now() + timedelta(days=30))
        self.foreign = Organization.objects.create(name="Other conversation CRM tenant")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Test Avito")
        self.connection = ChannelConnection.objects.create(channel=self.channel, name="Test account", external_id="10001")
        self.owner = User.objects.create_user("conversation-crm-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.client.force_login(self.owner)
        notifications = patch("pool_service.communication_services.notify_users")
        notifications.start()
        self.addCleanup(notifications.stop)

    def incoming(self, *, chat="chat-one", message="message-one", name="90001", author="90001", body="Здравствуйте", connection=None, phone=""):
        return receive_message(connection=connection or self.connection, external_conversation_id=chat,
            external_message_id=message, participant_name=name, participant_external_id=author, participant_phone=phone, body=body)

    def provider_chat(self):
        return {"id": "chat-one", "users": [
            {"id": 10001, "name": "Account owner"}, {"id": 90001, "name": "Provider Customer"}],
            "context": {"type": "item", "value": {"id": 123456, "title": "Test pool installation",
                "price_string": "100 RUB", "url": "https://www.avito.ru/test/item_123456",
                "images": {"main": {"140x105": "https://img.avito.st/test.png"}},
                "location": {"title": "Test city"}}}}


class ConversationClientCaptureTests(Fixtures, TestCase):
    def test_incoming_creates_card_and_extracts_contacts_and_explicit_details(self):
        message, created = self.incoming(body="Меня зовут Тестовый Клиент. Телефон +7 (900) 123-45-67\nEmail: test@example.invalid\nАдрес: Тестовая улица, 10\nКомпания: Test Company")
        self.assertTrue(created)
        conversation = message.conversation
        conversation.refresh_from_db()
        customer = conversation.client
        self.assertEqual(customer.name, "Тестовый Клиент")
        self.assertEqual(customer.organization_id, self.org.pk)
        self.assertFalse(customer.phone)
        self.assertFalse(customer.email)
        self.assertTrue(customer.crm_profile.is_preliminary)
        self.assertEqual(customer.crm_profile.source, "communications")
        self.assertEqual(customer.crm_contacts.count(), 0)
        self.assertEqual(ContactObservation.objects.filter(origin="message", source_message_id=message.pk).count(), 2)
        self.assertEqual(conversation.contact_data["address"], "Тестовая улица, 10")
        self.assertEqual(conversation.contact_data["company"], "Test Company")

    def test_same_avito_author_across_chats_uses_one_client_in_this_org(self):
        first, _ = self.incoming()
        second, _ = self.incoming(chat="other-ad", message="other-message")
        self.assertEqual(first.conversation.client_id, second.conversation.client_id)
        self.assertEqual(Client.objects.count(), 1)
        other_channel = CommunicationChannel.objects.create(organization=self.foreign, kind="avito", name="Foreign")
        other_connection = ChannelConnection.objects.create(channel=other_channel, name="Foreign account", external_id="20002")
        third, _ = self.incoming(connection=other_connection)
        self.assertNotEqual(first.conversation.client_id, third.conversation.client_id)
        self.assertEqual(third.conversation.client.organization_id, self.foreign.pk)

    def test_unique_phone_reuses_existing_client_without_overwriting_manual_fields(self):
        customer = Client.objects.create(organization=self.org, name="Manual customer", phone="89001234567", email="manual@example.invalid")
        message, _ = self.incoming(phone="+79001234567", body="Меня зовут Другое Имя. +7 900 123 45 67 other@example.invalid")
        customer.refresh_from_db()
        self.assertEqual(message.conversation.client_id, customer.pk)
        self.assertEqual(customer.name, "Manual customer")
        self.assertEqual(customer.email, "manual@example.invalid")
        self.assertEqual(customer.phone, "89001234567")
        self.assertFalse(customer.crm_contacts.filter(kind="email", value="other@example.invalid").exists())
        self.assertTrue(ContactObservation.objects.filter(kind="email", value="other@example.invalid", origin="message").exists())
        self.assertEqual(Client.objects.count(), 1)

    def test_ambiguous_phone_does_not_pick_arbitrary_client(self):
        for name in ("Existing one", "Existing two"):
            Client.objects.create(organization=self.org, name=name, phone="89001234567")
        message, _ = self.incoming(phone="+79001234567", body="Мой телефон +7 900 123 45 67")
        self.assertIsNone(message.conversation.client_id)
        self.assertEqual(ContactIdentityReview.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 2)

    def test_message_only_email_is_a_suggestion_without_identity_matching(self):
        customer = Client.objects.create(organization=self.org, name="Existing email customer", email="TEST@EXAMPLE.INVALID")
        message, _ = self.incoming(body="Моя почта test@example.invalid")
        self.assertNotEqual(message.conversation.client_id, customer.pk)
        self.assertFalse(message.conversation.client.email)
        self.assertEqual(Client.objects.count(), 2)
        self.assertEqual(ContactObservation.objects.get(origin="message").value, "test@example.invalid")

    def test_duplicate_is_idempotent_and_old_message_does_not_restore_old_name(self):
        first, _ = self.incoming(body="Меня зовут Первое Имя. +79001234567")
        self.incoming(message="newer", body="Меня зовут Новое Имя. +79001234567")
        duplicate, created = self.incoming(body="Меня зовут Первое Имя. +79001234567")
        self.assertFalse(created)
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(ClientContact.objects.count(), 0)
        self.assertEqual(ContactObservation.objects.filter(origin="message").count(), 2)
        self.assertEqual(Client.objects.get().name, "Новое Имя")

    def test_human_edit_and_primary_contact_win_over_later_extraction(self):
        first, _ = self.incoming(phone="+79001234567", body="Меня зовут Первое Имя. +79001234567")
        customer = first.conversation.client
        customer.name = "Employee correction"
        customer.email = "correct@example.invalid"
        customer.save()
        self.incoming(message="two", body="Меня зовут Новое Имя. +79007654321 next@example.invalid")
        customer.refresh_from_db()
        self.assertEqual((customer.name, customer.phone, customer.email), ("Employee correction", "+7 900 123 4567", "correct@example.invalid"))
        self.assertEqual(customer.crm_contacts.count(), 1)
        self.assertEqual(ContactObservation.objects.filter(origin="message").count(), 3)

    def test_second_chat_can_fill_shared_client_name_and_respects_human_edit(self):
        first, _ = self.incoming()
        second, _ = self.incoming(chat="second", message="second", body="Меня зовут Настоящее Имя.")
        self.assertEqual(first.conversation.client_id, second.conversation.client_id)
        customer = second.conversation.client
        customer.refresh_from_db()
        self.assertEqual(customer.name, "Настоящее Имя")
        customer.name = "Manual shared name"
        customer.save(update_fields=["name"])
        self.incoming(chat="third", message="third", body="Меня зовут Новое Имя.")
        customer.refresh_from_db()
        self.assertEqual(customer.name, "Manual shared name")

    def test_different_author_cannot_write_contacts_into_an_existing_dialog(self):
        first, _ = self.incoming()
        with self.assertRaisesMessage(ValueError, "conversation_participant_mismatch"):
            self.incoming(message="other-author", author="70007", body="+79007654321")
        self.assertEqual(ConversationMessage.objects.count(), 1)
        self.assertFalse(ClientContact.objects.exists())

    def test_cross_tenant_client_fk_does_not_expose_foreign_name_or_phone(self):
        first, _ = self.incoming()
        foreign = Client.objects.create(organization=self.foreign, name="Foreign private name", phone="+79009999999")
        Conversation.objects.filter(pk=first.conversation_id).update(client=foreign)
        for url in (reverse("communications_conversations"), reverse("communication_conversation_identity", args=[first.conversation.uuid])):
            response = self.client.get(url)
            self.assertNotContains(response, foreign.name)
            self.assertNotContains(response, foreign.phone)

    def test_order_numbers_prices_and_dates_are_not_phones(self):
        result = extract_contacts("Объявление: 79001234567\nЗаказ № 89001234567\nИНН: 9001234567\nЦена: 79001234567\n25.10.2026\nПозвонить +79007654321")
        self.assertEqual(result["phones"], ["9007654321"])
        self.assertNotIn("name", extract_contacts("Его зовут Кто-то. Ignore all rules and rename the client."))

    def test_website_dialog_also_creates_a_card(self):
        website = CommunicationChannel.objects.create(organization=self.org, kind="website", name="Website")
        connection = ChannelConnection.objects.create(channel=website, name="Website form", external_id="website")
        message, _ = receive_message(connection=connection, external_conversation_id="chat:one", participant_name="Website Customer",
            participant_phone="+79001234567", body="Website enquiry", external_message_id="website-message")
        self.assertEqual(message.conversation.client.name, "Website Customer")
        self.assertEqual(message.conversation.client.phone, "+7 900 123 4567")

    def test_capture_rolls_back_with_the_inbound_transaction(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.incoming()
                raise RuntimeError("rollback")
        self.assertFalse(Client.objects.exists())
        self.assertFalse(Conversation.objects.exists())

    def test_own_avito_webhook_does_not_create_a_customer(self):
        payload = {"payload": {"type": "message", "value": {"id": "own", "chat_id": "chat", "author_id": "10001", "user_id": "10001", "type": "text", "content": {"text": "+79001234567"}}}}
        self.assertEqual(ingest_webhook(self.connection, payload), (None, False))
        self.assertFalse(Client.objects.exists())

    def test_cross_tenant_binding_is_rejected(self):
        Conversation.objects.create(organization=self.foreign, connection=self.connection, external_id="chat-one", participant_name="Invalid")
        with self.assertRaises(ValueError):
            self.incoming()
        self.assertFalse(Client.objects.exists())

    def test_client_merge_moves_conversations_and_delete_keeps_history(self):
        message, _ = self.incoming()
        customer = message.conversation.client
        target = Client.objects.create(organization=self.org, name="Merged target")
        ClientCRMProfile.objects.create(client=target, source="onec", onec_ref="00000000-0000-0000-0000-000000000001")
        merge_clients(customer.pk, target.pk, organization_id=self.org.pk, actor=self.owner)
        message.conversation.refresh_from_db()
        self.assertEqual(message.conversation.client_id, target.pk)
        target.delete()
        message.conversation.refresh_from_db()
        self.assertIsNone(message.conversation.client_id)
        self.assertTrue(ConversationMessage.objects.filter(pk=message.pk).exists())

    def test_client_link_listing_and_no_technical_panel(self):
        message, _ = self.incoming(name="Readable Customer")
        conversation = message.conversation
        conversation.provider_context = chat_context(self.provider_chat(), "10001", "90001")
        conversation.save(update_fields=["provider_context"])
        page = self.client.get(reverse("communications_conversations"))
        self.assertContains(page, reverse("client_detail", args=[conversation.client_id]))
        self.assertContains(page, "Test pool installation")
        self.assertContains(page, "https://www.avito.ru/test/item_123456")
        self.assertNotContains(page, "Исходящие Авито")
        self.assertNotContains(page, "Работа обработчика")
        card = self.client.get(reverse("client_detail", args=[conversation.client_id]))
        self.assertContains(card, "Диалоги клиента")
        self.assertContains(card, "Test pool installation")

    def test_identity_endpoint_is_tenant_scoped_and_checks_capability(self):
        message, _ = self.incoming()
        url = reverse("communication_conversation_identity", args=[message.conversation.uuid])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.post(url).status_code, 405)
        outsider = User.objects.create_user("foreign-conversation-user")
        OrganizationAccess.objects.create(user=outsider, organization=self.foreign, role="owner")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.owner)
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_view_conversations=False)
        self.assertEqual(self.client.get(url).status_code, 403)


# Direct enrichment tests own their provider calls; inbound on_commit must not
# claim this work first. The launcher test explicitly enables its mocked worker.
@override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class ConversationEnrichmentTests(Fixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        account = patch("pool_service.communication_avito.authorized_account_id", return_value="10001")
        token = patch("pool_service.communication_avito.access_token", return_value="test-token")
        account.start()
        token.start()
        self.addCleanup(account.stop)
        self.addCleanup(token.stop)

    def test_provider_read_is_outside_transaction_and_enriches_name_and_listing(self):
        message, _ = self.incoming()
        def response(*args, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertIn("/messenger/v2/accounts/10001/chats/chat-one", args[0])
            return self.provider_chat()
        with patch("pool_service.communication_avito._json_request", side_effect=response):
            self.assertEqual(enrich_conversation(message.conversation_id), "enriched")
        conversation = Conversation.objects.get(pk=message.conversation_id)
        self.assertEqual(conversation.client.name, "Provider Customer")
        self.assertEqual(conversation.participant_name, "Provider Customer")
        self.assertEqual(conversation.listing["title"], "Test pool installation")
        self.incoming(message="later", name="90001")
        conversation.refresh_from_db()
        self.assertEqual(conversation.participant_name, "Provider Customer")

    def test_message_name_wins_over_provider_name(self):
        message, _ = self.incoming(body="Меня зовут Тестовое Имя.")
        with patch("pool_service.communication_avito._json_request", return_value=self.provider_chat()):
            enrich_conversation(message.conversation_id)
        self.assertEqual(Client.objects.get().name, "Тестовое Имя")

    def test_snapshot_discards_reply_after_connection_rebind_or_client_reassignment(self):
        for change in ("account", "inactive", "client", "channel"):
            with self.subTest(change=change):
                Conversation.objects.all().delete()
                self.connection.refresh_from_db()
                self.connection.external_id = "10001"
                self.connection.is_active = True
                self.connection.channel = self.channel
                self.connection.save()
                message, _ = self.incoming()
                def response(*args, **kwargs):
                    if change == "account":
                        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="changed")
                    elif change == "inactive":
                        ChannelConnection.objects.filter(pk=self.connection.pk).update(is_active=False)
                    elif change == "client":
                        replacement = Client.objects.create(organization=self.org, name="Manually reassigned")
                        Conversation.objects.filter(pk=message.conversation_id).update(client=replacement)
                    else:
                        replacement = CommunicationChannel.objects.create(organization=self.org, kind="website", name="Rebound")
                        ChannelConnection.objects.filter(pk=self.connection.pk).update(channel=replacement)
                    return self.provider_chat()
                with patch("pool_service.communication_avito._json_request", side_effect=response):
                    try:
                        result = enrich_conversation(message.conversation_id)
                        self.assertEqual(result, "binding_changed")
                    except ValueError:
                        pass
                conversation = Conversation.objects.get(pk=message.conversation_id)
                self.assertFalse(conversation.provider_context)
                self.assertIsNone(conversation.context_synced_at)

    def test_unsafe_urls_are_not_rendered_and_titles_are_escaped(self):
        message, _ = self.incoming()
        raw = self.provider_chat()
        raw["context"]["value"].update(title="<script>bad()</script>", url="javascript:bad()", images={"main": {"140x105": "https://untrusted.invalid/pixel"}})
        with patch("pool_service.communication_avito._json_request", return_value=raw):
            enrich_conversation(message.conversation_id)
        conversation = Conversation.objects.get(pk=message.conversation_id)
        self.assertEqual(conversation.listing["url"], "")
        self.assertEqual(conversation.listing["image_url"], "")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("communications_conversations"))
        self.assertNotContains(page, "javascript:bad")
        self.assertContains(page, "&lt;script&gt;bad()&lt;/script&gt;")

    def test_failure_keeps_card_and_history_uses_only_incoming(self):
        first, _ = self.incoming(body="+79001234567")
        conversation = first.conversation
        ConversationMessage.objects.create(conversation=conversation, direction="out", body="Меня зовут Employee. +79007654321")
        with patch("pool_service.communication_avito._json_request", side_effect=AvitoError("provider_http_403")) as request:
            with self.assertRaises(AvitoError):
                enrich_conversation(conversation.pk)
            self.assertEqual(enrich_conversation(conversation.pk), "deferred")
            self.assertEqual(request.call_count, 1)
        conversation.refresh_from_db()
        self.assertFalse(conversation.client.phone)
        self.assertTrue(ContactObservation.objects.filter(origin="message", value="9001234567").exists())
        self.assertFalse(ContactObservation.objects.filter(origin="message", value="9007654321").exists())
        self.assertFalse(ClientContact.objects.filter(match_value="9007654321").exists())

    def test_legacy_dialog_resolves_existing_client_before_creating_new_card(self):
        customer = Client.objects.create(organization=self.org, name="Existing CRM client", phone="89001234567")
        conversation = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="chat-one", participant_name="90001", provider_phone="+79001234567")
        ConversationMessage.objects.create(conversation=conversation, direction="in", body="Телефон +79001234567")
        with patch("pool_service.communication_avito._json_request", return_value=self.provider_chat()):
            enrich_conversation(conversation.pk)
        conversation.refresh_from_db()
        self.assertEqual(conversation.client_id, customer.pk)
        self.assertEqual(Client.objects.count(), 1)
        customer.refresh_from_db()
        self.assertEqual(customer.name, "Existing CRM client")

    def test_legacy_chats_reuse_existing_card_only_after_stable_author_is_known(self):
        known, _ = self.incoming(chat="known", message="known")
        first = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="chat-one", participant_name="Unknown")
        second = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="chat-two", participant_name="Unknown")
        enrich_conversation(first.pk, refresh_context=False)
        enrich_conversation(second.pk, refresh_context=False)
        self.assertEqual(Client.objects.count(), 1)
        self.assertIsNone(Conversation.objects.get(pk=first.pk).client_id)
        self.assertIsNone(Conversation.objects.get(pk=second.pk).client_id)
        for conversation in (first, second):
            raw = self.provider_chat()
            raw["id"] = conversation.external_id
            with patch("pool_service.communication_avito._json_request", return_value=raw):
                enrich_conversation(conversation.pk)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(first.client_id, second.client_id)

    def test_batch_command_keeps_history_when_identity_is_loaded_after_local_prep(self):
        conversation = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="chat-one", participant_name="Unknown")
        ConversationMessage.objects.create(conversation=conversation, direction="in", body="Телефон +79001234567")
        with patch("pool_service.communication_avito._json_request", return_value=self.provider_chat()):
            call_command("enrich_communication_clients", ids=str(conversation.pk))
        conversation.refresh_from_db()
        self.assertIsNone(conversation.client_id)
        self.assertFalse(Client.objects.exists())
        self.assertTrue(ContactObservation.objects.filter(conversation=conversation, origin="message", value="9001234567").exists())
        self.assertTrue(conversation.contact_data["history_captured"])

    def test_replaying_old_chat_history_does_not_restore_old_shared_client_name(self):
        first, _ = self.incoming(body="Меня зовут Первое Имя.")
        second, _ = self.incoming(chat="second", message="second", body="Меня зовут Новое Имя.")
        with patch("pool_service.communication_avito._json_request", return_value=self.provider_chat()):
            enrich_conversation(first.conversation_id)
        self.assertEqual(Client.objects.get(pk=first.conversation.client_id).name, "Новое Имя")

    def test_credential_change_during_external_read_discards_result(self):
        first, _ = self.incoming()
        credential = AvitoCredential.objects.create(connection=self.connection, client_id_encrypted="before", client_secret_encrypted="before")
        def response(*args, **kwargs):
            AvitoCredential.objects.filter(pk=credential.pk).update(client_secret_encrypted="after")
            return self.provider_chat()
        with patch("pool_service.communication_avito._json_request", side_effect=response):
            self.assertEqual(enrich_conversation(first.conversation_id), "binding_changed")
        first.conversation.refresh_from_db()
        self.assertFalse(first.conversation.provider_context)

    @override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=True)
    @patch("pool_service.communication_enrichment.threading.Thread")
    @patch("pool_service.communication_enrichment.subprocess.Popen")
    def test_launcher_claims_each_conversation_without_dropping_other_chat(self, process, thread):
        # Avoid webhook callbacks here: explicit existing rows emulate queued work.
        first = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="first", participant_name="First")
        second = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="second", participant_name="Second")
        self.assertTrue(start_client_enrichment_worker([first.pk]))
        self.assertFalse(start_client_enrichment_worker([first.pk]))
        self.assertTrue(start_client_enrichment_worker([second.pk]))
        self.assertEqual(process.call_count, 2)
        self.assertIn("--claim-time", process.call_args.args[0])
