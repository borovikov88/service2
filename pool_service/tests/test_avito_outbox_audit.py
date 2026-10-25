import hashlib
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from pool_service import avito_outbox_audit as audit
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationChannel, Conversation, ConversationMessage,
)
from pool_service.models import Organization, OrganizationAccess
from scripts import avito_outbox_receipt as receipt


def worker():
    return {"worker_state": "not_configured", "last_started_at": None, "last_success_at": None,
        "last_error_code": "", "pending_count": 1, "oldest_pending_at": audit.INCIDENT_START.isoformat(),
        "oldest_pending_age_seconds": 60, "unknown_count": 0, "failed_count": 0, "legacy_held_count": 1}


def scheduler():
    return {"source": "hosting_user_crontab_cli", "state": "unavailable",
        "services_outbox_entry_present": None, "other_outbox_entry_present": None,
        "account_monitor_entry_present": None, "panel_jobs_verified": False}


def chain():
    return {"source": "avito_webhook_chain_v1", "state": "awaiting_first", "generation_bound": True,
        "authenticated_event": False, "conversation_bound": False, "message_persisted": False,
        "event_result": None, "received_at": None, "owner_notification_state": "awaiting_first",
        "owner_notification_recorded": False, "owner_notification_error": None, "push_delivery_confirmed": False}


class OutboxIncidentTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Outbox incident")
        self.owner = User.objects.create_user(username="outbox-incident-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Services")
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel, name="Services", external_id="446312402")
        AvitoCredential.objects.create(connection=self.connection, client_id_encrypted="test-id", client_secret_encrypted="test-secret")
        self.conversation = Conversation.objects.create(organization=self.org, connection=self.connection,
            external_id="test-private-chat", participant_name="Test participant")
        self.message = ConversationMessage.objects.create(conversation=self.conversation, direction="out",
            body="Generic agreed test reply", sent_by=self.owner, delivery_status="pending")
        ConversationMessage.objects.filter(pk=self.message.pk).update(created_at=audit.INCIDENT_START + timedelta(seconds=3))
        self.message.refresh_from_db()
        from pool_service import avito_outbox_original_recovery as recovery
        for name, value in (("BODY_SHA", hashlib.sha256(self.message.body.encode()).hexdigest()),
                            ("CONVERSATION_UUID_SHA", hashlib.sha256(str(self.conversation.uuid).encode()).hexdigest())):
            started = patch.object(recovery, name, value)
            started.start()
            self.addCleanup(started.stop)

    def collect(self, proof=None):
        proof = proof or {"state": "absent", "error_code": "", "complete": True,
            "pages": 1, "messages_checked": 3, "provider_id": None}
        with patch("pool_service.avito_outbox.outbox_diagnostics", return_value=worker()), \
             patch("pool_service.avito_outbox.reconcile_message", return_value=proof) as reconcile, \
             patch.object(audit, "scheduler_inspection", return_value=scheduler()), \
             patch.object(audit, "webhook_summary", return_value=chain()):
            return audit.collect(), reconcile.call_args_list

    def test_exact_unique_incident_is_read_only_and_exports_no_customer_payload(self):
        before = dict(ConversationMessage.objects.values().get(pk=self.message.pk))
        result, calls = self.collect()
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["message"]["local_id"], self.message.pk)
        self.assertEqual(result["message"]["attempts"], 0)
        self.assertEqual(result["message"]["post_evidence"], "not_claimed")
        self.assertEqual(result["message"]["body_sha256"], hashlib.sha256(self.message.body.encode()).hexdigest())
        self.assertEqual(result["reconciliation"]["state"], "absent")
        self.assertEqual(calls[0].args, (4, self.message.pk))
        self.assertNotIn(self.message.body, json.dumps(result))
        self.assertNotIn("test-private-chat", json.dumps(result))
        self.assertEqual(before, dict(ConversationMessage.objects.values().get(pk=self.message.pk)))

    def test_already_accepted_row_is_included_without_resend_or_provider_id_export(self):
        ConversationMessage.objects.filter(pk=self.message.pk).update(delivery_status="delivered", external_id="private-provider-id")
        result, _ = self.collect({"state": "accepted", "error_code": "", "complete": True,
            "pages": 1, "messages_checked": 3, "provider_id": "private-provider-id"})
        self.assertEqual(result["message"]["post_evidence"], "accepted_local")
        self.assertTrue(result["reconciliation"]["provider_id_present"])
        self.assertNotIn("private-provider-id", json.dumps(result))

    def test_two_candidates_never_choose_one_or_read_any_provider_chat(self):
        second = ConversationMessage.objects.create(conversation=self.conversation, direction="out", body="Second generic reply",
            delivery_status="pending", sent_by=self.owner)
        ConversationMessage.objects.filter(pk=second.pk).update(created_at=audit.INCIDENT_START)
        result, calls = self.collect()
        self.assertEqual(result["candidate_count"], 2)
        self.assertIsNone(result["message"])
        self.assertEqual(calls, [])
        self.assertEqual(result["reconciliation"]["state"], "not_checked")

    def test_outside_minute_foreign_connection_and_foreign_org_are_not_selected(self):
        ConversationMessage.objects.filter(pk=self.message.pk).update(created_at=audit.INCIDENT_END)
        result, calls = self.collect()
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(calls, [])
        ConversationMessage.objects.filter(pk=self.message.pk).update(created_at=audit.INCIDENT_START)
        other_org = Organization.objects.create(name="Foreign organization")
        Conversation.objects.filter(pk=self.conversation.pk).update(organization=other_org)
        result, calls = self.collect()
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(ValueError, "scope_invalid"):
            audit.collect(connection_id=1, account_id="446312402")
        with self.assertRaisesRegex(ValueError, "scope_invalid"):
            audit.collect(connection_id=4, account_id="111")

    def test_changed_message_during_read_never_exports_absence_as_authorization(self):
        def changed(*args):
            ConversationMessage.objects.filter(pk=self.message.pk).update(delivery_attempts=1, delivery_status="sending")
            return {"state": "absent", "error_code": "", "complete": True, "pages": 1,
                "messages_checked": 0, "provider_id": None}
        with patch("pool_service.avito_outbox.outbox_diagnostics", return_value=worker()), \
             patch("pool_service.avito_outbox.reconcile_message", side_effect=changed), \
             patch.object(audit, "scheduler_inspection", return_value=scheduler()), \
             patch.object(audit, "webhook_summary", return_value=chain()):
            result = audit.collect()
        self.assertEqual(result["reconciliation"]["state"], "unknown")
        self.assertEqual(result["reconciliation"]["error_code"], "audit_binding_changed")

    def test_saved_raw_provider_error_is_never_logged(self):
        ConversationMessage.objects.filter(pk=self.message.pk).update(delivery_error="PRIVATE token body")
        result, _ = self.collect()
        self.assertTrue(result["message"]["has_saved_error"])
        self.assertEqual(result["message"]["error_code"], "legacy_error_unclassified")
        self.assertNotIn("PRIVATE", json.dumps(result))


class SchedulerInspectionTests(SimpleTestCase):
    def test_missing_cli_means_panel_jobs_unknown_not_absent(self):
        with patch.object(audit.shutil, "which", return_value=None):
            result = audit.scheduler_inspection()
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["services_outbox_entry_present"])
        self.assertFalse(result["panel_jobs_verified"])

    def test_cli_table_is_summarized_without_other_commands_or_secrets(self):
        table = b"* * * * * python manage.py send_avito_outbox --connection-id 4\n7 * * * * monitor_avito_statuses\n* * * * * PRIVATE_SECRET=value private-command\n"
        with patch.object(audit.shutil, "which", return_value="/usr/bin/crontab"), \
             patch.object(audit.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=table)) as run:
            result = audit.scheduler_inspection()
        self.assertTrue(result["services_outbox_entry_present"])
        self.assertTrue(result["account_monitor_entry_present"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/crontab", "-l"])

    def test_failed_cli_cannot_claim_absent(self):
        with patch.object(audit.shutil, "which", return_value="/usr/bin/crontab"), \
             patch.object(audit.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=b"")):
            result = audit.scheduler_inspection()
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["services_outbox_entry_present"])
