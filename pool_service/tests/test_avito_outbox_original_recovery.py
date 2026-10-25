import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from pool_service import avito_outbox as service
from pool_service import avito_outbox_original_recovery as recovery
from pool_service.communication_models import CommunicationAccess, ConversationMessage
from pool_service.tests import test_avito_outbox_audit as incident_tests
from scripts import avito_outbox_recovery_receipt as receipt

ABSENT = {"state": "absent", "error_code": "", "complete": True,
    "pages": 1, "messages_checked": 1, "provider_id": None}
ACCEPTED = {**ABSENT, "state": "accepted", "provider_id": "provider-test-message"}


class OriginalRecoveryTests(TransactionTestCase):
    def setUp(self):
        incident_tests.OutboxIncidentTests.setUp(self)
        with patch.object(service, "_provider_context", return_value="test-token"):
            service.configure_outbox(4, "446312402")
        authorized = patch.object(recovery, "_authorized_release", return_value=None)
        authorized.start()
        self.addCleanup(authorized.stop)

    def accepted_sender(self, message, *, token=None):
        self.assertFalse(connection.in_atomic_block)
        message.delivery_status = "delivered"
        message.external_id = "provider-test-message"
        message.delivered_at = timezone.now()
        message.delivery_error = ""
        message.save(update_fields=["delivery_status", "external_id", "delivered_at", "delivery_error"])

    def recover(self, proof=ABSENT, provider_proof=ABSENT, sender=None):
        with patch.object(service, "reconcile_message", return_value=proof) as first, \
             patch.object(service, "_provider_context", return_value="test-token"), \
             patch.object(service, "_reconcile", return_value=provider_proof), \
             patch.object(recovery.communication_avito, "send_message", side_effect=sender or self.accepted_sender) as send:
            result = recovery.recover(expected_sha="a" * 40)
            return result, first.call_count, send.call_count

    def test_one_existing_original_is_accepted_and_repeated_command_cannot_send_again(self):
        original_count = ConversationMessage.objects.count()
        result, reads, sends = self.recover()
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual(result["local_id"], self.message.pk)
        self.assertEqual(result["send_function_invocations"], 1)
        self.assertEqual(sends, 1)
        self.assertFalse(result["client_read_confirmed"])
        repeated, reads, sends = self.recover()
        self.assertEqual(repeated["outcome"], "accepted")
        self.assertTrue(repeated["ledger_reused"])
        self.assertEqual((reads, sends), (0, 0))
        self.assertEqual(ConversationMessage.objects.count(), original_count)
        self.message.refresh_from_db()
        self.assertEqual((self.message.delivery_status, self.message.delivery_attempts), ("delivered", 1))

    def test_already_in_provider_history_updates_original_without_any_send(self):
        result, reads, sends = self.recover(proof=ACCEPTED, provider_proof=ABSENT)
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual((reads, sends), (1, 0))
        self.assertEqual(result["send_function_invocations"], 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.external_id, "provider-test-message")
        self.assertEqual(self.message.delivery_attempts, 0)

    def test_unknown_history_or_previously_started_attempt_never_sends(self):
        unknown = {**ABSENT, "state": "unknown", "complete": False,
            "pages": 0, "messages_checked": 0, "error_code": "provider_reconcile_incomplete"}
        result, _, sends = self.recover(proof=unknown)
        self.assertEqual((result["outcome"], sends), ("unknown", 0))
        self.message.refresh_from_db()
        self.assertEqual((self.message.delivery_status, self.message.delivery_attempts), ("pending", 0))
        self.assertEqual(self.message.delivery_error, "provider_reconcile_incomplete")

    def test_concurrent_claim_is_not_reverted_by_recording_a_read_block(self):
        unknown = {**ABSENT, "state": "unknown", "complete": False,
            "pages": 0, "messages_checked": 0, "error_code": "provider_reconcile_incomplete"}
        record = service._record
        def concurrent_claim(*args, **kwargs):
            ConversationMessage.objects.filter(pk=self.message.pk).update(
                delivery_status="sending", delivery_attempts=1, delivery_error="provider_delivery_unknown")
            return record(*args, **kwargs)
        with patch.object(service, "_record", side_effect=concurrent_claim):
            result, _, sends = self.recover(proof=unknown)
        self.assertEqual(sends, 0)
        self.assertEqual(result["outcome"], "unknown")
        self.message.refresh_from_db()
        self.assertEqual((self.message.delivery_status, self.message.delivery_attempts), ("sending", 1))
        self.assertEqual(self.message.delivery_error, "provider_delivery_unknown")

    def test_previous_attempt_and_apparent_absence_never_authorizes_post(self):
        ConversationMessage.objects.filter(pk=self.message.pk).update(delivery_status="sending", delivery_attempts=1)
        result, _, sends = self.recover()
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(sends, 0)

    def test_same_org_superuser_uses_canonical_reply_capability_without_explicit_row(self):
        actor = User.objects.create_user(username="superuser-recovery-actor", is_superuser=True)
        recovery.OrganizationAccess.objects.create(user=actor, organization=self.org, role="manager")
        CommunicationAccess.objects.filter(user=actor, organization=self.org).delete()
        ConversationMessage.objects.filter(pk=self.message.pk).update(sent_by=actor)
        result, reads, sends = self.recover(proof=ACCEPTED)
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual((reads, sends), (1, 0))

    def test_accountant_stale_explicit_reply_flag_cannot_authorize_recovery(self):
        actor = User.objects.create_user(username="accountant-recovery-actor")
        recovery.OrganizationAccess.objects.create(user=actor, organization=self.org, role="accountant")
        CommunicationAccess.objects.filter(user=actor, organization=self.org).update(can_reply_conversations=True)
        ConversationMessage.objects.filter(pk=self.message.pk).update(sent_by=actor)
        result, reads, sends = self.recover()
        self.assertEqual(result["error_code"], "original_target_mismatch")
        self.assertEqual((reads, sends), (0, 0))

    def test_inactive_or_foreign_superuser_does_not_bypass_explicit_membership(self):
        actor = User.objects.create_user(username="foreign-recovery-actor", is_superuser=True)
        ConversationMessage.objects.filter(pk=self.message.pk).update(sent_by=actor)
        result, reads, sends = self.recover()
        self.assertEqual(result["error_code"], "original_target_mismatch")
        self.assertEqual((reads, sends), (0, 0))
        recovery.OrganizationAccess.objects.create(user=actor, organization=self.org, role="manager")
        actor.is_active = False
        actor.save(update_fields=["is_active"])
        result, reads, sends = self.recover()
        self.assertEqual(result["error_code"], "original_target_mismatch")
        self.assertEqual((reads, sends), (0, 0))

    def test_exact_original_body_uuid_and_current_actor_are_required(self):
        ConversationMessage.objects.filter(pk=self.message.pk).update(body="Changed generic reply")
        result, reads, sends = self.recover()
        self.assertEqual(result["error_code"], "original_target_mismatch")
        self.assertEqual((reads, sends), (0, 0))

    def test_revoked_reply_permission_blocks_before_provider_read(self):
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_reply_conversations=False)
        result, reads, sends = self.recover()
        self.assertEqual(result["error_code"], "original_target_mismatch")
        self.assertEqual((reads, sends), (0, 0))

    def test_uuid_change_and_multiple_candidates_block_before_provider_read(self):
        import uuid
        self.conversation.uuid = uuid.uuid4()
        self.conversation.save(update_fields=["uuid"])
        result, reads, sends = self.recover()
        self.assertEqual(result["error_code"], "original_target_mismatch")
        self.assertEqual((reads, sends), (0, 0))

    def test_changed_body_during_first_get_is_held_without_post(self):
        def changed(*args):
            ConversationMessage.objects.filter(pk=self.message.pk).update(body="Later generic edit")
            return ABSENT
        with patch.object(service, "reconcile_message", side_effect=changed), \
             patch.object(recovery.communication_avito, "send_message") as send:
            result = recovery.recover(expected_sha="a" * 40)
        self.assertEqual(result["error_code"], "original_target_changed")
        send.assert_not_called()

    def test_interrupted_ledger_is_not_a_second_send_permission(self):
        item = self.connection
        item.refresh_from_db()
        settings = dict(item.settings)
        settings[recovery.LEDGER_KEY] = {"version": 1, "state": "running", "local_id": self.message.pk}
        item.settings = settings
        item.save(update_fields=["settings"])
        result, reads, sends = self.recover()
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual((reads, sends), (0, 0))

    def test_failure_after_send_path_is_unknown_and_repeat_does_not_send(self):
        result, _, sends = self.recover(sender=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private")))
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(sends, 1)
        self.assertNotIn("private", json.dumps(result))
        repeated, reads, sends = self.recover()
        self.assertTrue(repeated["ledger_reused"])
        self.assertEqual((reads, sends), (0, 0))


class OriginalAuthorizationTests(SimpleTestCase):
    def test_missing_marker_or_wrong_sha_never_authorizes_operation(self):
        with patch.dict(os.environ, {"SERVICE2_AVITO_OUTBOX_RECOVERY": "1"}):
            def run(argv, **kwargs):
                return SimpleNamespace(returncode=0, stdout=(b"a" * 40 + b"\n") if argv[1] == "rev-parse" else b"ordinary release")
            with patch.object(recovery.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(ValueError, "release_not_authorized"):
                    recovery._authorized_release("a" * 40)
        with patch.dict(os.environ, {"SERVICE2_AVITO_OUTBOX_RECOVERY": ""}):
            with self.assertRaisesRegex(ValueError, "release_not_authorized"):
                recovery._authorized_release("a" * 40)

    def test_exact_marker_is_checked_in_actual_commit(self):
        with patch.dict(os.environ, {"SERVICE2_AVITO_OUTBOX_RECOVERY": "1"}):
            def run(argv, **kwargs):
                return SimpleNamespace(returncode=0, stdout=(b"a" * 40 + b"\n") if argv[1] == "rev-parse"
                    else recovery.MARKER.encode())
            with patch.object(recovery.subprocess, "run", side_effect=run):
                recovery._authorized_release("a" * 40)

    def test_recovery_receipt_rejects_fake_read_or_unapproved_send(self):
        value = recovery._base()
        self.assertEqual(receipt.normalize(value)["outcome"], "held")
        with self.assertRaises(ValueError):
            receipt.normalize(value | {"client_read_confirmed": True})
        with self.assertRaises(ValueError):
            receipt.normalize(value | {"send_function_invocations": 1})
