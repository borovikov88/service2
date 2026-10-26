"""Synthetic durable capture, SQL delivery, historical safety and MySQL races."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier, Event
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_avito import ingest_webhook, sync_recent_messages
from pool_service.communication_models import ChannelConnection, CommunicationChannel, Conversation, ConversationMessage, PhoneCall, TelephonyConnection
from pool_service.communication_services import receive_message
from pool_service.contact_identity_models import ContactIdentityReview, ContactObservation
from pool_service.crm_inbox_models import CrmInboxBoundary, CrmInboxCursor, CrmInboxEvent
from pool_service.models import Client, Organization
from pool_service.services.crm_inbox import (MAX_ATTEMPTS, capture_source, claim_one, deliver_claim,
    inbox_status, reconcile, retry_failed, run_worker)


class Fixture:
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        CrmInboxBoundary.objects.update_or_create(key="sources", defaults={"captured_at": self.now - timedelta(hours=1)})
        self.org = Organization.objects.create(name="Inbox synthetic", crm_intake_process=False)
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(channel=self.channel, external_id="synthetic-account", name="Synthetic")
        self.notify = patch("pool_service.communication_services.notify_users").start()
        self.addCleanup(patch.stopall)

    def incoming(self, **changes):
        values = dict(connection=self.connection, external_conversation_id="chat", participant_name="Synthetic",
            participant_external_id="synthetic-author", external_message_id="message", body="Synthetic private body")
        values.update(changes)
        return receive_message(**values)[0]

    def scope(self, source_type="message"):
        return dict(organization_id=self.org.pk, source_type=source_type,
            connection_id=self.connection.pk if source_type == "message" else self.telephony.pk)

    def enable(self):
        Organization.objects.filter(pk=self.org.pk).update(crm_intake_process=True)

    def megafon(self):
        self.megafon_channel = CommunicationChannel.objects.create(organization=self.org, kind="megafon", name="Synthetic phone")
        self.megafon_connection = ChannelConnection.objects.create(channel=self.megafon_channel, external_id="synthetic-line", name="Synthetic")
        self.megafon_connection.set_api_token("synthetic-token")
        self.megafon_connection.save()
        self.telephony = TelephonyConnection.objects.create(organization=self.org, external_id="synthetic-line")
        self.call_data = dict(crm_token="synthetic-token", cmd="history", callid="synthetic-call", phone="+79001234567",
            type="in", status="success", duration="20", start=self.now.strftime("%Y%m%dT%H%M%SZ"))

    def history(self, **changes):
        values = {**self.call_data, **changes}
        return self.client.post(reverse("megafon_webhook", args=[self.megafon_connection.public_id]), values)

    def avito_payload(self, created=None):
        value = dict(user_id=self.connection.external_id, id="message", chat_id="chat", author_id="synthetic-author",
            type="text", content={"text": "Synthetic private body"})
        if created is not None:
            value["created"] = created
        return {"payload": {"type": "message", "value": value}}


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class InboxCaptureTests(Fixture, TestCase):
    def test_off_ai_and_duplicate_repair_are_atomic_without_repeat_client_or_notification(self):
        message = self.incoming()
        event = CrmInboxEvent.objects.get()
        self.assertFalse(self.org.crm_intake_process)
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.source_id, message.pk)
        self.assertEqual(event.account_id_snapshot, self.connection.external_id)
        self.assertIsNone(event.occurred_at)
        self.assertFalse(event.historical)
        client_count = Client.objects.count()
        observations = list(ContactObservation.objects.values())
        self.assertEqual(self.notify.call_count, 1)
        CrmInboxEvent.objects.all()._raw_delete("default")  # Trusted corruption fixture, not supported ORM.
        self.incoming()
        self.assertEqual((CrmInboxEvent.objects.count(), Client.objects.count(), self.notify.call_count), (1, client_count, 1))
        self.assertEqual(observations, list(ContactObservation.objects.values()))
        self.assertIsNone(claim_one(**self.scope()))

    def test_capture_failure_rolls_back_source_client_and_ack_path(self):
        with patch("pool_service.services.crm_inbox.capture_source", side_effect=RuntimeError("synthetic capture failure")):
            with self.assertRaises(RuntimeError):
                self.incoming()
        self.assertFalse(ConversationMessage.objects.exists())
        self.assertFalse(Conversation.objects.exists())
        self.assertFalse(Client.objects.exists())
        self.assertFalse(CrmInboxEvent.objects.exists())
        self.notify.assert_not_called()

    def test_outer_rollback_and_no_wakeup_loss(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.incoming()
            raise RuntimeError("synthetic rollback")
        self.assertFalse(CrmInboxEvent.objects.exists())
        with patch("django.db.transaction.on_commit"):
            self.incoming()
        self.assertEqual(CrmInboxEvent.objects.count(), 1)

    def test_legacy_duplicate_never_creates_client_or_current_effects(self):
        conv = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="chat", participant_name="Synthetic", status="done")
        message = ConversationMessage.objects.create(conversation=conv, direction="in", external_id="message", body="Legacy")
        self.incoming(participant_external_id="")
        event = CrmInboxEvent.objects.get()
        self.assertEqual(event.state, "blocked")
        self.assertEqual(event.error_code, "legacy_scope_unknown")
        self.assertTrue(event.historical)
        self.assertFalse(event.scope_verified)
        self.assertEqual(event.account_id_snapshot, "")
        self.assertEqual(event.source_id, message.pk)
        self.assertEqual(Client.objects.count(), 0)
        conv.refresh_from_db()
        self.assertEqual(conv.status, "done")
        self.assertIsNone(conv.last_message_at)
        self.notify.assert_not_called()

    def test_deleted_source_recreation_cannot_duplicate_logical_provider_identity(self):
        from django.core.exceptions import ValidationError
        message = self.incoming()
        client_count = Client.objects.count()
        ConversationMessage.objects.filter(pk=message.pk).delete()
        with self.assertRaises(ValidationError):
            self.incoming()
        self.assertEqual(CrmInboxEvent.objects.count(), 1)
        self.assertFalse(ConversationMessage.objects.exists())
        self.assertEqual(Client.objects.count(), client_count)
        self.assertEqual(self.notify.call_count, 1)

    def test_historical_time_unknown_sync_and_live_webhook_classification(self):
        current = self.incoming(external_message_id="current", occurred_at=self.now)
        conv = current.conversation
        Conversation.objects.filter(pk=conv.pk).update(status="done")
        old = self.incoming(external_message_id="old", occurred_at=self.now - timedelta(days=3))
        unknown = self.incoming(external_message_id="unknown", intake_origin="recovery")
        explicit = self.incoming(external_message_id="explicit", intake_origin="history", occurred_at=self.now)
        self.assertEqual(list(CrmInboxEvent.objects.order_by("pk").values_list("historical", flat=True)), [False, True, True, True])
        self.assertEqual(CrmInboxEvent.objects.get(source_id=old.pk).occurred_at, self.now - timedelta(days=3))
        self.assertIsNone(CrmInboxEvent.objects.get(source_id=unknown.pk).occurred_at)
        conv.refresh_from_db()
        self.assertEqual(conv.status, "done")
        self.assertEqual(conv.last_message_at, self.now)
        self.assertEqual(self.notify.call_count, 1)
        self.incoming(external_message_id="old", occurred_at=self.now)
        old.refresh_from_db()
        self.assertTrue(old.intake_historical)
        self.assertEqual(old.intake_occurred_at, self.now - timedelta(days=3))

    def test_avito_real_webhook_and_sync_keep_provider_time_and_historical_no_effects(self):
        epoch = int((self.now - timedelta(days=2)).timestamp())
        old, _ = ingest_webhook(self.connection, self.avito_payload(epoch))
        self.assertTrue(old.intake_historical)
        self.assertEqual(int(old.intake_occurred_at.timestamp()), epoch)
        self.notify.assert_not_called()
        unknown = dict(id="unknown-sync", author_id="synthetic-author", direction="in", type="text", content={"text": "Synthetic"})
        with patch("pool_service.communication_avito.authorized_account_id", return_value=self.connection.external_id), patch("pool_service.communication_avito.access_token", return_value="synthetic"), patch("pool_service.communication_avito._json_request", return_value={"chats": [{"id": "chat"}]}), patch("pool_service.communication_avito._json_list_request", return_value=[unknown]):
            sync_recent_messages(self.connection)
        event = CrmInboxEvent.objects.get(external_id="unknown-sync")
        self.assertTrue(event.historical)
        self.assertIsNone(event.occurred_at)
        self.assertEqual(event.origin, "recovery")
        self.notify.assert_not_called()

    def test_same_external_ids_are_separate_across_account_conversation_and_org(self):
        first = self.incoming()
        other_connection = ChannelConnection.objects.create(channel=self.channel, name="Other", external_id="other-account")
        self.incoming(connection=other_connection)
        self.incoming(external_conversation_id="other-chat")
        other_org = Organization.objects.create(name="Other synthetic")
        channel = CommunicationChannel.objects.create(organization=other_org, kind="avito", name="Other")
        connection = ChannelConnection.objects.create(channel=channel, name="Other", external_id=self.connection.external_id)
        self.incoming(connection=connection)
        self.assertEqual(len(set(CrmInboxEvent.objects.values_list("source_key", flat=True))), 4)
        self.incoming()
        self.assertEqual(CrmInboxEvent.objects.count(), 4)

    def test_manual_pending_resolved_and_rebound_scope_fail_closed(self):
        a = Client.objects.create(organization=self.org, name="Synthetic A", phone="+79001234567")
        b = Client.objects.create(organization=self.org, name="Synthetic B", phone="+79001234567")
        message = self.incoming(participant_phone="+79001234567")
        review_before = list(ContactIdentityReview.objects.values())
        self.incoming(participant_phone="+79001234567")
        self.assertEqual(review_before, list(ContactIdentityReview.objects.values()))
        self.assertEqual(Client.objects.count(), 2)
        Conversation.objects.filter(pk=message.conversation_id).update(client=a)
        self.incoming(participant_phone="+79001234567")
        self.assertEqual(Conversation.objects.get().client_id, a.pk)
        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="rebound")
        self.connection.refresh_from_db()
        with self.assertRaises(ValueError):
            self.incoming()
        self.enable()
        claim = claim_one(**self.scope())
        self.assertEqual(deliver_claim(*claim), "blocked")
        self.assertEqual(CrmInboxEvent.objects.get().error_code, "source_scope_changed")
        self.assertEqual(Client.objects.count(), 2)

    def test_call_revisions_missing_then_known_time_recording_and_same_pk_reconciliation(self):
        self.megafon()
        self.assertEqual(self.history(start="  ", status="missed").status_code, 200)
        call = PhoneCall.objects.get()
        initial_receipt = call.intake_received_at
        self.assertIsNone(CrmInboxEvent.objects.get().occurred_at)
        self.assertEqual(self.history(start="", status="missed").status_code, 200)
        self.assertEqual(CrmInboxEvent.objects.count(), 1)
        self.assertEqual(self.history().status_code, 200)
        call.refresh_from_db()
        expected_time = self.now.replace(microsecond=0)
        self.assertEqual(call.intake_occurred_at, expected_time)
        self.assertEqual(call.intake_received_at, initial_receipt)
        self.assertEqual(list(CrmInboxEvent.objects.order_by("revision").values_list("revision", flat=True)), [1, 2])
        self.assertEqual(CrmInboxEvent.objects.get(revision=2).occurred_at, expected_time)
        CrmInboxEvent.objects.filter(revision=2)._raw_delete("default")  # Trusted lost-inbox fixture.
        reconcile(**self.scope("call"))
        self.assertEqual(CrmInboxEvent.objects.get(revision=2).occurred_at, expected_time)
        self.assertEqual(CrmInboxEvent.objects.count(), 2)
        self.history(link="https://synthetic.megapbx.ru/synthetic-recording")
        self.assertEqual(CrmInboxEvent.objects.count(), 2)
        # A mutable source changed at the same PK is revisited on the next cycle.
        reconcile(**self.scope("call"), limit=100)
        PhoneCall.objects.filter(pk=call.pk).update(duration_seconds=41)
        result = reconcile(**self.scope("call"), limit=100)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(CrmInboxEvent.objects.count(), 3)
        reconcile(**self.scope("call"), limit=100)
        self.assertEqual(CrmInboxEvent.objects.count(), 3)
        call.refresh_from_db()
        self.assertEqual(call.intake_received_at, initial_receipt)

    def test_call_history_command_is_live_but_old_provider_time_is_historical(self):
        self.megafon()
        self.history()
        self.assertFalse(CrmInboxEvent.objects.get().historical)
        self.history(callid="old", start=(self.now - timedelta(days=3)).strftime("%Y%m%dT%H%M%SZ"))
        self.assertTrue(CrmInboxEvent.objects.get(external_id="old").historical)
        self.history(callid="old")
        self.assertTrue(CrmInboxEvent.objects.filter(external_id="old").latest("revision").historical)


    def test_new_historical_dialogue_retains_evidence_without_creating_client(self):
        old = self.incoming(occurred_at=self.now - timedelta(days=3), body="Synthetic phone +79001234567")
        self.assertEqual(Client.objects.count(), 0)
        self.assertIsNone(Conversation.objects.get().client_id)
        self.assertTrue(ContactObservation.objects.filter(source_message_id=old.pk, confirmation="suggested").exists())
        self.assertIsNone(Conversation.objects.get().last_message_at)
        self.notify.assert_not_called()

    def test_current_provider_timestamps_do_not_move_dialogue_activity_backwards(self):
        latest = self.now + timedelta(seconds=10)
        self.incoming(occurred_at=latest)
        self.incoming(external_message_id="earlier", occurred_at=self.now)
        self.assertEqual(Conversation.objects.get().last_message_at, latest)
        self.assertEqual(self.notify.call_count, 2)

    def test_late_genuine_old_call_time_stays_historical_after_newer_replay_and_reconcile(self):
        self.megafon()
        self.history(start="")
        first = CrmInboxEvent.objects.get()
        self.assertFalse(first.historical)
        call = PhoneCall.objects.get()
        receipt, cutover = call.intake_received_at, call.intake_cutover_at
        self.history(start=(self.now - timedelta(days=3)).strftime("%Y%m%dT%H%M%SZ"))
        self.assertTrue(CrmInboxEvent.objects.latest("revision").historical)
        self.history()
        call.refresh_from_db()
        self.assertFalse(call.intake_historical)  # Initial classification is immutable.
        self.assertTrue(call.intake_revision_historical)
        self.assertEqual((call.intake_received_at, call.intake_cutover_at), (receipt, cutover))
        self.assertTrue(CrmInboxEvent.objects.latest("revision").historical)
        count = CrmInboxEvent.objects.count()
        reconcile(**self.scope("call"))
        self.assertEqual(CrmInboxEvent.objects.count(), count)
        self.assertTrue(CrmInboxEvent.objects.latest("revision").historical)

    def test_call_capture_failure_rolls_back_and_event_outgoing_internal_upload_excluded(self):
        self.megafon()
        with patch("pool_service.services.crm_inbox.capture_source", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.history()
        self.assertFalse(PhoneCall.objects.exists())
        self.history(type="out")
        self.assertFalse(CrmInboxEvent.objects.exists())
        for direction, source_kind in [("internal", "telephony"), ("in", "uploaded")]:
            call = PhoneCall.objects.create(organization=self.org, connection=self.telephony if source_kind == "telephony" else None,
                external_id=direction, source_kind=source_kind, phone_number="synthetic", direction=direction,
                started_at=self.now, result="answered")
            with transaction.atomic():
                self.assertIsNone(capture_source("call", call))
        response = self.history(cmd="event", type="INCOMING", callid="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CrmInboxEvent.objects.exists())


class InboxWorkerTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.message = self.incoming()
        self.enable()

    def test_missing_adapter_retains_delivery_and_does_not_exhaust_attempts(self):
        self.assertEqual(run_worker(**self.scope(), limit=10), {"awaiting_consumer": 1})
        event = CrmInboxEvent.objects.get()
        self.assertEqual((event.state, event.attempts, event.processed_at), ("awaiting_consumer", 0, None))
        self.assertIsNotNone(event.prepared_at)
        # Installation of a future consumer uses the same row and stable key.
        claim = claim_one(**self.scope(), now=event.next_attempt_at)
        effects = []
        self.assertEqual(deliver_claim(*claim, consumer=lambda event, source, key: effects.append(key), now=event.next_attempt_at), "done")
        self.assertEqual(effects, [f"{event.source_key}:1"])
        self.assertIsNone(claim_one(**self.scope()))
        self.assertEqual(deliver_claim(*claim, consumer=lambda *args: self.fail("duplicate effect")), "stale_claim")

    def test_dead_lease_recovery_and_stale_ack_are_token_safe(self):
        now = timezone.now()
        first = claim_one(**self.scope(), now=now)
        second = claim_one(**self.scope(), now=now + timedelta(seconds=61))
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(deliver_claim(*first, now=now + timedelta(seconds=61)), "stale_claim")
        self.assertEqual(deliver_claim(*second, consumer=lambda *args: None, now=now + timedelta(seconds=62)), "done")
        self.assertEqual(CrmInboxEvent.objects.get().attempts, 1)

    def test_lease_expiry_during_sql_adapter_rolls_back_effect_before_ack(self):
        client = self.message.conversation.client
        old_name = client.name
        base = timezone.now()
        claim = claim_one(**self.scope(), now=base)
        def effect(event, source, key):
            Client.objects.filter(pk=client.pk).update(name="Synthetic effect")
        with patch("pool_service.services.crm_inbox.timezone.now", return_value=base + timedelta(seconds=61)):
            result = deliver_claim(*claim, consumer=effect, now=base)
        self.assertEqual(result, "lease_expired")
        client.refresh_from_db()
        self.assertEqual(client.name, old_name)
        event = CrmInboxEvent.objects.get()
        self.assertEqual((event.state, event.attempts, event.processed_at), ("pending", 0, None))

    def test_off_after_claim_prevents_adapter_and_can_resume_later(self):
        claim = claim_one(**self.scope())
        Organization.objects.filter(pk=self.org.pk).update(crm_intake_process=False)
        called = []
        self.assertEqual(deliver_claim(*claim, consumer=lambda *args: called.append(True)), "disabled")
        self.assertFalse(called)
        self.assertEqual(CrmInboxEvent.objects.get().state, "pending")
        self.enable()
        self.assertEqual(run_worker(**self.scope(), consumer=lambda *args: None), {"done": 1})

    def test_adapter_sql_effect_and_ack_commit_together_failure_rolls_back(self):
        client = self.message.conversation.client
        initial_name = client.name
        def crash(event, source, key):
            Client.objects.filter(pk=client.pk).update(name="Synthetic effect")
            raise RuntimeError("Synthetic private body +79001234567 secret")
        claim = claim_one(**self.scope())
        self.assertEqual(deliver_claim(*claim, consumer=crash), "retry")
        client.refresh_from_db()
        self.assertEqual(client.name, initial_name)
        event = CrmInboxEvent.objects.get()
        self.assertEqual((event.attempts, event.error_code, event.processed_at), (1, "consumer_failed", None))
        self.assertNotIn("private", json.dumps(inbox_status(**self.scope()), default=str))
        def effect(event, source, key):
            Client.objects.filter(pk=client.pk).update(name="Synthetic effect")
        claim = claim_one(**self.scope(), now=event.next_attempt_at)
        self.assertEqual(deliver_claim(*claim, consumer=effect, now=event.next_attempt_at), "done")
        client.refresh_from_db()
        self.assertEqual(client.name, "Synthetic effect")
        self.assertIsNotNone(CrmInboxEvent.objects.get().processed_at)

    def test_ack_save_crash_rolls_back_adapter_effect(self):
        client = self.message.conversation.client
        initial_name = client.name
        original_save = CrmInboxEvent.save
        def save(event, *args, **kwargs):
            if event.state == "done":
                raise RuntimeError("synthetic before ack")
            return original_save(event, *args, **kwargs)
        claim = claim_one(**self.scope())
        with patch.object(CrmInboxEvent, "save", save):
            result = deliver_claim(*claim, consumer=lambda *args: Client.objects.filter(pk=client.pk).update(name="Synthetic effect"))
        self.assertEqual(result, "retry")
        client.refresh_from_db()
        self.assertEqual(client.name, initial_name)
        self.assertIsNone(CrmInboxEvent.objects.get().processed_at)

    def test_in_transaction_flag_off_rolls_back_effect(self):
        client = self.message.conversation.client
        old_name = client.name
        def disable(event, source, key):
            Client.objects.filter(pk=client.pk).update(name="Synthetic effect")
            Organization.objects.filter(pk=self.org.pk).update(crm_intake_process=False)
        self.assertEqual(deliver_claim(*claim_one(**self.scope()), consumer=disable), "disabled")
        client.refresh_from_db()
        self.assertEqual(client.name, old_name)
        self.assertIsNone(CrmInboxEvent.objects.get().processed_at)

    def test_retry_failure_does_not_block_other_source_and_exhaustion_is_retained(self):
        second = self.incoming(external_message_id="second")
        def adapter(event, source, key):
            if source.pk == self.message.pk:
                raise RuntimeError("synthetic")
        self.assertEqual(run_worker(**self.scope(), consumer=adapter), {"retry": 1, "done": 1})
        event = CrmInboxEvent.objects.get(source_id=self.message.pk)
        for _ in range(MAX_ATTEMPTS - 1):
            claim = claim_one(**self.scope(), now=event.next_attempt_at)
            deliver_claim(*claim, consumer=adapter, now=event.next_attempt_at)
            event.refresh_from_db()
        self.assertEqual((event.state, event.attempts, event.next_attempt_at), ("failed", MAX_ATTEMPTS, None))
        self.assertEqual(inbox_status(**self.scope())["counts"], {"failed": 1, "done": 1})
        self.assertEqual(retry_failed(**self.scope(), limit=1), 1)
        self.assertEqual(CrmInboxEvent.objects.get(pk=event.pk).attempts, 0)

    def test_untrusted_source_snapshot_rewrite_and_foreign_client_are_blocked(self):
        claim = claim_one(**self.scope())
        scope = dict(self.message.intake_scope)
        scope["account"] = "rebound"
        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="rebound")
        ConversationMessage.objects.filter(pk=self.message.pk).update(intake_scope=scope)
        self.assertEqual(deliver_claim(*claim, consumer=lambda *args: self.fail("scope bypass")), "blocked")
        self.assertEqual(CrmInboxEvent.objects.get().account_id_snapshot, "synthetic-account")

    def test_supported_orm_cannot_relabel_or_delete_evidence(self):
        from django.core.exceptions import ValidationError
        event = CrmInboxEvent.objects.get()
        with self.assertRaises(ValidationError):
            CrmInboxEvent.objects.filter(pk=event.pk).update(account_id_snapshot="rebound")
        with self.assertRaises(ValidationError):
            CrmInboxEvent.objects.bulk_update([event], ["state"])
        with self.assertRaises(ValidationError):
            CrmInboxEvent.objects.bulk_create([])
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            CrmInboxEvent.objects.all().delete()
        event.account_id_snapshot = "rebound"
        with self.assertRaises(ValidationError):
            event.save()

    def test_call_older_revision_is_superseded_and_current_not_delivered_twice(self):
        self.megafon()
        self.history(status="missed")
        self.history()
        results = run_worker(**self.scope("call"), consumer=lambda *args: None)
        self.assertEqual(results, {"superseded": 1, "done": 1})
        self.assertEqual(list(CrmInboxEvent.objects.filter(source_type="call").order_by("revision").values_list("state", flat=True)), ["superseded", "done"])


class InboxReconciliationTests(Fixture, TestCase):
    def test_bounded_restart_same_timestamp_gap_and_dry_run(self):
        messages = [self.incoming(external_message_id=f"event-{n}") for n in range(3)]
        CrmInboxEvent.objects.all()._raw_delete("default")  # Trusted artificial missing inbox.
        # Same timestamps do not lose rows: cursor uses ordered source PKs.
        ConversationMessage.objects.update(intake_received_at=self.now)
        self.assertEqual(reconcile(**self.scope(), limit=1, dry_run=True), {"checked": 1, "dry_run": True})
        self.assertFalse(CrmInboxCursor.objects.exists())
        self.assertFalse(CrmInboxEvent.objects.exists())
        self.assertEqual(reconcile(**self.scope(), limit=1)["checked"], 1)
        cursor = CrmInboxCursor.objects.get()
        self.assertEqual(cursor.last_id, messages[0].pk)
        real_capture = capture_source
        def fail(source_type, source):
            if source.pk == messages[1].pk:
                raise RuntimeError("synthetic secret")
            return real_capture(source_type, source)
        with patch("pool_service.services.crm_inbox.capture_source", side_effect=fail):
            self.assertEqual(reconcile(**self.scope(), limit=2)["error_code"], "reconcile_failed")
        cursor.refresh_from_db()
        self.assertEqual(cursor.last_id, messages[0].pk)
        self.assertEqual(reconcile(**self.scope(), limit=2)["checked"], 2)
        self.assertEqual(CrmInboxEvent.objects.count(), 3)
        self.assertTrue(reconcile(**self.scope(), limit=2)["cycle_complete"])
        self.assertEqual(reconcile(**self.scope(), limit=3)["checked"], 3)
        self.assertEqual(CrmInboxEvent.objects.count(), 3)

    def test_no_implicit_legacy_scan_explicit_range_remains_unverified(self):
        conv = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="legacy", participant_name="Synthetic")
        msg = ConversationMessage.objects.create(conversation=conv, external_id="legacy", direction="in", body="Synthetic legacy")
        self.assertEqual(reconcile(**self.scope(), limit=1)["checked"], 0)
        self.assertFalse(CrmInboxEvent.objects.exists())
        with self.assertRaisesMessage(ValueError, "explicit_legacy_range_required"):
            reconcile(**self.scope(), include_legacy=True)
        reconcile(**self.scope(), include_legacy=True, after_id=0, through_id=msg.pk)
        event = CrmInboxEvent.objects.get()
        self.assertTrue(event.historical)
        self.assertFalse(event.scope_verified)
        self.assertEqual((event.state, event.account_id_snapshot), ("blocked", ""))

    def test_rebound_account_reconciliation_does_not_relabel_old_scope(self):
        original = self.incoming()
        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="rebound-account")
        self.connection.refresh_from_db()
        current = self.incoming(external_conversation_id="fresh-chat", participant_external_id="fresh-author", external_message_id="fresh-event")
        CrmInboxEvent.objects.filter(source_id=current.pk)._raw_delete("default")  # Trusted missing capture fixture.
        result = reconcile(**self.scope())
        self.assertEqual(result["checked"], 1)
        self.assertEqual(CrmInboxEvent.objects.get(source_id=original.pk).account_id_snapshot, "synthetic-account")
        self.assertEqual(CrmInboxEvent.objects.get(source_id=current.pk).account_id_snapshot, "rebound-account")

    def test_status_command_is_scoped_sanitized_and_unavailable_is_not_health(self):
        self.incoming()
        self.enable()
        run_worker(**self.scope())
        output = StringIO()
        call_command("crm_inbox", "status", organization=self.org.pk, source_type="message", connection=self.connection.pk, stdout=output)
        status = json.loads(output.getvalue())
        self.assertEqual(status["counts"], {"awaiting_consumer": 1})
        self.assertFalse(status["consumer_available"])
        self.assertNotIn("Synthetic private body", output.getvalue())
        other = Organization.objects.create(name="Other synthetic")
        with self.assertRaises(CommandError):
            call_command("crm_inbox", "status", organization=other.pk, source_type="message", connection=self.connection.pk, stdout=StringIO())


@override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False, COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False)
class InboxMySQLConcurrencyTests(Fixture, TransactionTestCase):
    def race(self, work):
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
            result = list(executor.map(execute, (0, 1)))
        self.assertNotEqual(result[0][0], result[1][0])
        return [row[1] for row in result]

    @skipUnlessDBFeature("has_select_for_update")
    def test_webhook_sync_same_source_one_source_inbox_and_notification(self):
        # Both paths converge through the real receive_message source transaction.
        occurred = self.now.replace(microsecond=0)
        self.race(lambda index: ingest_webhook(self.connection, self.avito_payload(int(occurred.timestamp())))[0].pk
            if index == 0 else self.incoming(intake_origin="recovery", occurred_at=occurred).pk)
        self.assertEqual(ConversationMessage.objects.count(), 1)
        self.assertEqual(CrmInboxEvent.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(self.notify.call_count, 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_workers_claim_and_ack_one_sql_effect(self):
        message = self.incoming()
        self.enable()
        from django.db.models import F
        def effect(event, source, key):
            # Durable synthetic SQL effect, not a Python list shared across threads.
            ConversationMessage.objects.filter(pk=source.pk).update(delivery_attempts=F("delivery_attempts") + 1)
        results = self.race(lambda index: run_worker(**self.scope(), limit=1, consumer=effect))
        self.assertEqual(sum(result.get("done", 0) for result in results), 1)
        self.assertEqual(CrmInboxEvent.objects.get().attempts, 1)
        self.assertEqual(ConversationMessage.objects.get(pk=message.pk).delivery_attempts, 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_reconcile_same_missing_source_inserts_once(self):
        self.incoming()
        CrmInboxEvent.objects.all()._raw_delete("default")  # Trusted missing-capture fixture.
        self.race(lambda index: reconcile(**self.scope(), limit=1))
        self.assertEqual(CrmInboxEvent.objects.count(), 1)
        self.assertEqual(CrmInboxCursor.objects.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_uncommitted_flag_off_wins_before_effect(self):
        self.incoming()
        self.enable()
        claim = claim_one(**self.scope())
        locked, release = Event(), Event()
        def disable():
            close_old_connections()
            try:
                with transaction.atomic():
                    org = Organization.objects.select_for_update().get(pk=self.org.pk)
                    org.crm_intake_process = False
                    org.save(update_fields=["crm_intake_process"])
                    locked.set()
                    if not release.wait(timeout=15):
                        raise AssertionError("synthetic timeout")
            finally:
                connections["default"].close()
        def deliver():
            close_old_connections()
            try:
                return deliver_claim(*claim, consumer=lambda *args: self.fail("disabled adapter effect"))
            finally:
                connections["default"].close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            mutation = executor.submit(disable)
            self.assertTrue(locked.wait(timeout=15))
            delivery = executor.submit(deliver)
            release.set()
            mutation.result(timeout=15)
            self.assertEqual(delivery.result(timeout=15), "disabled")
        self.assertEqual(CrmInboxEvent.objects.get().state, "pending")

    @skipUnlessDBFeature("has_select_for_update")
    def test_history_same_call_revision_race_one_source_per_fact(self):
        self.megafon()
        # Django clients are per-thread, sharing only synthetic request values.
        from django.test import Client as HttpClient
        url = reverse("megafon_webhook", args=[self.megafon_connection.public_id])
        results = self.race(lambda index: HttpClient().post(url, self.call_data).status_code)
        self.assertEqual(results, [200, 200])
        self.assertEqual((PhoneCall.objects.count(), CrmInboxEvent.objects.count()), (1, 1))
