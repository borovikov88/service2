"""Synthetic provenance, authorization and real separate-connection race tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from pool_service.client_crm_models import ClientContact, ClientCRMProfile
from pool_service.client_merge import merge_clients
from pool_service.client_phone_matching import clients_by_phone
from pool_service.communication_clients import capture_client
from pool_service.communication_enrichment import enrich_conversation
from pool_service.communication_models import ChannelConnection, CommunicationAccess, CommunicationChannel, Conversation, ConversationMessage
from pool_service.communication_services import receive_message
from pool_service.contact_identity import IdentityConflict, preview_identity_review, resolve_identity_review, source_fields
from pool_service.contact_identity_models import ContactIdentityReview, ContactObservation
from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import Client, Employee, Organization, OrganizationAccess

PHONE = "+79001234567"


class Fixture:
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Identity synthetic", paid_until=timezone.now() + timedelta(days=2))
        self.other = Organization.objects.create(name="Other synthetic")
        self.user = get_user_model().objects.create_user("identity-synthetic")
        self.membership = OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic")
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.user, resolve_contact_identity=True)
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(channel=self.channel, name="Synthetic", external_id="test-account")
        notifications = patch("pool_service.communication_services.notify_users")
        notifications.start()
        self.addCleanup(notifications.stop)

    def incoming(self, **changes):
        values = dict(connection=self.connection, external_conversation_id="chat", participant_name="Synthetic customer",
            participant_phone=PHONE, participant_external_id="source-author", external_message_id="event", body="Hello")
        values.update(changes)
        return receive_message(**values)[0]

    def ambiguous(self):
        self.a = Client.objects.create(organization=self.org, name="Synthetic A", phone=PHONE)
        self.b = Client.objects.create(organization=self.org, name="Synthetic B", phone=PHONE)
        return self.incoming().conversation

    def resolve(self, conversation, **changes):
        values = dict(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk,
            client_id=self.a.pk, expected_revision=1, operation_key="choose-1")
        values.update(changes)
        return resolve_identity_review(**values)

    def snapshot(self):
        return (list(Conversation.objects.values()), list(ContactIdentityReview.objects.values()),
            list(ContactObservation.objects.values()), list(Client.objects.values()), list(ClientContact.objects.values()))

    def rejected(self, conversation, error=PermissionDenied, **changes):
        before = self.snapshot()
        with self.assertRaises(error):
            self.resolve(conversation, **changes)
        self.assertEqual(before, self.snapshot())


@override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class ContactIdentityTests(Fixture, TestCase):
    def test_ambiguous_provider_phone_retains_messages_one_review_no_client_or_foreign_candidate(self):
        foreign = Client.objects.create(organization=self.other, name="Private foreign", phone=PHONE)
        conversation = self.ambiguous()
        self.assertIsNone(conversation.client_id)
        self.incoming()
        self.incoming(external_message_id="second", body="A later message")
        self.assertEqual((ConversationMessage.objects.count(), Client.objects.count(), ContactIdentityReview.objects.count()), (2, 3, 1))
        review = ContactIdentityReview.objects.get()
        self.assertEqual(set(review.candidate_ids), {self.a.pk, self.b.pk})
        preview = preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)
        self.assertNotIn(foreign.pk, preview["candidate_ids"])
        self.assertNotIn("Private foreign", str(preview))
        self.assertNotIn(PHONE, str(preview))
        self.assertEqual((review.reason, review.revision), ("provider_phone", 1))

    def test_new_chat_with_pending_provider_author_does_not_create_provisional_duplicate(self):
        self.ambiguous()
        second = self.incoming(external_conversation_id="second", external_message_id="second", participant_phone="")
        self.assertIsNone(second.conversation.client_id)
        self.assertEqual(Client.objects.count(), 2)
        self.assertEqual(ContactIdentityReview.objects.count(), 2)  # one per source conversation

    def test_unknown_creates_one_preliminary_client_without_phone_and_reuses_author(self):
        first = self.incoming(participant_phone="", body="No contact number")
        second = self.incoming(external_conversation_id="other", external_message_id="other", participant_phone="")
        self.assertEqual(first.conversation.client_id, second.conversation.client_id)
        self.assertEqual(Client.objects.count(), 1)
        self.assertTrue(first.conversation.client.crm_profile.is_preliminary)
        self.assertFalse(first.conversation.client.phone)
        self.assertEqual(ContactObservation.objects.filter(origin="provider_identity").count(), 2)
        self.assertFalse(ContactIdentityReview.objects.exists())

    def test_phone_email_text_suggestions_do_not_match_even_after_enrichment_or_replay(self):
        existing = Client.objects.create(organization=self.org, name="Existing", phone=PHONE, email="site@example.invalid")
        first = self.incoming(participant_phone="", body="Телефон прораба +79001234567; Email: site@example.invalid")
        self.assertNotEqual(first.conversation.client_id, existing.pk)
        self.assertFalse(first.conversation.client.phone)
        self.assertFalse(first.conversation.client.email)
        self.assertFalse(ClientContact.objects.exists())
        enrich_conversation(first.conversation_id, refresh_context=False)
        self.incoming(participant_phone="", body="Телефон прораба +79001234567; Email: site@example.invalid")
        second = self.incoming(participant_phone="", external_conversation_id="different-author", external_message_id="next",
            participant_external_id="other-author", body="+79001234567 site@example.invalid")
        self.assertNotEqual(second.conversation.client_id, first.conversation.client_id)
        self.assertEqual(clients_by_phone(self.org, PHONE), [existing])
        self.assertEqual(Client.objects.count(), 3)
        self.assertFalse(ClientContact.objects.exists())
        suggestion = ContactObservation.objects.get(conversation=first.conversation, kind="phone", origin="message")
        self.assertEqual((suggestion.role, suggestion.confirmation, suggestion.source_message_id), ("foreman", "suggested", first.pk))
        self.assertIsNone(suggestion.confirmed_by_id)

    def test_role_is_local_to_a_single_unambiguous_phone_clause(self):
        message = self.incoming(body="Телефон прораба +79007654321; другой +79005554433", participant_phone="")
        observations = ContactObservation.objects.filter(origin="message", conversation=message.conversation)
        self.assertEqual(dict(observations.values_list("value", "role")), {"9007654321": "foreman", "9005554433": "unknown"})
        message = self.incoming(external_message_id="uncertain", body="Телефон прораба +79007654321 и +79005554433", participant_phone="")
        self.assertEqual(set(ContactObservation.objects.filter(source_message_id=message.pk).values_list("role", flat=True)), {"unknown"})

    def test_provider_phone_matches_legacy_or_manual_extra_contact_without_reclassifying(self):
        existing = Client.objects.create(organization=self.org, name="Manual corrected", phone=PHONE, email="manual@example.invalid")
        profile = ClientCRMProfile.objects.create(client=existing, source="manual")
        message = self.incoming(body="Телефон прораба +79007654321; Меня зовут Другое Имя.")
        self.assertEqual(message.conversation.client_id, existing.pk)
        existing.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual((existing.name, existing.phone, existing.email), ("Manual corrected", PHONE, "manual@example.invalid"))
        self.assertFalse(profile.is_preliminary)
        self.assertFalse(ClientContact.objects.filter(match_value="9007654321").exists())

    def test_new_provider_phone_enters_canonical_existing_contacts_with_no_human_confirmation(self):
        first = self.incoming()
        self.assertEqual(first.conversation.client.phone, "+7 900 123 4567")
        self.assertEqual(clients_by_phone(self.org, PHONE)[0].pk, first.conversation.client_id)
        provider = ContactObservation.objects.get(origin="provider_phone")
        self.assertEqual(provider.confirmation, "provider")
        self.assertIsNone(provider.confirmed_by_id)
        other = self.incoming(participant_external_id="different", external_conversation_id="other", external_message_id="other")
        self.assertEqual(other.conversation.client_id, first.conversation.client_id)

    def test_avito_identity_is_bound_to_connection_not_just_numeric_author(self):
        first = self.incoming(participant_phone="")
        account = ChannelConnection.objects.create(channel=self.channel, name="Different account", external_id="different-account")
        second = self.incoming(connection=account, participant_phone="")
        self.assertNotEqual(first.conversation.client_id, second.conversation.client_id)

    def test_multiple_provider_identity_bindings_require_clarification(self):
        a = Client.objects.create(organization=self.org, name="A")
        b = Client.objects.create(organization=self.org, name="B")
        for index, client in enumerate((a, b)):
            Conversation.objects.create(organization=self.org, connection=self.connection, external_id=f"legacy-{index}",
                participant_external_id="source-author", participant_name="Legacy", client=client)
        message = self.incoming(participant_phone="")
        self.assertIsNone(message.conversation.client_id)
        self.assertEqual(ContactIdentityReview.objects.get().reason, "provider_identity")
        self.assertEqual(Client.objects.count(), 2)

    def test_legacy_synthesized_participant_phone_is_not_provider_evidence_or_matching(self):
        existing = Client.objects.create(organization=self.org, name="Historical", phone=PHONE)
        legacy = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="legacy",
            participant_phone=PHONE, participant_name="Legacy")
        ConversationMessage.objects.create(conversation=legacy, external_id="historic", direction="in", body="+79001234567")
        enrich_conversation(legacy.pk, refresh_context=False)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.client_id)
        self.assertEqual(Client.objects.count(), 1)
        self.assertFalse(ContactObservation.objects.filter(origin="provider_phone").exists())
        self.assertTrue(ContactObservation.objects.filter(origin="message").exists())
        # Duplicate webhook does not reinterpret the persisted display field.
        self.incoming(external_conversation_id="legacy", external_message_id="historic", participant_external_id="", participant_phone="")
        legacy.refresh_from_db()
        self.assertNotEqual(legacy.client_id, existing.pk)
        self.assertFalse(ContactObservation.objects.filter(origin="provider_phone").exists())

    def test_explicit_legacy_binding_wins_over_new_ambiguous_phone_and_manual_fields(self):
        conversation = self.ambiguous()
        Conversation.objects.filter(pk=conversation.pk).update(client=self.b)
        self.incoming(external_message_id="later", body="+79007654321 third@example.invalid")
        conversation.refresh_from_db()
        self.assertEqual(conversation.client_id, self.b.pk)
        self.assertFalse(ClientContact.objects.filter(match_value="9007654321").exists())
        self.rejected(conversation, IdentityConflict)

    def test_resolution_replay_and_capture_cannot_override_explicit_choice(self):
        conversation = self.ambiguous()
        result = self.resolve(conversation)
        self.assertEqual(result, self.resolve(conversation))
        self.incoming()
        self.incoming(external_message_id="newer", body="Телефон прораба +79007654321")
        enrich_conversation(conversation.pk, refresh_context=False)
        conversation.refresh_from_db()
        self.assertEqual(conversation.client_id, self.a.pk)
        self.assertEqual(ContactObservation.objects.filter(origin="resolution", confirmed_by=self.user).count(), 1)
        self.assertFalse(ContactObservation.objects.filter(origin="message", confirmation="confirmed").exists())
        self.rejected(conversation, IdentityConflict, client_id=self.b.pk)
        self.rejected(conversation, IdentityConflict, operation_key="changed")
        self.rejected(conversation, IdentityConflict, expected_revision=2)

    def test_resolved_replay_rechecks_live_manual_and_foreign_binding_without_writes(self):
        conversation = self.ambiguous()
        self.resolve(conversation)
        Conversation.objects.filter(pk=conversation.pk).update(client=self.b)
        self.rejected(conversation, IdentityConflict)
        foreign = Client.objects.create(organization=self.other, name="Foreign private")
        Conversation.objects.filter(pk=conversation.pk).update(client=foreign)
        self.rejected(conversation)
        with self.assertRaises(PermissionDenied):
            preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)

    def test_same_connection_account_rebind_invalidates_old_review_without_relabelling_provenance(self):
        conversation = self.ambiguous()
        before = list(ContactObservation.objects.values())
        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="changed-account")
        self.rejected(conversation, ValidationError)
        with self.assertRaises(ValidationError):
            preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)
        self.assertEqual(before, list(ContactObservation.objects.values()))
        self.assertEqual(ContactIdentityReview.objects.get().account_id_snapshot, "test-account")

    def test_capture_rejects_rebound_account_instead_of_relabelling_existing_source(self):
        conversation = self.ambiguous()
        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="changed-account")
        self.connection.refresh_from_db()
        before = self.snapshot()
        with self.assertRaisesMessage(ValueError, "conversation_source_binding_changed"):
            self.incoming(external_message_id="from-rebound-account")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(ConversationMessage.objects.count(), 1)

    def test_manual_clearing_provider_primary_phone_is_not_reversed_by_capture(self):
        message = self.incoming()
        Client.objects.filter(pk=message.conversation.client_id).update(phone="", email="fixed@example.invalid")
        self.incoming(external_message_id="later", body="+79007654321 changed@example.invalid")
        customer = Client.objects.get(pk=message.conversation.client_id)
        self.assertEqual((customer.phone, customer.email), ("", "fixed@example.invalid"))

    def test_historical_enrichment_reuses_proven_provider_phone_without_creating_card(self):
        existing = Client.objects.create(organization=self.org, name="Existing manual", phone=PHONE)
        conversation = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="legacy",
            participant_name="Legacy", participant_phone="", provider_phone=PHONE)
        enrich_conversation(conversation.pk, refresh_context=False)
        conversation.refresh_from_db()
        self.assertEqual(conversation.client_id, existing.pk)
        self.assertEqual(Client.objects.count(), 1)
        self.assertTrue(ContactObservation.objects.filter(origin="provider_phone").exists())

    def test_no_number_or_author_still_captures_new_inbound_while_legacy_enrichment_creates_none(self):
        conversation = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="legacy",
            participant_name="Legacy")
        enrich_conversation(conversation.pk, refresh_context=False)
        self.assertFalse(Client.objects.exists())
        message = self.incoming(participant_phone="", participant_external_id="")
        self.assertTrue(message.conversation.client.crm_profile.is_preliminary)
        self.assertEqual(Client.objects.count(), 1)
        self.assertTrue(ConversationMessage.objects.filter(pk=message.pk).exists())

    def test_stale_revision_invalid_target_and_cross_org_are_atomic(self):
        conversation = self.ambiguous()
        self.rejected(conversation, IdentityConflict, expected_revision=2)
        foreign = Client.objects.create(organization=self.other, name="Foreign")
        self.rejected(conversation, client_id=foreign.pk)
        other = Client.objects.create(organization=self.org, name="Not a candidate")
        self.rejected(conversation, client_id=other.pk)
        self.rejected(conversation, organization_id=self.other.pk)

    def test_resolution_rollback_retains_client_contacts_source_and_review(self):
        conversation = self.ambiguous()
        before = self.snapshot()
        with patch("pool_service.contact_identity.observe", side_effect=RuntimeError("synthetic rollback")):
            with self.assertRaises(RuntimeError):
                self.resolve(conversation)
        self.assertEqual(before, self.snapshot())

    def test_current_rights_revocation_employment_and_source_scopes_apply_to_replay_and_preview(self):
        conversation = self.ambiguous()
        self.resolve(conversation)
        cases = [
            (get_user_model(), self.user.pk, {"is_active": False}, {"is_active": True}),
            (Employee, self.employee.pk, {"employment_status": Employee.STATUS_DISMISSED}, {"employment_status": Employee.STATUS_EMPLOYED}),
            (Employee, self.employee.pk, {"is_active": False}, {"is_active": True}),
            (ChannelConnection, self.connection.pk, {"is_active": False}, {"is_active": True}),
            (CommunicationChannel, self.channel.pk, {"is_active": False}, {"is_active": True}),
        ]
        for model, pk, changed, restored in cases:
            with self.subTest(model=model.__name__, changed=changed):
                model.objects.filter(pk=pk).update(**changed)
                self.rejected(conversation)
                with self.assertRaises(PermissionDenied):
                    preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)
                model.objects.filter(pk=pk).update(**restored)
        self.membership.delete()
        self.rejected(conversation)

    def test_new_capability_defaults_off_and_team_read_or_manager_role_cannot_resolve(self):
        conversation = self.ambiguous()
        self.grant.resolve_contact_identity = False
        self.grant.view_team_deals = True
        self.grant.save()
        self.rejected(conversation)
        self.grant.resolve_contact_identity = True
        self.grant.save()
        self.membership.role = "manager"
        self.membership.save()
        self.rejected(conversation)  # legacy client-card manage authority is owner/admin
        default = CrmAccess(organization=self.other, user=self.user)
        self.assertFalse(default.resolve_contact_identity)

    def test_communication_view_take_assignment_are_independent_and_fresh(self):
        conversation = self.ambiguous()
        for field in ("can_view_conversations", "can_take_conversation"):
            CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(**{field: False})
            self.rejected(conversation)
            with self.assertRaises(PermissionDenied):
                preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)
            CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(**{field: True})
        other = get_user_model().objects.create_user("other-assignee")
        Conversation.objects.filter(pk=conversation.pk).update(assignee=other)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_assign_conversation=False)
        self.rejected(conversation)

    def test_candidates_are_revalidated_after_merge_or_reparent_before_preview_or_write(self):
        conversation = self.ambiguous()
        ClientCRMProfile.objects.create(client=self.a, merged_into=self.b)
        self.rejected(conversation)
        preview = preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)
        self.assertEqual(preview["candidate_ids"], [self.b.pk])
        Client.objects.filter(pk=self.b.pk).update(organization=self.other)
        self.rejected(conversation, ValidationError)
        with self.assertRaises(ValidationError):
            preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=conversation.pk)

    def test_rebound_and_cross_org_graphs_refuse_without_exposing_candidates(self):
        conversation = self.ambiguous()
        Conversation.objects.filter(pk=conversation.pk).update(organization=self.other)
        self.rejected(conversation)
        Conversation.objects.filter(pk=conversation.pk).update(organization=self.org)
        CommunicationChannel.objects.filter(pk=self.channel.pk).update(organization=self.other)
        self.rejected(conversation)

    def test_observations_and_reviews_refuse_cross_scope_and_unsafe_bulk_writes(self):
        conversation = self.ambiguous()
        observation = ContactObservation.objects.first()
        observation.value = "changed"
        with self.assertRaises(ValidationError):
            observation.save()
        for model in (ContactObservation, ContactIdentityReview):
            with self.assertRaises(ValidationError):
                model.objects.all().update(organization=self.other)
            with self.assertRaises(ValidationError):
                model.objects.all().delete()
        review = ContactIdentityReview.objects.get()
        review.candidate_ids = [self.a.pk, Client.objects.create(organization=self.other, name="Foreign").pk]
        with self.assertRaises(ValidationError):
            review.save()

    def test_confirming_actor_is_protected_and_generic_merge_preserves_historical_decision(self):
        conversation = self.ambiguous()
        self.resolve(conversation)
        with self.assertRaises(ProtectedError):
            self.user.delete()
        target = Client.objects.create(organization=self.org, name="Imported target")
        ClientCRMProfile.objects.create(client=target, source="onec", onec_ref="00000000-0000-0000-0000-000000000005")
        merge_clients(self.a.pk, target.pk, organization_id=self.org.pk, actor=self.user)
        conversation.refresh_from_db()
        self.assertEqual(conversation.client_id, target.pk)
        self.assertEqual(ContactIdentityReview.objects.get().selected_client_id, self.a.pk)
        # Historical replay conflicts with the source's current merged binding.
        self.rejected(conversation, IdentityConflict)
        message = self.incoming(external_conversation_id="after-merge", external_message_id="after-merge", participant_phone="")
        self.assertEqual(message.conversation.client_id, target.pk)

    def test_candidate_list_is_bounded_and_truncation_is_explicit(self):
        for index in range(52):
            Client.objects.create(organization=self.org, name=f"Candidate {index}", phone=PHONE)
        conversation = self.incoming().conversation
        review = ContactIdentityReview.objects.get()
        self.assertIsNone(conversation.client_id)
        self.assertEqual(len(review.candidate_ids), 50)
        self.assertTrue(review.candidates_truncated)
        self.assertEqual(Client.objects.count(), 52)

    def test_propagated_review_preserves_truncation_from_its_bounded_source_snapshot(self):
        for index in range(52):
            Client.objects.create(organization=self.org, name=f"Synthetic {index}", phone=PHONE)
        first = self.incoming().conversation
        second = self.incoming(external_conversation_id="other", external_message_id="other", participant_phone="").conversation
        original = ContactIdentityReview.objects.get(conversation=first)
        propagated = ContactIdentityReview.objects.get(conversation=second)
        self.assertEqual(original.candidate_ids, propagated.candidate_ids)
        self.assertTrue(original.candidates_truncated)
        self.assertTrue(propagated.candidates_truncated)
        preview = preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=second.pk)
        self.assertTrue(preview["candidates_truncated"])
        self.assertEqual((Client.objects.count(), ConversationMessage.objects.count()), (52, 2))

    def test_foreign_candidate_drift_retains_new_inbound_and_only_current_safe_hints(self):
        original_conversation = self.ambiguous()
        original = ContactIdentityReview.objects.values().get(conversation=original_conversation)
        Client.objects.filter(pk=self.a.pk).update(organization=self.other)
        second = self.incoming(external_conversation_id="other", external_message_id="other", participant_phone="").conversation
        repeated = self.incoming(external_conversation_id="other", external_message_id="other", participant_phone="").conversation
        self.assertEqual(second.pk, repeated.pk)
        self.assertIsNone(second.client_id)
        self.assertEqual((Client.objects.count(), ConversationMessage.objects.count(), ContactIdentityReview.objects.count()), (2, 2, 2))
        review = ContactIdentityReview.objects.get(conversation=second)
        self.assertEqual((review.reason, review.candidate_ids), ("candidate_drift", [self.b.pk]))
        self.assertEqual(original, ContactIdentityReview.objects.values().get(conversation=original_conversation))
        preview = preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=second.pk)
        self.assertEqual(preview["candidate_ids"], [self.b.pk])
        self.rejected(second)
        self.resolve(second, client_id=self.b.pk)
        second.refresh_from_db()
        self.assertEqual(second.client_id, self.b.pk)

    def test_all_stale_candidates_keep_an_empty_durable_queue_without_foreign_hints_or_new_client(self):
        self.ambiguous()
        Client.objects.filter(pk__in=[self.a.pk, self.b.pk]).update(organization=self.other)
        second = self.incoming(external_conversation_id="other", external_message_id="other", participant_phone="").conversation
        review = ContactIdentityReview.objects.get(conversation=second)
        self.assertEqual((review.reason, review.candidate_ids), ("candidate_drift", []))
        preview = preview_identity_review(actor=self.user, organization_id=self.org.pk, conversation_id=second.pk)
        self.assertEqual(preview["candidate_ids"], [])
        self.rejected(second)
        self.assertIsNone(second.client_id)
        self.assertEqual((Client.objects.count(), ConversationMessage.objects.count()), (2, 2))
        third = self.incoming(external_conversation_id="third", external_message_id="third", participant_phone="").conversation
        self.assertEqual(ContactIdentityReview.objects.get(conversation=third).candidate_ids, [])
        self.assertIsNone(third.client_id)
        self.assertEqual(Client.objects.count(), 2)

    def test_merged_and_deleted_snapshot_candidates_do_not_rollback_new_source(self):
        self.ambiguous()
        ClientCRMProfile.objects.create(client=self.a, merged_into=self.b)
        self.b.delete()
        # SET_NULL on merged_into makes A active again under the legacy contract;
        # the deleted ID is still excluded and does not block saving the event.
        second = self.incoming(external_conversation_id="other", external_message_id="other", participant_phone="").conversation
        review = ContactIdentityReview.objects.get(conversation=second)
        self.assertEqual((review.reason, review.candidate_ids), ("candidate_drift", [self.a.pk]))
        self.assertEqual(ConversationMessage.objects.count(), 2)
        self.assertEqual(Client.objects.count(), 1)
        self.assertIsNone(second.client_id)

    def test_negation_competing_owner_or_intervening_prose_never_assert_foreman_role(self):
        for index, body in enumerate((
                "Телефон прораба не знаю, мой телефон +79007654321",
                "Не телефон прораба +79007654321",
                "Телефон прораба неизвестен +79007654321",
                "Телефон прораба, а это мой +79007654321")):
            with self.subTest(body=body):
                message = self.incoming(external_message_id=f"uncertain-{index}", participant_phone="", body=body)
                observation = ContactObservation.objects.get(source_message_id=message.pk, kind="phone")
                self.assertEqual(observation.role, "unknown")
                self.assertEqual(observation.confirmation, "suggested")
        self.assertFalse(ClientContact.objects.exists())
        self.assertFalse(Client.objects.get().phone)

    def test_archive_of_commercial_or_legacy_item_does_not_delete_message_or_change_identity(self):
        from pool_service.models import CrmItem
        message = self.incoming()
        item = CrmItem.objects.create(organization=self.org, client=message.conversation.client, title="Archived", direction="project", stage="prj_idea", is_archived=True)
        self.incoming(external_message_id="after-archive")
        self.assertEqual(ConversationMessage.objects.count(), 2)
        self.assertEqual(Conversation.objects.get().client_id, item.client_id)


@override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class ContactIdentityConcurrencyTests(Fixture, TransactionTestCase):
    def run_race(self, work):
        barrier = Barrier(2)
        def execute(index):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT CONNECTION_ID()")
                    connection_id = cursor.fetchone()[0]
                barrier.wait(timeout=15)
                return connection_id, work(index)
            finally:
                connections["default"].close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(execute, (0, 1)))
        self.assertNotEqual(results[0][0], results[1][0])
        return [item[1] for item in results]

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_capture_one_durable_review_and_one_source_event(self):
        self.a = Client.objects.create(organization=self.org, name="A", phone=PHONE)
        self.b = Client.objects.create(organization=self.org, name="B", phone=PHONE)
        self.run_race(lambda index: self.incoming().pk)
        self.assertEqual((ContactIdentityReview.objects.count(), ConversationMessage.objects.count(), Client.objects.count()), (1, 1, 2))
        self.assertIsNone(Conversation.objects.get().client_id)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_unknown_capture_reuses_one_preliminary_client_across_chats(self):
        self.run_race(lambda index: self.incoming(external_conversation_id=f"chat-{index}", external_message_id=f"event-{index}", participant_phone="").pk)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(ClientCRMProfile.objects.filter(is_preliminary=True).count(), 1)
        self.assertEqual(ConversationMessage.objects.count(), 2)
        self.assertEqual(len(set(Conversation.objects.values_list("client_id", flat=True))), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_resolutions_have_one_winner_and_one_explicit_conflict(self):
        conversation = self.ambiguous()
        def work(index):
            try:
                self.resolve(conversation, client_id=(self.a.pk, self.b.pk)[index], operation_key=f"decision-{index}")
                return "resolved"
            except IdentityConflict:
                return "conflict"
        self.assertCountEqual(self.run_race(work), ["resolved", "conflict"])
        self.assertEqual(ContactObservation.objects.filter(origin="resolution").count(), 1)
        self.assertEqual(ContactIdentityReview.objects.get().revision, 2)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_resolution_and_source_replay_preserve_explicit_decision(self):
        conversation = self.ambiguous()
        self.run_race(lambda index: self.resolve(conversation) if index == 0 else self.incoming().pk)
        conversation.refresh_from_db()
        self.assertEqual(conversation.client_id, self.a.pk)
        self.assertEqual(ContactIdentityReview.objects.count(), 1)
        self.assertEqual(ConversationMessage.objects.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_uncommitted_communication_revocation_blocks_resolution_and_replay_then_denies(self):
        conversation = self.ambiguous()
        for replay in (False, True):
            if replay:
                self.resolve(conversation)
            before = self.snapshot()
            held, release, started, done = Event(), Event(), Event(), Event()
            connection_ids = []
            def separate_connection(work):
                close_old_connections()
                try:
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SELECT CONNECTION_ID()")
                        connection_ids.append(cursor.fetchone()[0])
                    return work()
                finally:
                    connections["default"].close()
            def revoke():
                with transaction.atomic():
                    # This legacy write does not acquire the organization mutex:
                    # it exercises the resolver's actual fresh authorization lock.
                    CommunicationAccess.objects.filter(organization=self.org, user=self.user).update(can_take_conversation=False)
                    held.set()
                    if not release.wait(10):
                        raise RuntimeError("Synthetic revoke release timed out")
            def resolve():
                started.set()
                try:
                    self.resolve(conversation)
                except PermissionDenied:
                    return "denied"
                finally:
                    done.set()
                return "resolved"
            with ThreadPoolExecutor(max_workers=2) as executor:
                mutation = executor.submit(separate_connection, revoke)
                self.assertTrue(held.wait(10))
                decision = executor.submit(separate_connection, resolve)
                try:
                    self.assertTrue(started.wait(10))
                    self.assertFalse(done.wait(.2), "Resolver bypassed the uncommitted communication revocation")
                finally:
                    release.set()
                mutation.result(timeout=20)
                self.assertEqual(decision.result(timeout=20), "denied")
            self.assertEqual(len(set(connection_ids)), 2)
            self.assertEqual(before, self.snapshot())
            CommunicationAccess.objects.filter(organization=self.org, user=self.user).update(can_take_conversation=True)
