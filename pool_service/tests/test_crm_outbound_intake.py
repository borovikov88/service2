"""Synthetic outbound intake and unchanged existing privacy guards; no provider requests."""
from datetime import timedelta

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.client_crm_models import ClientBusinessRole, ClientContact
from pool_service.communication_models import PhoneCall
from pool_service.crm_commercial_models import CrmLead, CrmLeadSource
from pool_service.crm_inbox_models import CrmInboxBoundary, CrmInboxEvent
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.services.crm_inbox import capture_source, claim_one, reconcile
from pool_service.services.crm_lead_access import can_read_lead
from pool_service.tests.test_crm_contact_episodes import EpisodeFixture


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class OutboundIntakeTests(EpisodeFixture, TestCase):
    def mark(self, enabled=True):
        from pool_service.services.owner_call_privacy import contact_state, set_personal_peer
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role="owner")
        state = contact_state(self.user, self.person)
        return set_personal_peer(self.user, self.person.pk, "1" if enabled else "0", state["revision"])

    def participant(self, call, user):
        call.employee = user
        call.save(update_fields=["employee"])
        return call

    def coworker(self):
        from django.contrib.auth import get_user_model
        from pool_service.models import Employee
        user = get_user_model().objects.create_user("synthetic-business-coworker")
        OrganizationAccess.objects.create(organization=self.org, user=user, role="manager")
        Employee.objects.create(organization=self.org, user=user, display_name="Synthetic coworker")
        return user

    def test_outbound_in_each_active_status_reuses_card_and_after_close_creates(self):
        call = self.call(type="out"); self.deliver("call")
        lead = CrmLead.objects.get()
        self.assertTrue(can_read_lead(self.user, self.org, lead))
        for status in ("new", "working", "qualifying", "waiting", "deferred"):
            lead.follow_up_at = self.now + timedelta(days=1)
            lead.reason = "Synthetic waiting reason"
            lead.status = status; lead.save()
            self.call(type="out"); event = self.deliver("call")
            self.assertEqual(CrmLeadSource.objects.get(inbox=event).lead_id, lead.pk)
        self.close(lead)
        self.call(type="out"); self.deliver("call")
        self.history(type="out", callid=call.external_id, duration="42")
        event = self.deliver("call")
        self.assertEqual(CrmLeadSource.objects.get(inbox=event).lead_id, lead.pk)
        self.assertEqual(CrmLead.objects.count(), 2)
        self.history(type="out", callid=call.external_id, duration="42")
        self.assertIsNone(claim_one(**self.scope("call")))

    def test_outbound_first_call_projects_current_contact_and_direction(self):
        self.call(type="out"); self.deliver("call")
        lead = CrmLead.objects.get()
        self.person.name = "Synthetic updated outbound contact"; self.person.save()
        self.client.force_login(self.user)
        row = self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk}).json()["lead"]
        self.assertEqual(row["title"], "Исходящий звонок · " + self.person.name)
        self.assertEqual(row["contact"]["id"], self.person.pk)
        self.assertEqual(row["source"]["direction"], "out")

    def test_outbound_history_of_distinct_employees_survives_late_contact_creation(self):
        from pool_service.client_crm_models import ClientCompanyLink
        coworker = self.coworker()
        for index, first_direction in enumerate(("in", "out")):
            with self.subTest(first_direction=first_direction):
                phone = "+7900555112" + str(index)
                first = self.participant(self.call(type=first_direction, phone=phone), self.user)
                self.assertIsNone(first.client_id)
                self.assertEqual(self.deliver("call").error_code, "lead_created")
                lead = CrmLead.objects.latest("pk")
                outbound = self.participant(self.call(type="out", phone=phone), coworker)
                self.assertEqual(self.deliver("call").error_code, "lead_linked")
                person = Client.objects.create(organization=self.org, phone=phone, name="Synthetic late contact " + str(index))
                company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic late company " + str(index))
                ClientCompanyLink.objects.create(person=person, company=company)
                self.client.force_login(self.user)
                detail = self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk}).json()["lead"]
                self.assertEqual(detail["contact"]["id"], person.pk)
                self.assertEqual(detail["company"]["id"], company.pk)
                self.assertIn(person.name, detail["title"])
                rows = {row["id"]: row for row in self.activity(lead)}
                for call in (first, outbound):
                    self.assertEqual(rows["call:" + str(call.pk)]["relation"], "lead_source")
                self.assertIn("Исходящий", rows["call:" + str(outbound.pk)]["title"])
                person.name = "Synthetic renamed contact " + str(index); person.save()
                company.name = "Synthetic renamed company " + str(index); company.save()
                board = self.client.get(reverse("crm_live_lead_data"), {"org_id": self.org.pk}).json()["leads"]
                row = next(row for row in board if row["id"] == lead.pk)
                self.assertIn(person.name, row["title"])
                self.assertIn(company.name, row["title"])
                self.assertEqual(CrmLead.objects.count(), index + 1)

    def test_outbound_after_lost_or_archive_starts_new_episode(self):
        self.call(type="out"); self.deliver("call")
        for result, archived in (("lost", False), ("no_deal", True)):
            lead = CrmLead.objects.latest("pk")
            self.close(lead, result, archived)
            before = CrmLead.objects.filter(pk=lead.pk).values().get()
            self.call(type="out"); event = self.deliver("call")
            self.assertNotEqual(CrmLeadSource.objects.get(inbox=event).lead_id, lead.pk)
            self.assertEqual(CrmLead.objects.filter(pk=lead.pk).values().get(), before)

    def test_reconcile_repairs_new_outbound_but_rollout_backlog_stays_historical(self):
        boundary = CrmInboxBoundary.objects.get(key="outbound_calls")
        for key, before in (("old", True), ("new", False)):
            call = self.call(key=key, type="out")
            CrmInboxEvent.objects.filter(source_id=call.pk, source_type="call")._raw_delete("default")
            if before:
                call.intake_received_at = boundary.captured_at - timedelta(seconds=1)
                call.save(update_fields=["intake_received_at"])
            reconcile(**self.scope("call"))
            event = self.deliver("call")
            self.assertEqual(event.error_code, "historical_ignored" if before else "lead_created")
            if before:
                self.history(type="out", callid=key, duration="37")
                self.assertEqual(self.deliver("call").error_code, "historical_ignored")
                self.assertFalse(CrmLead.objects.exists())

    def test_owner_personal_incoming_and_outgoing_never_create_lead(self):
        self.mark()
        for direction in ("in", "out"):
            self.participant(self.call(type=direction), self.user)
            self.assertEqual(self.deliver("call").error_code, "private_ignored")
        self.assertFalse(CrmLead.objects.exists())
        self.assertFalse(CrmLeadSource.objects.exists())

    def test_coworker_calls_to_same_person_remain_business_in_both_directions(self):
        self.mark()
        ClientBusinessRole.objects.create(client=self.person, role="personal", active=True)
        coworker = self.coworker()
        for direction in ("in", "out"):
            self.participant(self.call(type=direction), coworker)
            self.assertIn(self.deliver("call").error_code, ("lead_created", "lead_linked"))
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(CrmLeadSource.objects.count(), 2)
        self.assertTrue(can_read_lead(self.user, self.org, CrmLead.objects.get()))


    def test_normal_mapping_updates_visibility_without_new_webhook(self):
        from pool_service.communication_models import TelephonyEmployeeIdentity
        from pool_service.models import Employee
        from pool_service.services.employee_identity_sync import map_telephony_identity
        self.mark()
        coworker = self.coworker()
        for index, participant in enumerate((self.user, coworker)):
            call = self.call(type="out", ext=str(800 + index), user="synthetic-unmapped-" + str(index))
            self.assertIsNone(call.employee_id)
            self.deliver("call")
            lead = CrmLeadSource.objects.get(inbox__source_id=call.pk, inbox__source_type="call").lead
            event_count = CrmInboxEvent.objects.count()
            identity = TelephonyEmployeeIdentity.objects.get(connection=self.telephony, extension=str(800 + index))
            employee = Employee.objects.get(user=participant, organization=self.org)
            map_telephony_identity(identity, employee, self.user)
            call.refresh_from_db()
            self.assertEqual(call.employee_id, participant.pk)
            self.assertEqual(CrmInboxEvent.objects.count(), event_count)
            self.assertEqual(can_read_lead(self.user, self.org, lead), participant == coworker)
        self.assertEqual(CrmLeadSource.objects.count(), 2)



    def test_mark_after_creation_hides_owner_source_without_deleting_working_history(self):
        self.participant(self.call(), self.user); self.deliver("call")
        old = CrmLead.objects.get()
        self.mark()
        self.assertFalse(can_read_lead(self.user, self.org, old))
        self.participant(self.call(type="out"), self.user)
        self.assertEqual(self.deliver("call").error_code, "private_ignored")
        self.assertEqual(CrmLeadSource.objects.filter(lead=old).count(), 1)
        self.participant(self.call(type="out"), self.coworker())
        self.assertEqual(self.deliver("call").error_code, "lead_created")
        current = CrmLead.objects.exclude(pk=old.pk).get()
        self.assertTrue(can_read_lead(self.user, self.org, current))
        self.assertEqual(len(self.activity(current)), 1)

    def test_personal_contact_category_and_phone_type_do_not_suppress_corporate_messages(self):
        self.mark()
        ClientBusinessRole.objects.create(client=self.person, role="personal", active=True)
        ClientContact.objects.create(client=self.person, kind="phone", value=self.person.phone, phone_type="personal")
        self.incoming(participant_phone=self.person.phone)
        self.assertEqual(self.deliver().error_code, "lead_created")
        self.incoming(participant_phone=self.person.phone, external_message_id="second-work-message")
        self.assertEqual(self.deliver().error_code, "lead_linked")
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(CrmLeadSource.objects.count(), 2)

    def test_personal_category_edit_does_not_change_call_or_message_intake(self):
        ClientBusinessRole.objects.create(client=self.person, role="personal", active=True)
        self.call(type="out"); self.deliver("call")
        self.incoming(participant_phone=self.person.phone); self.deliver()
        ClientBusinessRole.objects.filter(client=self.person).update(active=False)
        self.call(type="in"); self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(CrmLeadSource.objects.count(), 3)

    def test_other_owner_and_foreign_organization_lists_do_not_apply(self):
        from pool_service.phone_utils import normalize_phone
        coworker = self.coworker()
        CallPrivateNumber.objects.create(organization=self.org, owner=coworker, phone_key=normalize_phone(self.person.phone))
        foreign = Organization.objects.create(name="Synthetic private foreign scope")
        CallPrivateNumber.objects.create(organization=foreign, owner=self.user, phone_key=normalize_phone(self.person.phone))
        self.participant(self.call(type="out"), self.user)
        self.assertEqual(self.deliver("call").error_code, "lead_created")
        self.assertTrue(can_read_lead(self.user, self.org, CrmLead.objects.get()))

    def test_no_outbound_boundary_fails_closed_without_lead(self):
        CrmInboxBoundary.objects.filter(key="outbound_calls").delete()
        self.call(type="out")
        self.assertEqual(self.deliver("call").error_code, "historical_ignored")
        self.assertFalse(CrmLead.objects.exists())

    def test_board_projection_batches_contact_history_queries(self):
        self.client.force_login(self.user)
        self.call(type="out"); self.deliver("call")
        url = reverse("crm_live_lead_data") + "?org_id=" + str(self.org.pk) + "&layout=board"
        with CaptureQueriesContext(connection) as one:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
        for index in range(5):
            person = Client.objects.create(organization=self.org, name="Synthetic board contact " + str(index),
                phone="+7900555100" + str(index))
            self.call(type="out", person=person, phone=person.phone); self.deliver("call")
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
        print(f"Synthetic board queries: 1 card={len(one)}, 6 cards={len(many)}")
        self.assertLessEqual(len(many), len(one) + 2, "Source identity/title must be batched for the board")
