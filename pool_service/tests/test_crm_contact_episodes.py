"""Contact episodes use synthetic saved communications, never live data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse

from pool_service.client_crm_models import ClientCompanyLink, ClientContact, ClientContactExclusion
from pool_service.communication_models import ConversationMessage, PhoneCall, CommunicationAccess
from pool_service.crm_commercial_models import CrmLead, CrmLeadSource, CrmLeadThread
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_inbox import capture_source, claim_one, deliver_claim
from pool_service.services.crm_lead_commands import execute_lead_command
from pool_service.services.crm_lead_intake import consume_incoming
from pool_service.services.crm_lead_identity import lead_identity
from pool_service.tests.test_crm_lead_intake import LeadIntakeFixture


class EpisodeFixture(LeadIntakeFixture):
    def setUp(self):
        super().setUp()
        self.megafon()
        # Episode/identity tests inspect even unassigned and unmapped calls as
        # the owner. Staff visibility is covered separately; an all-calls grant
        # no longer exposes owner or unidentified conversations after PR490.
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role="owner")
        self.person = Client.objects.create(organization=self.org, name="Synthetic caller", phone="+79001234567")
        self.grant.create_own_leads = self.grant.transition_own_leads = True
        self.grant.save()

    def call(self, key=None, person=None, **changes):
        key = key or uuid4().hex
        self.assertEqual(self.history(callid=key, **changes).status_code, 200)
        call = PhoneCall.objects.get(connection=self.telephony, external_id=key)
        if person is not None:
            call.client = person
            call.save(update_fields=["client"])
        return call

    def command(self, kind, changes, lead=None):
        return execute_lead_command(actor=self.user, organization=self.org, operation_key=uuid4().hex,
            command_type=kind, subject_id=lead.pk if lead else None,
            expected_revision=lead.revision if lead else None, changes=changes)

    def manual(self, person=None):
        return self.command("lead.create", {"title": "Synthetic manual title", "need": "Synthetic need", "client_id": (person or self.person).pk,
            "next_action": {"title": "Synthetic next action", "due_at": (self.now + timedelta(days=1)).isoformat()}})

    def close(self, lead, result="no_deal", archived=False):
        lead.status, lead.result, lead.reason = "closed", result, "Synthetic resolution"
        lead.is_archived = archived
        lead.needs_review = False
        lead.save()

    def activity(self, lead, kind="call"):
        self.client.force_login(self.user)
        response = self.client.get(reverse("crm_lead_activity", args=[lead.pk]), {"org_id": self.org.pk, "type": kind})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class ContactEpisodeTests(EpisodeFixture, TestCase):
    def test_contact_merge_refuses_converging_source_identities_without_saved_lead_fks(self):
        from pool_service.models import OrganizationAccess
        from pool_service.client_crm_models import ClientMergeRecord, ClientCRMProfile
        from pool_service.client_merge import merge_clients
        from pool_service.services.contact_management import preview_merge, execute_merge
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        second = Client.objects.create(organization=self.org, name="Synthetic distinct caller", phone="+79005550999")
        calls = []
        for person in (self.person, second):
            calls.append(self.call(person=person, phone=person.phone))
            self.deliver("call")
        for lead in CrmLead.objects.all():
            lead.client = lead.contact = None
            lead.save(update_fields=["client", "contact"])
        ClientCRMProfile.objects.update_or_create(client=self.person, defaults={
            "onec_ref": "11111111-2222-4333-8444-555555555555"})
        for directory in (True, False):
            with self.subTest(directory=directory), self.assertRaises(ValidationError if directory else ValueError):
                if directory:
                    preview = preview_merge(self.user, [self.person.pk, second.pk], self.person.pk)
                    execute_merge(self.user, preview["token"])
                else:
                    merge_clients(second.pk, self.person.pk, organization_id=self.org.pk, actor=self.user)
        self.assertEqual(CrmLead.objects.count(), 2)
        for call, person in zip(calls, (self.person, second)):
            call.refresh_from_db()
            self.assertEqual(call.client_id, person.pk)
        self.assertFalse(ClientMergeRecord.objects.exists())

    def test_distinct_calls_same_contact_and_normalized_unknown_phone_share_active_lead(self):
        for phone in ("+79001234567", "8 (900) 123-45-67", "9001234567"):
            self.call(phone=phone)
            self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 1)
        lead = CrmLead.objects.get()
        self.assertEqual(lead.contact_id, self.person.pk)
        self.assertEqual(CrmLeadSource.objects.count(), 3)
        for phone in ("+79005550011", "8 (900) 555-00-11"):
            call = self.call(phone=phone)
            call.client = None
            call.save(update_fields=["client"])
            self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 2)

    def test_new_calls_after_each_terminal_state_create_new_episode(self):
        self.call(); self.deliver("call")
        for result, archive in (("no_deal", False), ("lost", False), ("spam", False), ("no_deal", True)):
            with self.subTest(result=result, archived=archive):
                previous = CrmLead.objects.latest("pk")
                self.close(previous, result, archive)
                before = CrmLead.objects.filter(pk=previous.pk).values().get()
                self.call(); event = self.deliver("call")
                self.assertNotEqual(CrmLeadSource.objects.get(inbox=event).lead_id, previous.pk)
                self.assertEqual(CrmLead.objects.filter(pk=previous.pk).values().get(), before)
        self.assertEqual(CrmLead.objects.count(), 5)

    def test_archived_open_card_is_not_active(self):
        self.call(); self.deliver("call")
        previous = CrmLead.objects.get()
        previous.is_archived = True; previous.save()
        self.call(); self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 2)

    def test_revisions_remain_on_original_after_close_and_new_episode(self):
        call = self.call(status="missed"); first = self.deliver("call")
        old = CrmLeadSource.objects.get(inbox=first).lead
        self.close(old)
        self.call(); self.deliver("call")
        new = CrmLead.objects.exclude(pk=old.pk).get()
        pointer = CrmLeadThread.objects.get(lead=old)
        pointer.lead = new; pointer.save()  # Also covers explicit continuation.
        self.history(callid=call.external_id, status="success", duration="45")
        revised = self.deliver("call")
        self.assertEqual(CrmLeadSource.objects.get(inbox=revised).lead_id, old.pk)
        self.assertEqual(CrmLead.objects.count(), 2)
        old.refresh_from_db()
        self.assertFalse(old.needs_review)
        self.history(callid=call.external_id, status="success", duration="45")
        self.assertIsNone(claim_one(**self.scope("call")))

    def test_messages_after_close_keep_old_sources_and_replay(self):
        first = self.incoming(participant_phone=self.person.phone)
        self.deliver()
        old = CrmLead.objects.get()
        self.close(old)
        self.incoming(participant_phone=self.person.phone, external_message_id="second")
        second = self.deliver()
        new = CrmLeadSource.objects.get(inbox=second).lead
        self.assertNotEqual(old.pk, new.pk)
        self.incoming(participant_phone=self.person.phone)
        self.assertIsNone(claim_one(**self.scope()))
        self.assertEqual(CrmLeadSource.objects.get(inbox__source_id=first.pk, inbox__source_type="message").lead_id, old.pk)
        self.incoming(participant_phone=self.person.phone, external_message_id="third")
        self.deliver()
        self.assertEqual(CrmLead.objects.count(), 2)
        items = {row["id"]: row for row in self.activity(new, "message")}
        self.assertEqual(items["message:" + str(first.pk)]["relation"], "contact_history")
        self.assertEqual(items["message:" + str(second.source_id)]["relation"], "lead_source")

    def test_incoming_message_and_call_share_resolved_contact(self):
        self.call(); self.deliver("call")
        self.incoming(participant_phone=self.person.phone); self.deliver()
        self.assertEqual(CrmLead.objects.count(), 1)
        self.client.force_login(self.user)
        row = self.client.get(reverse("crm_live_lead_detail", args=[CrmLead.objects.get().pk]), {"org_id": self.org.pk}).json()["lead"]
        self.assertEqual(row["source"]["type"], "message")
        self.assertIn(self.person.name, row["title"])

    def test_coworkers_with_common_company_and_phone_remain_distinct(self):
        company = Client.objects.create(organization=self.org, name="Synthetic company", client_type="legal")
        coworker = Client.objects.create(organization=self.org, name="Synthetic coworker", phone=self.person.phone)
        for person in (self.person, coworker):
            ClientCompanyLink.objects.create(company=company, person=person)
            self.call(person=person); self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 2)
        self.assertEqual(set(CrmLead.objects.values_list("contact_id", flat=True)), {self.person.pk, coworker.pk})
        self.assertEqual(set(CrmLead.objects.values_list("client_id", flat=True)), {company.pk})
        self.call(person=self.person); self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 2)
        ambiguous = self.call()
        ambiguous.client = None; ambiguous.save(update_fields=["client"])
        self.deliver("call")
        lead = CrmLead.objects.latest("pk")
        self.assertIsNone(lead.contact_id)
        self.assertEqual(CrmLead.objects.count(), 3)
        first = CrmLead.objects.get(contact=self.person)
        rows = self.activity(first)
        coworker_sources = CrmLeadSource.objects.filter(lead__contact=coworker, inbox__source_type="call").values_list("inbox__source_id", flat=True)
        self.assertFalse({"call:" + str(pk) for pk in coworker_sources} & {row["id"] for row in rows})
        self.assertNotIn("call:" + str(ambiguous.pk), {row["id"] for row in rows})

    def test_foreign_clients_do_not_participate_in_phone_resolution(self):
        foreign = Organization.objects.create(name="Synthetic foreign organization")
        other = Client.objects.create(organization=foreign, name="Synthetic foreign caller", phone=self.person.phone)
        CrmLead.objects.create(organization=foreign, client=other, contact=other, title="Synthetic foreign lead")
        self.call(); self.deliver("call")
        self.assertEqual(CrmLead.objects.get(organization=self.org).contact_id, self.person.pk)
        self.assertEqual(CrmLead.objects.filter(organization=foreign).count(), 1)

    def test_outbound_creates_and_reuses_lead_with_current_visibility(self):
        outbound = self.call(type="out", person=self.person)
        self.assertEqual(self.deliver("call").error_code, "lead_created")
        inbound = self.call(person=self.person); self.deliver("call")
        lead = CrmLead.objects.get()
        unbound = self.call(type="out", phone="8 (900) 123-45-67")
        unbound.client = None; unbound.save(update_fields=["client"])
        self.deliver("call")
        rows = {row["id"]: row for row in self.activity(lead)}
        self.assertEqual(rows["call:" + str(inbound.pk)]["relation"], "lead_source")
        for call in (outbound, unbound):
            self.assertEqual(rows["call:" + str(call.pk)]["relation"], "lead_source")
        self.assertEqual(CrmLead.objects.count(), 1)
        CommunicationAccess.objects.filter(organization=self.org, user=self.user).update(can_view_all_calls=False, can_view_own_calls=False)
        self.assertEqual(self.client.get(reverse("crm_lead_activity", args=[lead.pk]), {"org_id": self.org.pk}).status_code, 404)

    def test_late_contact_binding_names_company_and_custom_title_are_current_on_read(self):
        self.person.phone = ""; self.person.save()
        call = self.call(status="missed")
        call.client = None; call.save(update_fields=["client"])
        self.deliver("call")
        lead = CrmLead.objects.get()
        self.assertIsNone(lead.client_id)
        call.client = self.person; call.save(update_fields=["client"])
        company = Client.objects.create(organization=self.org, name="Synthetic company", client_type="legal")
        ClientCompanyLink.objects.create(company=company, person=self.person)
        self.person.name = "Synthetic updated name"; self.person.save()
        before = CrmLead.objects.filter(pk=lead.pk).values().get()
        self.client.force_login(self.user)
        detail = self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk})
        self.assertEqual(detail.status_code, 200, detail.content)
        row = detail.json()["lead"]
        self.assertEqual(row["contact"]["name"], self.person.name)
        self.assertEqual(row["company"]["name"], company.name)
        self.assertIn(self.person.name, row["title"])
        self.assertIn(company.name, row["title"])
        board = self.client.get(reverse("crm_live_lead_data"), {"org_id": self.org.pk}).json()
        listed = next(item for item in board["leads"] if item["id"] == lead.pk)
        self.assertEqual(listed["title"], row["title"])
        self.assertEqual(listed["company"]["name"], company.name)
        self.assertEqual(CrmLead.objects.filter(pk=lead.pk).values().get(), before)
        lead.assignee = self.user; lead.status = "working"; lead.save()
        self.command("lead.edit", {"title": "Synthetic user's title"}, lead)
        self.person.name = "Another synthetic name"; self.person.save()
        row = self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk}).json()["lead"]
        self.assertEqual(row["title"], "Synthetic user's title")
        self.assertEqual(row["contact"]["name"], self.person.name)

    def test_manual_create_edit_restore_and_intake_share_active_identity_guard(self):
        manual = CrmLead.objects.get(pk=self.manual()["lead_id"])
        self.call(); self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 1)
        with self.assertRaises(ValidationError), transaction.atomic():
            self.manual()
        other = Client.objects.create(organization=self.org, name="Synthetic other caller")
        second = CrmLead.objects.get(pk=self.manual(other)["lead_id"])
        with self.assertRaises(ValidationError), transaction.atomic():
            self.command("lead.edit", {"client_id": self.person.pk}, second)
        manual.refresh_from_db(); self.close(manual)
        self.call(); self.deliver("call")
        with self.assertRaises(ValidationError), transaction.atomic():
            self.command("lead.disposition", {"column": "working", "reason": "Synthetic restore"}, manual)

    def test_existing_duplicates_are_not_merged_or_arbitrarily_selected(self):
        for _ in range(2):
            CrmLead.objects.create(organization=self.org, client=self.person, contact=self.person, title="Synthetic legacy duplicate")
        self.call()
        claim = claim_one(**self.scope("call"))
        self.assertEqual(deliver_claim(*claim, consumer=consume_incoming), "retry")
        self.assertEqual(CrmLead.objects.count(), 2)
        self.assertFalse(CrmLeadSource.objects.exists())

    def test_rebound_source_does_not_swallow_new_incoming_in_hidden_card(self):
        call = self.call(); self.deliver("call")
        old = CrmLead.objects.get()
        call.external_id = "synthetic-rebound-source"
        call.save(update_fields=["external_id"])
        self.call(); event = self.deliver("call")
        self.assertNotEqual(CrmLeadSource.objects.get(inbox=event).lead_id, old.pk)
        self.assertEqual(CrmLead.objects.count(), 2)

    def test_incoming_identity_rebinding_and_manual_choice_have_distinct_authority(self):
        call = self.call(); self.deliver("call")
        lead = CrmLead.objects.get()
        other = Client.objects.create(organization=self.org, name="Synthetic corrected contact")
        call.client = other; call.save(update_fields=["client"])
        self.assertEqual(lead_identity(lead, user=self.user).contact.pk, other.pk)
        lead.assignee = self.user; lead.status = "working"; lead.save()
        self.command("lead.edit", {"client_id": self.person.pk,
            "next_action": {"title": "Synthetic chosen contact", "due_at": (self.now + timedelta(days=1)).isoformat()}}, lead)
        lead.refresh_from_db()
        self.assertEqual(lead_identity(lead, user=self.user).contact.pk, self.person.pk)
        company = Client.objects.create(organization=self.org, name="Synthetic directory company", client_type="legal")
        ClientCompanyLink.objects.create(company=company, person=self.person)
        self.assertEqual(lead_identity(lead, user=self.user).client.pk, self.person.pk)

    def test_late_enrichment_does_not_break_current_task_client_binding(self):
        self.person.phone = ""; self.person.save()
        self.call(); self.deliver("call")
        lead = CrmLead.objects.get()
        self.command("lead.take", {"next_action": {"title": "Synthetic follow up", "due_at": (self.now + timedelta(days=1)).isoformat()}}, lead)
        lead.refresh_from_db()
        before = (lead.client_id, lead.next_task.client_id)
        call = PhoneCall.objects.get()
        call.client = self.person; call.save(update_fields=["client"])
        self.history(callid=call.external_id, duration="40")
        self.deliver("call")
        lead.refresh_from_db()
        self.assertEqual((lead.client_id, lead.next_task.client_id), before)
        self.assertEqual(lead_identity(lead, user=self.user).contact.pk, self.person.pk)

    def conversion_phone_version(self, lead):
        self.client.force_login(self.user)
        page = self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk})
        self.assertEqual(page.status_code, 200)
        return page.json()["lead"]["source_phone_version"]

    def test_unsaved_call_number_goes_to_selected_person_or_company_once(self):
        from pool_service.phone_utils import normalize_phone
        from pool_service.models import DataAuditLog

        company = Client.objects.create(organization=self.org, name="Synthetic company", client_type="legal")
        person = {"id": self.person.pk}
        legal = {"id": company.pk}
        original = self.person.phone
        foreign = Organization.objects.create(name="Synthetic separate directory")
        Client.objects.create(organization=foreign, name="Synthetic foreign person", phone="+79990001001")
        cases = [
            ({"contact": person, "company": legal}, self.person, "+79990001001", False),
            ({"contact": person}, self.person, "+79990001002", False),
            ({"company": legal}, company, "+79990001003", True),
        ]
        for customer, target, phone, primary in cases:
            with self.subTest(customer=customer):
                self.call(type="out", phone=phone)
                lead = CrmLeadSource.objects.get(inbox=self.deliver("call")).lead
                operation = dict(actor=self.user, organization=self.org, operation_key=uuid4().hex,
                    command_type="lead.complete_customer", subject_id=lead.pk, expected_revision=lead.revision,
                    changes={"customer": customer, "source_phone_version": self.conversion_phone_version(lead)})
                result = execute_lead_command(**operation)
                self.assertEqual(execute_lead_command(**operation), result)
                row = ClientContact.objects.get(match_value=normalize_phone(phone))
                self.assertEqual((row.client_id, row.is_primary), (target.pk, primary))
                self.assertTrue(DataAuditLog.objects.filter(client=target, actor=self.user, changed_fields=["phone"]).exists())
        self.person.refresh_from_db()
        company.refresh_from_db()
        self.assertEqual(self.person.phone, original)
        self.assertEqual(normalize_phone(company.phone), normalize_phone(cases[-1][2]))
        from pool_service.forms import ClientCreateForm
        form = ClientCreateForm({"client_type": "private", "first_name": "Synthetic",
            "last_name": "Person", "phone": original}, instance=self.person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        for _, target, phone, _ in cases:
            self.assertTrue(ClientContact.objects.filter(client=target, match_value=normalize_phone(phone)).exists())

    def test_saved_or_previously_removed_number_is_not_copied(self):
        from pool_service.phone_utils import normalize_phone
        company = Client.objects.create(organization=self.org, name="Synthetic company", client_type="legal")
        legacy = "+79990001004"
        removed = "+79990001005"
        ClientContact.objects.create(client=self.person, kind="phone", value="8 (999)\t000-10-04")
        ClientContactExclusion.objects.create(client=company, kind="phone", match_value=normalize_phone(removed))
        for phone in (self.person.phone, legacy, removed):
            with self.subTest(phone=phone):
                self.call(phone=phone)
                lead = CrmLeadSource.objects.get(inbox=self.deliver("call")).lead
                self.command("lead.complete_customer", {"customer": {"company": {"id": company.pk}},
                    "source_phone_version": self.conversion_phone_version(lead)}, lead)
        company.refresh_from_db()
        self.assertFalse(company.phone)
        self.assertFalse(ClientContact.objects.filter(client=company, kind="phone").exists())
        self.assertTrue(ClientContactExclusion.objects.filter(client=company, kind="phone").exists())

    def test_deal_conversion_only_appends_to_one_unambiguous_recipient(self):
        from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
        from pool_service.crm_commercial_models import CrmPipeline
        bootstrap_commercial_settings(self.org.pk)
        pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.grant.qualify_own_leads = self.grant.create_own_deals = self.grant.manage_own_next_action = True
        self.grant.view_team_deals = True
        self.grant.save()
        company = Client.objects.create(organization=self.org, name="Synthetic sales company", client_type="legal")
        person = {"contact": {"id": self.person.pk}, "company": {"id": company.pk}}
        legal = {"company": {"id": company.pk}}
        for number, choices, recipient in (
                ("9990001006", [person], self.person),
                ("9990001007", [legal, legal], company),
                ("9990001008", [legal, person], None)):
            with self.subTest(number=number):
                self.call(phone="+7" + number)
                lead = CrmLeadSource.objects.get(inbox=self.deliver("call")).lead
                deals = [{"pipeline_id": pipeline.pk, "title": "Synthetic conversion", "customer": customer,
                    "next_action": {"title": "Synthetic step", "due_at": (self.now + timedelta(days=1)).isoformat()}}
                    for customer in choices]
                self.command("lead.qualify", {"need": "Synthetic need", "deals": deals,
                    "source_phone_version": self.conversion_phone_version(lead)}, lead)
                phones = ClientContact.objects.filter(kind="phone", match_value=number)
                self.assertEqual(list(phones.values_list("client_id", flat=True)), [recipient.pk] if recipient else [])

    def test_changed_source_phone_requires_refresh_without_writes(self):
        from pool_service.services.crm_commercial_commands import CommercialConflict
        call = self.call(phone="+79990001010")
        lead = CrmLeadSource.objects.get(inbox=self.deliver("call")).lead
        changes = {"customer": {"contact": {"id": self.person.pk}},
            "source_phone_version": self.conversion_phone_version(lead)}
        call.phone_number = "+79990001011"
        call.save(update_fields=["phone_number"])
        with self.assertRaises(CommercialConflict):
            self.command("lead.complete_customer", changes, lead)
        lead.refresh_from_db()
        self.assertEqual(lead.status, "new")
        self.assertFalse(ClientContact.objects.filter(client=self.person).exists())

    def test_directory_edit_permission_is_required_to_append(self):
        from pool_service.services.crm_conversion_contacts import save_unsaved_lead_phone
        original = self.person.phone
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role="installer")
        save_unsaved_lead_phone("default", self.user, self.person, "+79990001009")
        self.person.refresh_from_db()
        self.assertEqual(self.person.phone, original)
        self.assertFalse(ClientContact.objects.filter(match_value="9990001009").exists())

    def test_customer_completion_and_conversion_preserve_confirmed_identity(self):
        from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
        from pool_service.crm_commercial_models import CrmPipeline
        bootstrap_commercial_settings(self.org.pk)
        pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.grant.qualify_own_leads = self.grant.create_own_deals = self.grant.manage_own_next_action = True
        self.grant.view_team_deals = True
        self.grant.save()
        other = Client.objects.create(organization=self.org, name="Synthetic confirmed customer")
        for command in ("lead.complete_customer", "lead.qualify"):
            with self.subTest(command=command):
                self.call(); self.deliver("call")
                lead = CrmLead.objects.filter(status="new").get()
                customer = {"contact": {"id": other.pk}}
                values = {"customer": customer} if command == "lead.complete_customer" else {"need": "Synthetic need",
                    "deals": [{"pipeline_id": pipeline.pk, "title": "Synthetic conversion", "customer": customer,
                        "next_action": {"title": "Synthetic next step", "due_at": (self.now + timedelta(days=1)).isoformat()}}]}
                self.command(command, values, lead)
                lead.refresh_from_db()
                self.assertEqual(lead.identity_origin, "manual")
                self.assertEqual(lead_identity(lead, user=self.user).contact.pk, other.pk)

    def test_need_only_form_edit_keeps_automatic_identity_and_title(self):
        self.call(); self.deliver("call")
        lead = CrmLead.objects.get()
        lead.assignee = self.user; lead.status = "working"; lead.save()
        # The actual form's leadEditChanges strips unchanged projections;
        # the corresponding Node test covers the full form values.
        self.command("lead.edit", {"need": "Synthetic revised context"}, lead)
        lead.refresh_from_db()
        self.assertEqual(lead.identity_origin, "incoming")
        self.assertFalse(lead.title_is_manual)
        self.person.name = "Synthetic renamed after edit"; self.person.save()
        self.client.force_login(self.user)
        row = self.client.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk}).json()["lead"]
        self.assertIn(self.person.name, row["title"])

    def test_legacy_explicit_person_only_binding_is_preserved(self):
        company = Client.objects.create(organization=self.org, name="Synthetic legacy company", client_type="legal")
        ClientCompanyLink.objects.create(company=company, person=self.person)
        lead = CrmLead.objects.create(organization=self.org, client=self.person, contact=self.person,
            acquisition_source="manual", title="Synthetic existing manual lead")
        self.assertEqual(lead.identity_origin, "legacy")
        self.assertEqual(lead_identity(lead, user=self.user).client.pk, self.person.pk)

    def test_unbound_whitespace_aliases_group_and_keep_private_calls_hidden(self):
        from pool_service.services.crm_lead_access import can_read_lead
        from pool_service.call_processing_models import CallPrivateNumber
        for phone in ("\u00a0+7 (900) 555-00-11\u00a0", "\v+79005550011\f", "89005550011"):
            call = self.call(phone="+79005550011")
            call.phone_number, call.client, call.employee = phone, None, self.user
            call.save(update_fields=["phone_number", "client", "employee"])
            self.deliver("call")
        self.assertEqual(CrmLead.objects.count(), 1)
        lead = CrmLead.objects.get()
        self.assertTrue(can_read_lead(self.user, self.org, lead))
        self.assertEqual(len(self.activity(lead)), 3)
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, phone_key="9005550011")
        self.assertFalse(can_read_lead(self.user, self.org, lead))


@skipUnlessDBFeature("has_select_for_update")
@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class ContactEpisodeRaceTests(EpisodeFixture, TransactionTestCase):
    def race(self, *operations):
        barrier = Barrier(len(operations))
        def run(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return operation()
            except ValidationError:
                return "conflict"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            return list(executor.map(run, operations))

    def test_parallel_new_calls_create_one_active_lead(self):
        for _ in range(2):
            self.call(person=self.person)
        claims = [claim_one(**self.scope("call")) for _ in range(2)]
        outcomes = self.race(*(lambda claim=claim: deliver_claim(*claim, consumer=consume_incoming) for claim in claims))
        self.assertEqual(outcomes, ["done", "done"])
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(CrmLeadSource.objects.count(), 2)

    def test_contact_merge_racing_intake_keeps_unique_final_episode(self):
        from pool_service.models import OrganizationAccess
        from pool_service.client_crm_models import ClientMergeRecord
        from pool_service.services.contact_management import preview_merge, execute_merge
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        self.manual()
        second = Client.objects.create(organization=self.org, name="Synthetic merging caller", phone="+79005550998")
        self.call(person=second, phone=second.phone)
        claim = claim_one(**self.scope("call"))
        preview = preview_merge(self.user, [self.person.pk, second.pk], self.person.pk)
        outcomes = self.race(lambda: execute_merge(self.user, preview["token"]),
            lambda: deliver_claim(*claim, consumer=consume_incoming))
        self.assertEqual(outcomes[1], "done")
        identities = [lead_identity(lead).key for lead in CrmLead.objects.all()]
        self.assertEqual(len(identities), len({tuple(key) for key in identities}))
        self.assertEqual(CrmLead.objects.count(), 1 if ClientMergeRecord.objects.exists() else 2)

    def test_parallel_outbound_and_message_create_one_active_lead(self):
        self.call(type="out", person=self.person)
        self.incoming(participant_phone=self.person.phone)
        claims = [claim_one(**self.scope(kind)) for kind in ("call", "message")]
        outcomes = self.race(*(lambda claim=claim: deliver_claim(*claim, consumer=consume_incoming) for claim in claims))
        self.assertEqual(outcomes, ["done", "done"])
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(CrmLeadSource.objects.count(), 2)

    def test_owner_mark_change_racing_intake_serializes(self):
        from pool_service.models import OrganizationAccess
        from pool_service.services.owner_call_privacy import contact_state, set_personal_peer
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        revision = contact_state(self.user, self.person)["revision"]
        call = self.call(type="out", person=self.person)
        call.employee = self.user; call.save(update_fields=["employee"])
        claim = claim_one(**self.scope("call"))
        outcomes = self.race(lambda: set_personal_peer(self.user, self.person.pk, "1", revision),
            lambda: deliver_claim(*claim, consumer=consume_incoming))
        self.assertEqual(outcomes[1], "done")
        self.assertTrue(contact_state(self.user, self.person)["marked"])
        event = CrmInboxEvent.objects.get(pk=claim[0])
        self.assertIn(event.error_code, ("lead_created", "private_ignored"))
        self.assertEqual(CrmLead.objects.count(), int(event.error_code == "lead_created"))
        before = CrmLead.objects.count()
        call = self.call(type="out", person=self.person)
        call.employee = self.user; call.save(update_fields=["employee"])
        self.assertEqual(self.deliver("call").error_code, "private_ignored")
        self.assertEqual(CrmLead.objects.count(), before)

    def test_manual_create_racing_intake_cannot_create_duplicate(self):
        self.call(person=self.person)
        claim = claim_one(**self.scope("call"))
        outcomes = self.race(lambda: self.manual(), lambda: deliver_claim(*claim, consumer=consume_incoming))
        self.assertEqual(outcomes[1], "done")
        self.assertEqual(CrmLead.objects.count(), 1)

    def test_manual_close_racing_new_incoming_keeps_completed_episode_closed(self):
        lead = CrmLead.objects.get(pk=self.manual()["lead_id"])
        self.call(person=self.person)
        claim = claim_one(**self.scope("call"))
        from pool_service.services.crm_commercial_commands import CommercialConflict
        def close():
            try:
                return self.command("lead.transition", {"status": "closed", "result": "no_deal", "reason": "Synthetic close"}, lead)
            except CommercialConflict:
                return "stale"
        outcomes = self.race(close, lambda: deliver_claim(*claim, consumer=consume_incoming))
        self.assertEqual(outcomes[1], "done")
        self.assertLessEqual(CrmLead.objects.filter(is_archived=False, result="").exclude(status="closed").count(), 1)
        lead.refresh_from_db()
        if outcomes[0] != "stale":
            self.assertEqual(lead.status, "closed")

    def test_manual_identity_edit_racing_intake_cannot_create_duplicate(self):
        other = Client.objects.create(organization=self.org, name="Synthetic previous contact")
        lead = CrmLead.objects.get(pk=self.manual(other)["lead_id"])
        self.call(person=self.person)
        claim = claim_one(**self.scope("call"))
        outcomes = self.race(lambda: self.command("lead.edit", {"client_id": self.person.pk}, lead),
            lambda: deliver_claim(*claim, consumer=consume_incoming))
        self.assertEqual(outcomes[1], "done")
        self.assertEqual(CrmLead.objects.filter(contact=self.person, result="", is_archived=False).exclude(status="closed").count(), 1)
