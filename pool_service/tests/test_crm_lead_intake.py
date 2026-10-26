"""Real source capture -> SQL consumer -> current lead visibility, synthetic data."""
from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import CommunicationAccess, PhoneCall, TelephonyEmployeeIdentity
from pool_service.crm_commercial_models import CrmAccess, CrmEvent, CrmIntakeActivation, CrmLead, CrmLeadSource, CrmLeadThread
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.models import Employee, Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_inbox import claim_one, deliver_claim
from pool_service.services.crm_lead_access import can_read_lead, leads_for_user, source_metadata
from pool_service.services.crm_lead_intake import consume_incoming
from pool_service.tests.test_crm_inbox import Fixture


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class LeadIntakeTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.crm_v2_read = self.org.crm_v2_write = self.org.crm_intake_process = True
        self.org.paid_until = self.now + timedelta(days=30)
        self.org.save()
        self.activation = CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now - timedelta(minutes=30))
        self.user = get_user_model().objects.create_user("intake-synthetic-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic intake")
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.user,
            view_leads=True, edit_own_leads=True, take_leads=True)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(
            can_view_conversations=True, can_view_all_calls=True, can_view_own_calls=True)

    def deliver(self, kind="message"):
        claim = claim_one(**self.scope(kind))
        self.assertIsNotNone(claim)
        # Exercise the consumer directly in a rollback savepoint first, so a
        # validation failure has a useful test traceback rather than retry code.
        event = CrmInboxEvent.objects.get(pk=claim[0])
        from pool_service.services.crm_inbox import _locked_source
        with transaction.atomic():
            _org, source, error = _locked_source(event.source_type, event.source_id,
                event.channel_id_snapshot, event.organization_id)
            if not error:
                consume_incoming(event, source, f"{event.source_key}:{event.revision}")
                transaction.set_rollback(True)
        self.assertEqual(deliver_claim(*claim, consumer=consume_incoming), "done")
        return CrmInboxEvent.objects.get(pk=event.pk)

    def mapped_call(self, **changes):
        self.megafon()
        TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension="101", external_user="synthetic-provider", employee=self.employee,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED, match_method="manual",
            confirmed_by=self.user, confirmed_at=self.now)
        self.assertEqual(self.history(user="synthetic-provider", ext="101", **changes).status_code, 200)
        return PhoneCall.objects.get(connection=self.telephony)

    def test_messages_group_only_confirmed_conversation_and_do_not_copy_content(self):
        self.incoming()
        self.assertEqual(self.deliver().error_code, "lead_created")
        first = CrmLead.objects.get()
        self.assertTrue(leads_for_user(self.user, self.org).filter(pk=first.pk).exists())
        self.assertTrue(can_read_lead(self.user, self.org, first))
        self.incoming()  # provider replay is already captured once
        self.assertIsNone(claim_one(**self.scope()))
        self.incoming(external_message_id="second")
        self.assertEqual(self.deliver().error_code, "lead_linked")
        self.assertEqual((CrmLead.objects.count(), CrmLeadSource.objects.count()), (1, 2))
        self.assertEqual(ServiceTask.objects.count(), 0)
        self.assertNotIn("Synthetic private body", first.title + first.need + str(list(CrmEvent.objects.values())))
        self.incoming(external_conversation_id="other-need", external_message_id="third")
        self.deliver()
        self.assertEqual(CrmLead.objects.count(), 2)
        self.assertEqual(len(source_metadata(user=self.user, organization=self.org, lead=first)), 2)

    def test_earlier_capture_and_historical_events_do_not_start_current_queue(self):
        self.activation.delete()
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now + timedelta(minutes=1))
        self.incoming()
        self.assertEqual(self.deliver().error_code, "historical_ignored")
        self.assertFalse(CrmLead.objects.exists())

    def test_missing_activation_retries_without_ack_or_business_effect(self):
        self.activation.delete()
        self.incoming()
        claim = claim_one(**self.scope())
        self.assertEqual(deliver_claim(*claim, consumer=consume_incoming), "retry")
        self.assertFalse(CrmLead.objects.exists())

    def test_confirmed_answer_assigns_current_employee_without_invented_sla(self):
        self.mapped_call()
        self.deliver("call")
        lead = CrmLead.objects.get()
        self.assertEqual((lead.assignee_id, lead.status, lead.intake_reason),
            (self.user.pk, "working", "needs_qualification"))
        self.assertTrue(lead.needs_review)
        self.assertIsNone(lead.next_task_id)
        self.assertIsNone(lead.follow_up_at)

    def test_missed_or_unmapped_stays_free_and_late_answer_cannot_replace_owner(self):
        self.mapped_call(status="missed")
        self.deliver("call")
        lead = CrmLead.objects.get()
        self.assertIsNone(lead.assignee_id)
        self.assertEqual(lead.intake_reason, "missed_call")
        other = get_user_model().objects.create_user("synthetic-explicit-taker")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        lead.assignee, lead.status = other, "working"
        lead.save()
        self.history(user="synthetic-provider", ext="101", status="success")
        self.deliver("call")
        lead.refresh_from_db()
        self.assertEqual(lead.assignee_id, other.pk)
        self.assertEqual(CrmLead.objects.count(), 1)

    def test_current_communication_revocation_or_rebinding_hides_card(self):
        message = self.incoming()
        self.deliver()
        lead = CrmLead.objects.get()
        access = CommunicationAccess.objects.get(user=self.user, organization=self.org)
        access.can_view_conversations = False
        access.save()
        self.assertFalse(leads_for_user(self.user, self.org).exists())
        self.assertFalse(can_read_lead(self.user, self.org, lead))
        access.can_view_conversations = True
        access.save()
        scope = dict(message.intake_scope)
        scope["namespace"] = "different-conversation"
        message.intake_scope = scope
        message.save(update_fields=["intake_scope"])
        self.assertFalse(leads_for_user(self.user, self.org).exists())

    def test_private_call_before_intake_never_creates_lead(self):
        self.mapped_call()
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, phone_key="9001234567")
        self.assertEqual(self.deliver("call").error_code, "private_ignored")
        self.assertFalse(CrmLead.objects.exists())

    def test_corrupt_source_tenant_hides_both_original_and_derived_lead(self):
        self.incoming()
        self.deliver()
        original = CrmLead.objects.get()
        derived = CrmLead.objects.create(organization=self.org, title="Synthetic derived need",
            origin_lead=original, assignee=self.user)
        self.assertEqual(set(leads_for_user(self.user, self.org).values_list("pk", flat=True)),
            {original.pk, derived.pk})
        other = Organization.objects.create(name="Synthetic foreign source tenant")
        source = CrmLeadSource.objects.get(lead=original)
        # Simulate storage corruption in the isolated test database. Supported
        # ORM writes already prohibit this immutable provenance reassignment.
        table = connection.ops.quote_name(CrmLeadSource._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET organization_id = %s WHERE id = %s", [other.pk, source.pk])
        self.assertFalse(leads_for_user(self.user, self.org).exists())
        for lead in (original, derived):
            self.assertFalse(can_read_lead(self.user, self.org, lead))
            self.assertEqual(source_metadata(user=self.user, organization=self.org, lead=lead), [])

    def test_later_privacy_and_whitespace_aliases_hide_list_and_detail(self):
        call = self.mapped_call()
        self.deliver("call")
        lead = CrmLead.objects.get()
        self.assertTrue(can_read_lead(self.user, self.org, lead))
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, phone_key="9001234567")
        for value in ("+7 900 123-45-67", "89001234567", "9001234567", "\u00a0+7 (900) 123-45-67\u00a0", "\v+79001234567\f"):
            with self.subTest(value=value):
                call.phone_number = value
                call.save(update_fields=["phone_number"])
                self.assertFalse(leads_for_user(self.user, self.org).exists())
                self.assertFalse(can_read_lead(self.user, self.org, lead))
                self.assertEqual(source_metadata(user=self.user, organization=self.org, lead=lead), [])

    def test_closed_thread_receives_activity_without_reopening_or_tasks(self):
        self.incoming()
        self.deliver()
        lead = CrmLead.objects.get()
        lead.status, lead.result, lead.reason = "closed", "no_deal", "Synthetic explicit result"
        lead.needs_review = False
        lead.save()
        self.incoming(external_message_id="later")
        self.deliver()
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.result), ("closed", "no_deal"))
        self.assertTrue(lead.needs_review)
        self.assertEqual((CrmLead.objects.count(), ServiceTask.objects.count()), (1, 0))

    def test_worker_heartbeat_and_current_processing_are_bounded(self):
        self.incoming()
        out = StringIO()
        call_command("run_crm_intake", organization=self.org.pk, limit=1, seconds=2, stdout=out)
        result = json.loads(out.getvalue())
        self.assertEqual((result["status"], result["processed"]), ("complete", 1))
        self.activation.refresh_from_db()
        self.assertIsNotNone(self.activation.last_run_at)
        self.assertFalse(self.activation.last_error)
