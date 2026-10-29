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

    def test_new_activity_keeps_spam_trash_and_current_scope_hides_all_counts_and_replay(self):
        from pool_service.services.crm_lead_commands import execute_lead_command
        self.grant.transition_own_leads = True; self.grant.save()
        self.incoming(); self.deliver()
        lead = CrmLead.objects.get()
        payload = dict(actor=self.user, organization=self.org, operation_key="source-spam-synthetic",
            command_type="lead.disposition", subject_id=lead.pk, expected_revision=lead.revision,
            changes={"column": "spam", "reason": "Synthetic unwanted inquiry"})
        first = execute_lead_command(**payload)
        self.incoming(external_message_id="after-spam"); self.deliver()
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead.assignee_id, lead.needs_review), ("closed", "spam", self.user.pk, True))
        self.assertEqual(execute_lead_command(**payload), first)
        self.client.force_login(self.user)
        url = reverse("crm_live_lead_data") + "?org_id=" + str(self.org.pk)
        self.assertEqual({column["key"]: column["total"] for column in self.client.get(url).json()["columns"]}, {"unparsed": 0, "working": 0, "history": 0, "spam": 1, "deleted": 0})
        execute_lead_command(actor=self.user, organization=self.org, operation_key="source-trash-synthetic",
            command_type="lead.disposition", subject_id=lead.pk, expected_revision=lead.revision,
            changes={"column": "deleted", "reason": "Synthetic removal"})
        self.incoming(external_message_id="after-trash"); self.deliver()
        lead.refresh_from_db()
        self.assertTrue(lead.is_archived)
        self.assertEqual((lead.status, lead.result, CrmLeadSource.objects.filter(lead=lead).count()), ("closed", "spam", 3))
        self.assertEqual({column["key"]: column["total"] for column in self.client.get(url).json()["columns"]}, {"unparsed": 0, "working": 0, "history": 0, "spam": 0, "deleted": 1})
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_conversations=False)
        self.assertEqual(self.client.get(url).json()["total"], 0)
        self.assertEqual(self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]) + "?org_id=" + str(self.org.pk)).status_code, 404)
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            execute_lead_command(**payload)

    def test_source_metadata_lists_are_bounded_and_do_not_copy_message_bodies(self):
        self.incoming(participant_name='Synthetic sender', occurred_at=self.now)
        self.deliver()
        self.client.force_login(self.user)
        url = reverse('crm_live_lead_data') + '?org_id=' + str(self.org.pk)
        data = self.client.get(url).json()
        source = data['leads'][0]['source']
        self.assertEqual((source['label'], source['contact_name'], source['direction']), ('Сообщение Авито', 'Synthetic sender', 'in'))
        self.assertEqual(source['occurred_at'], self.now.isoformat())
        self.assertNotIn('Synthetic private body', json.dumps(data))
        self.assertNotIn('media', source)
        before = list(CrmInboxEvent.objects.values('state', 'attempts'))
        self.client.get(url)
        self.assertEqual(list(CrmInboxEvent.objects.values('state', 'attempts')), before)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_conversations=False)
        self.assertEqual(self.client.get(url).json()['total'], 0)

    def test_saved_call_content_requires_listen_right_and_get_never_starts_analysis(self):
        from pool_service.communication_models import CallAnalysis
        self.mapped_call(); self.deliver('call')
        call = PhoneCall.objects.get()
        call.recording_file = 'synthetic/never-fetched.wav'; call.save(update_fields=['recording_file'])
        analysis, _ = CallAnalysis.objects.update_or_create(call=call, defaults=dict(status=CallAnalysis.STATUS_READY,
            transcript='Synthetic transcript', summary='Synthetic summary', facts={'client_name':'Synthetic person', 'phone':'+79000000000', 'request':'Synthetic need', 'private_extra':'not projected'}))
        lead = CrmLead.objects.get()
        self.client.force_login(self.user)
        detail = reverse('crm_live_lead_detail', args=[lead.pk]) + '?org_id=' + str(self.org.pk)
        data_url = reverse('crm_live_lead_data') + '?org_id=' + str(self.org.pk)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_listen_calls=False)
        source = self.client.get(detail).json()['sources'][0]
        self.assertEqual((source['phone'], source['employee']), (call.phone_number, self.employee.display_name))
        self.assertNotIn('media', source)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_listen_calls=True)
        before = (lead.revision, analysis.attempts, analysis.requested_at, CrmEvent.objects.count())
        with patch('pool_service.communication_views.request_call_analysis') as request_analysis, patch('pool_service.communication_views.start_requested_call_analysis_worker') as worker:
            for _ in range(2):
                response = self.client.get(detail)
                self.assertEqual(response.status_code, 200)
                media = response.json()['sources'][0]['media']
                self.assertEqual(media['transcript'], 'Synthetic transcript')
                self.assertEqual(media['facts']['request'], 'Synthetic need')
                self.assertNotIn('private_extra', media['facts'])
                self.assertEqual(media['recording_url'], reverse('communication_call_recording', args=[call.pk]) + '?org_id=' + str(self.org.pk))
            request_analysis.assert_not_called(); worker.assert_not_called()
        self.assertNotIn('Synthetic transcript', self.client.get(data_url).content.decode())
        lead.refresh_from_db(); analysis.refresh_from_db()
        self.assertEqual((lead.revision, analysis.attempts, analysis.requested_at, CrmEvent.objects.count()), before)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_listen_calls=False)
        self.assertNotIn('media', self.client.get(detail).json()['sources'][0])
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, phone_key="9001234567")
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertEqual(self.client.get(data_url).json()['total'], 0)

    def test_unknown_call_metadata_and_unready_transcript_remain_unknown(self):
        from pool_service.communication_models import CallAnalysis
        self.megafon(); self.history(); self.deliver('call')
        call = PhoneCall.objects.get()
        CallAnalysis.objects.update_or_create(call=call, defaults=dict(status=CallAnalysis.STATUS_PENDING,
            transcript='Unready synthetic content', facts={'request':'Not ready'}))
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_listen_calls=True)
        lead = CrmLead.objects.get(); self.client.force_login(self.user)
        data = self.client.get(reverse('crm_live_lead_detail', args=[lead.pk]) + '?org_id=' + str(self.org.pk)).json()
        source = data['sources'][0]
        self.assertEqual(source['employee'], '')
        self.assertIsNone(source['media']['recording_url'])
        self.assertNotIn('transcript', source['media'])
        self.assertNotIn('Not ready', json.dumps(data))

    def test_recording_explicit_organization_and_current_listening_permissions(self):
        from io import BytesIO
        from pool_service.communication_models import TelephonyConnection
        second = Organization.objects.create(name='Synthetic second source organization')
        OrganizationAccess.objects.create(organization=second, user=self.user, role='manager')
        CommunicationAccess.objects.filter(user=self.user, organization=second).update(can_view_all_calls=True, can_listen_calls=True)
        connection = TelephonyConnection.objects.create(organization=second, external_id='synthetic-second-line')
        call = PhoneCall.objects.create(organization=second, connection=connection, employee=self.user,
            external_id='synthetic-second-call', phone_number='+79005550000', direction='in', started_at=self.now,
            recording_file='synthetic-second.wav')
        self.client.force_login(self.user)
        url = reverse('communication_call_recording', args=[call.pk])
        # Mock file IO only; real scope and listen guards run on each request.
        with patch('pool_service.communication_views.os.path.getsize', return_value=4), patch('builtins.open', return_value=BytesIO(b'RIFF')):
            response = self.client.get(url + '?org_id=' + str(second.pk))
            self.assertEqual(response.status_code, 200); response.close()
        self.assertEqual(self.client.get(url + '?org_id=' + str(self.org.pk)).status_code, 404)
        foreign = Organization.objects.create(name='Synthetic inaccessible organization')
        self.assertEqual(self.client.get(url + '?org_id=' + str(foreign.pk)).status_code, 403)
        self.assertEqual(self.client.get(url + '?org_id=' + str(second.pk) + '&org_id=' + str(self.org.pk)).status_code, 403)
        CommunicationAccess.objects.filter(user=self.user, organization=second).update(can_listen_calls=False)
        self.assertEqual(self.client.get(url + '?org_id=' + str(second.pk)).status_code, 403)

    def test_source_contact_link_and_employee_labels_respect_current_org(self):
        from pool_service.models import Client
        call = self.mapped_call(); self.deliver('call')
        lead = CrmLead.objects.get()
        peer = Client.objects.create(organization=self.org, name='Synthetic actual caller', client_type='private')
        call.client = peer; call.save(update_fields=['client'])
        source = source_metadata(user=self.user, organization=self.org, lead=lead)[0]
        self.assertEqual(source['contact']['url'], reverse('client_detail',args=[peer.pk]))
        self.assertIn(call.phone_number,source['caller'])
        self.assertEqual(source['callee'],self.employee.display_name)
        foreign_org=Organization.objects.create(name='Foreign')
        foreign_employee=Employee.objects.create(organization=foreign_org,display_name='Foreign hidden employee')
        foreign=Client.objects.create(organization=foreign_org,name='Foreign hidden peer',client_type='private')
        PhoneCall.objects.filter(pk=call.pk).update(client=foreign,employee_profile=foreign_employee)
        source = source_metadata(user=self.user, organization=self.org, lead=lead)[0]
        self.assertIsNone(source['contact'])
        self.assertNotIn('Foreign hidden',str(source))
