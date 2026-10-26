"""Physical additive inbox constraints, repeat/reverse/forward, no source backfill."""
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class InboxMigrationTests(TransactionTestCase):
    before = [("pool_service", "0153_clientcrmprofile_is_preliminary_and_more")]
    after = [("pool_service", "0154_crm_inbox")]

    def test_expand_constraints_no_backfill_or_flag_and_disposable_reverse_forward(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(self.before)
            apps = executor.loader.project_state(self.before).apps
            Org = apps.get_model("pool_service", "Organization")
            org = Org.objects.create(name="Inbox migration synthetic")
            channel = apps.get_model("pool_service", "CommunicationChannel").objects.create(organization_id=org.pk, kind="website", name="Synthetic")
            conn = apps.get_model("pool_service", "ChannelConnection").objects.create(channel_id=channel.pk, external_id="synthetic", name="Synthetic")
            conv = apps.get_model("pool_service", "Conversation").objects.create(organization_id=org.pk, connection_id=conn.pk, external_id="synthetic", participant_name="Synthetic")
            message = apps.get_model("pool_service", "ConversationMessage").objects.create(conversation_id=conv.pk, direction="in", body="Synthetic historical")
            original = dict(apps.get_model("pool_service", "ConversationMessage").objects.values().get(pk=message.pk))
            executor = MigrationExecutor(connection)
            executor.migrate(self.after)
            apps_after = executor.loader.project_state(self.after).apps
            Event = apps_after.get_model("pool_service", "CrmInboxEvent")
            Boundary = apps_after.get_model("pool_service", "CrmInboxBoundary")
            Message = apps_after.get_model("pool_service", "ConversationMessage")
            self.assertFalse(Event.objects.exists())
            self.assertEqual(Boundary.objects.count(), 1)
            boundary_time = Boundary.objects.get().captured_at
            self.assertFalse(apps_after.get_model("pool_service", "Organization").objects.get(pk=org.pk).crm_intake_process)
            legacy = Message.objects.get(pk=message.pk)
            self.assertIsNone(legacy.intake_scope)
            self.assertIsNone(legacy.intake_received_at)
            self.assertIsNone(legacy.intake_occurred_at)
            self.assertTrue(legacy.intake_historical)
            event = dict(organization_id=org.pk, source_type="message", source_id=message.pk, source_key="a" * 64,
                provider_kind="website", connection_id_snapshot=conn.pk, channel_id_snapshot=conn.pk,
                account_id_snapshot="", namespace="synthetic", external_id="", scope_verified=False,
                revision=1, fingerprint="b" * 64, occurred_at=None, received_at=timezone.now(), origin="legacy",
                historical=True, state="blocked")
            Event.objects.create(**event)
            for change in ({}, {"revision": 0}, {"revision": 2, "state": "claimed"},
                           {"revision": 2, "state": "done"}, {"revision": 2, "source_type": "other"},
                           {"revision": 2, "state": "unknown"}):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Event.objects.create(**{**event, **change})
            # The uniqueness and shape constraints are physically present on MySQL.
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(cursor, Event._meta.db_table)
            for name in ("crm_inbox_key_revision", "crm_inbox_locator_revision", "crm_inbox_claim_shape", "crm_inbox_done_shape", "crm_inbox_state"):
                self.assertIn(name, constraints)
            MigrationExecutor(connection).migrate(self.after)
            self.assertEqual(Boundary.objects.get().captured_at, boundary_time)
            self.assertEqual(Event.objects.count(), 1)
            # Destructive reversal is exercised only with synthetic accepted rows.
            MigrationExecutor(connection).migrate(self.before)
            self.assertEqual(apps.get_model("pool_service", "ConversationMessage").objects.values().get(pk=message.pk), original)
            MigrationExecutor(connection).migrate(self.after)
            self.assertFalse(Event.objects.exists())
            self.assertEqual(Boundary.objects.count(), 1)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
