"""Additive provenance schema does not infer trust from historical display fields."""
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ContactIdentityMigrationTests(TransactionTestCase):
    before = [("pool_service", "0152_crmaccess_edit_own_deals_crmaccess_edit_own_leads_and_more")]
    after = [("pool_service", "0153_clientcrmprofile_is_preliminary_and_more")]

    def test_expand_repeat_reverse_forward_preserves_legacy_without_grants_or_backfill(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(self.before)
            apps = executor.loader.project_state(self.before).apps
            org = apps.get_model("pool_service", "Organization").objects.create(name="Migration synthetic")
            user = apps.get_model("auth", "User").objects.create(username="migration-contact-synthetic")
            access = apps.get_model("pool_service", "CrmAccess").objects.create(organization_id=org.pk, user_id=user.pk, view_team_deals=True)
            client = apps.get_model("pool_service", "Client").objects.create(organization_id=org.pk, name="Historical", phone="+79001234567")
            profile = apps.get_model("pool_service", "ClientCRMProfile").objects.create(client_id=client.pk, source="communications", communication_data={"applied_name": "Historical"})
            channel = apps.get_model("pool_service", "CommunicationChannel").objects.create(organization_id=org.pk, kind="website", name="Historical")
            connection_row = apps.get_model("pool_service", "ChannelConnection").objects.create(channel_id=channel.pk, name="Historical")
            conversation = apps.get_model("pool_service", "Conversation").objects.create(organization_id=org.pk, connection_id=connection_row.pk,
                client_id=client.pk, external_id="source", participant_name="Historical", participant_phone="+79001234567")
            message = apps.get_model("pool_service", "ConversationMessage").objects.create(conversation_id=conversation.pk, direction="in", body="Synthetic historical event")
            before = apps.get_model("pool_service", "Client").objects.values().get(pk=client.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(self.after)
            after = executor.loader.project_state(self.after).apps
            self.assertEqual(before, after.get_model("pool_service", "Client").objects.values().get(pk=client.pk))
            self.assertFalse(after.get_model("pool_service", "CrmAccess").objects.get(pk=access.pk).resolve_contact_identity)
            self.assertFalse(after.get_model("pool_service", "ClientCRMProfile").objects.get(pk=profile.pk).is_preliminary)
            self.assertEqual(after.get_model("pool_service", "Conversation").objects.get(pk=conversation.pk).provider_phone, "")
            self.assertEqual(after.get_model("pool_service", "ConversationMessage").objects.get(pk=message.pk).body, "Synthetic historical event")
            Observation = after.get_model("pool_service", "ContactObservation")
            Review = after.get_model("pool_service", "ContactIdentityReview")
            self.assertFalse(Observation.objects.exists())
            self.assertFalse(Review.objects.exists())
            source = dict(organization_id=org.pk, conversation_id=conversation.pk, connection_id_snapshot=connection_row.pk,
                account_id_snapshot="", source_kind="website", conversation_external_id="source")
            event = dict(**source, event_key="a" * 64, origin="provider_phone", kind="phone", value="9001234567", confirmation="provider")
            Observation.objects.create(**event)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Observation.objects.create(**event)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Observation.objects.create(**{**event, "event_key": "b" * 64, "confirmation": "confirmed"})
            Review.objects.create(**source, candidate_ids=[client.pk, client.pk + 1], reason="provider_phone")
            with self.assertRaises(IntegrityError), transaction.atomic():
                Review.objects.create(**source, candidate_ids=[client.pk, client.pk + 1], reason="provider_phone")
            MigrationExecutor(connection).migrate(self.after)
            self.assertEqual(Observation.objects.count(), 1)
            # Reverse loses only synthetic new schema/data; historical source remains.
            MigrationExecutor(connection).migrate(self.before)
            self.assertEqual(before, apps.get_model("pool_service", "Client").objects.values().get(pk=client.pk))
            MigrationExecutor(connection).migrate(self.after)
            self.assertEqual(Observation.objects.count(), 0)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
