"""Episodes retain current staff-call authority and bounded board projections."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pool_service.client_crm_models import ClientCompanyLink
from pool_service.communication_models import CommunicationAccess, PhoneCall
from pool_service.crm_commercial_models import CrmAccess, CrmLead, CrmLeadSource
from pool_service.models import Client, Employee, OrganizationAccess
from pool_service.services.call_access import can_read_call
from pool_service.services.crm_lead_access import can_read_lead
from pool_service.services.crm_lead_identity import identity_projection, lead_identity
from pool_service.services.owner_call_privacy import contact_state, set_personal_peer
from pool_service.tests.test_crm_contact_episodes import EpisodeFixture


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class EpisodeIntegrationTests(EpisodeFixture, TestCase):
    def staff(self, name):
        user = get_user_model().objects.create_user(name)
        OrganizationAccess.objects.create(user=user, organization=self.org, role="manager")
        Employee.objects.create(user=user, organization=self.org, display_name=name)
        CrmAccess.objects.create(user=user, organization=self.org, view_leads=True, edit_own_leads=True)
        CommunicationAccess.objects.filter(user=user, organization=self.org).update(
            can_view_all_calls=True, can_view_own_calls=True, can_view_conversations=True)
        return user

    def participant_call(self, participant, **changes):
        call = self.call(**changes)
        call.employee = participant
        call.save(update_fields=["employee"])
        return call

    def mark_personal(self):
        state = contact_state(self.user, self.person)
        set_personal_peer(self.user, self.person.pk, "1", state["revision"])

    def test_mixed_private_and_staff_sources_keep_one_active_work_episode(self):
        staff = self.staff("synthetic-mixed-worker")
        work = self.participant_call(staff); self.deliver("call")
        lead = CrmLead.objects.get()
        private = self.participant_call(self.user, type="out"); self.deliver("call")
        self.mark_personal()
        self.assertTrue(can_read_lead(self.user, self.org, lead))
        self.assertEqual(lead_identity(lead, validate_sources=True).contact, self.person)
        followup = self.participant_call(staff, type="out")
        event = self.deliver("call")
        self.assertEqual(event.error_code, "lead_linked")
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(CrmLeadSource.objects.get(inbox=event).lead_id, lead.pk)
        rows = {item["id"] for item in self.activity(lead)}
        self.assertTrue({f"call:{work.pk}", f"call:{followup.pk}"} <= rows)
        self.assertNotIn(f"call:{private.pk}", rows)

    def test_corporate_message_preserves_identity_after_owner_source_becomes_private(self):
        self.participant_call(self.user); self.deliver("call")
        self.incoming(participant_phone=self.person.phone); self.deliver()
        lead = CrmLead.objects.get()
        self.mark_personal()
        self.incoming(participant_phone=self.person.phone, external_conversation_id="new-work-dialog",
            external_message_id="new-work-message", participant_external_id="new-work-person")
        event = self.deliver()
        self.assertEqual(event.error_code, "lead_linked")
        self.assertEqual(CrmLeadSource.objects.get(inbox=event).lead_id, lead.pk)
        self.assertEqual(CrmLead.objects.count(), 1)

    def test_old_owner_call_revision_does_not_transfer_authority_to_next_episode(self):
        first_owner = self.staff("synthetic-first-responsible")
        next_owner = self.staff("synthetic-next-responsible")
        old_call = self.participant_call(self.user, type="out"); self.deliver("call")
        old = CrmLead.objects.get(); old.assignee = first_owner; old.save()
        self.close(old)
        new_call = self.participant_call(self.user, type="out"); self.deliver("call")
        new = CrmLead.objects.exclude(pk=old.pk).get(); new.assignee = next_owner; new.save()
        self.history(type="out", callid=old_call.external_id, duration="43")
        revised = self.deliver("call")
        self.assertEqual(CrmLeadSource.objects.get(inbox=revised).lead_id, old.pk)
        for actor, allowed, hidden in ((first_owner, old_call, new_call), (next_owner, new_call, old_call)):
            self.assertTrue(can_read_call(actor, allowed))
            self.assertFalse(can_read_call(actor, hidden))

    def test_unmapped_calls_do_not_gain_staff_access_from_a_contact_match(self):
        staff = self.staff("synthetic-unmapped-reader")
        call = self.call(type="out"); self.deliver("call")
        lead = CrmLead.objects.get()
        self.assertIsNone(call.employee_id)
        self.assertFalse(can_read_call(staff, call))
        self.assertFalse(can_read_lead(staff, self.org, lead))
        self.assertIsNone(lead_identity(lead, user=staff).contact)

    def test_lead_call_history_cursor_preserves_source_and_contact_history(self):
        first = self.participant_call(self.user, person=self.person); self.deliver("call")
        lead = CrmLead.objects.get()
        calls = [first]
        for index in range(31):
            calls.append(PhoneCall.objects.create(organization=self.org, connection=self.telephony,
                client=self.person, employee=self.user, external_id=f"synthetic-page-{index}",
                phone_number=self.person.phone, direction="out", result="answered",
                started_at=first.started_at + timedelta(seconds=index + 1)))
        self.client.force_login(self.user)
        url = reverse("crm_lead_activity", args=[lead.pk])
        params = {"org_id": self.org.pk, "type": "call"}
        first_page = self.client.get(url, params)
        self.assertEqual(first_page.status_code, 200, first_page.content)
        page = first_page.json(); self.assertEqual(len(page["items"]), 30)
        self.assertTrue(page["cursor"])
        second_page = self.client.get(url, {**params, "cursor": page["cursor"]})
        self.assertEqual(second_page.status_code, 200, second_page.content)
        tail = second_page.json(); self.assertIsNone(tail["cursor"])
        rows = page["items"] + tail["items"]
        self.assertEqual(len(rows), 32)
        self.assertEqual({row["id"] for row in rows}, {f"call:{call.pk}" for call in calls})
        self.assertEqual([row["id"] for row in rows if row["relation"] == "lead_source"], [f"call:{first.pk}"])

    def test_projection_deduplicates_revisions_and_never_loads_message_bodies(self):
        call = self.participant_call(self.user); self.deliver("call")
        for seconds in range(1, 9):
            self.history(callid=call.external_id, duration=str(seconds))
            self.deliver("call")
        message = self.incoming(participant_phone=self.person.phone, body="Synthetic large body " * 1000)
        self.deliver()
        lead = CrmLead.objects.get()
        with CaptureQueriesContext(connection) as queries:
            projection = identity_projection([lead], self.user)
            self.assertEqual(lead_identity(lead, user=self.user, projection=projection).contact, self.person)
        self.assertEqual(len(projection["links"][lead.pk]), 2)
        self.assertIn("body", projection["messages"][message.pk].get_deferred_fields())
        sql_queries = [row["sql"].replace("`", '"').lower() for row in queries]
        message_reads = [sql for sql in sql_queries if 'from "pool_service_conversationmessage"' in sql]
        self.assertTrue(message_reads)
        self.assertFalse(any('"body"' in sql or '"raw_payload"' in sql for sql in message_reads))

    def test_unique_confirmed_company_representative_joins_call_and_message(self):
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic company")
        ClientCompanyLink.objects.create(person=self.person, company=company)
        self.participant_call(self.user, person=company); self.deliver("call")
        self.incoming(participant_phone=self.person.phone); self.deliver()
        lead = CrmLead.objects.get()
        self.assertEqual((lead.contact_id, lead.client_id), (self.person.pk, company.pk))
        projection = identity_projection([lead], self.user)
        identity = lead_identity(lead, user=self.user, projection=projection)
        self.assertEqual((identity.contact, identity.client), (self.person, company))

    def test_phone_match_does_not_invent_company_membership(self):
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic unrelated company")
        self.participant_call(self.user, person=company); self.deliver("call")
        identity = lead_identity(CrmLead.objects.get())
        self.assertEqual(identity.client, company)
        self.assertIsNone(identity.contact)
