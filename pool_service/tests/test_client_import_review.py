"""Synthetic, local-only regression/security tests for non-destructive 1C review."""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as Browser, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_import import apply_candidate, scan_onec_clients
from pool_service.client_crm_models import ClientCRMProfile, ClientImportCandidate, ClientImportProposal, ClientContact, ClientCompanyLink
from pool_service.client_crm_sync import _stage_recent_candidate, sync_all_onec_clients
from pool_service.client_import_review import decide, token
from pool_service.models import Client, Organization, OrganizationAccess


class ReviewFixture:
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Synthetic review tenant", trial_started_at=timezone.now())
        self.actor = get_user_model().objects.create_user(username="synthetic-review-owner")
        OrganizationAccess.objects.create(user=self.actor, organization=self.org, role="owner")
        self.customer = Client.objects.create(organization=self.org, client_type="private", name="Synthetic Old", phone="+12025550101", email="old@example.test")
        self.profile = ClientCRMProfile.objects.create(client=self.customer, onec_ref="11111111-2222-3333-4444-555555555555", source="onec")
        self.candidate = ClientImportCandidate.objects.create(organization=self.org, source_ref=self.profile.onec_ref,
            name="Synthetic New", source_kind="private", phone="+12025550102", email="new@example.test",
            status="imported", matched_client=self.customer, applied_at=timezone.now())
        self.setting = override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.org.pk))
        self.setting.enable()
        self.addCleanup(self.setting.disable)

    def proposal(self, field="phone", client=None):
        return ClientImportProposal.objects.filter(candidate=self.candidate, client=client or self.customer, field=field).latest("pk")

    def decision(self, row, decision="accepted", actor=None):
        return decide(actor=actor or self.actor, organization_id=self.org.pk, proposal_id=row.pk,
            decision=decision, expected_token=token(row))

    def update_source(self, **values):
        ClientImportCandidate.objects.filter(pk=self.candidate.pk).update(**values)
        self.candidate.refresh_from_db()
        apply_candidate(self.candidate)


class OnecReviewTests(ReviewFixture, TestCase):
    def test_existing_fields_fill_only_empty_and_blank_source_never_erases(self):
        self.profile.middle_name = "Saved middle"
        self.profile.birth_date = date(1990, 1, 1)
        self.profile.save()
        self.update_source(birth_date=date(1991, 2, 2), inn="SYNTHETIC-INN")
        self.customer.refresh_from_db(); self.profile.refresh_from_db()
        self.assertEqual(self.customer.phone, "+12025550101")
        self.assertEqual(self.customer.inn, "SYNTHETIC-INN")
        self.assertEqual(self.profile.birth_date, date(1990, 1, 1))
        self.assertTrue(ClientImportProposal.objects.filter(client=self.customer, field="birth_date").exists())
        self.update_source(phone="", email="", name="", inn="", birth_date=None, payload={})
        self.customer.refresh_from_db(); self.profile.refresh_from_db()
        self.assertEqual((self.customer.phone, self.customer.email, self.customer.name, self.customer.inn),
            ("+12025550101", "old@example.test", "Synthetic Old", "SYNTHETIC-INN"))
        self.assertEqual(self.profile.birth_date, date(1990, 1, 1))

    def test_reject_survives_repeat_unrelated_change_and_manual_clear(self):
        apply_candidate(self.candidate)
        row = self.proposal()
        self.decision(row, "rejected")
        self.update_source(email="third@example.test")
        self.assertEqual(ClientImportProposal.objects.filter(client=self.customer, field="phone").count(), 1)
        Client.objects.filter(pk=self.customer.pk).update(phone="")
        apply_candidate(self.candidate)
        self.customer.refresh_from_db(); self.assertEqual(self.customer.phone, "")
        self.update_source(phone="+12025550103")
        self.customer.refresh_from_db(); self.assertEqual(self.customer.phone, "+12025550103")

    def test_new_source_version_including_absence_does_not_resurrect_old_decision(self):
        apply_candidate(self.candidate)
        old = self.proposal(); self.decision(old, "rejected")
        self.update_source(phone="")
        self.update_source(phone="+12025550102")
        new = self.proposal()
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(new.source_version, 3)
        self.assertEqual(new.state, "pending")

    def test_accept_repeat_and_conflicting_decision_do_not_rewrite_manual_edit(self):
        apply_candidate(self.candidate); row = self.proposal()
        self.decision(row)
        Client.objects.filter(pk=self.customer.pk).update(phone="+12025550199")
        self.decision(row)
        with self.assertRaises(ValidationError): self.decision(row, "rejected")
        self.customer.refresh_from_db(); self.assertEqual(self.customer.phone, "+12025550199")
        self.assertTrue(ClientContact.objects.filter(client=self.customer, value="+12025550101").exists())
        self.assertEqual(ClientContact.objects.filter(client=self.customer, is_primary=True).count(), 1)

    def test_stale_manual_edit_requires_explicit_new_comparison_and_old_token_fails(self):
        apply_candidate(self.candidate); row = self.proposal()
        Client.objects.filter(pk=self.customer.pk).update(phone="+12025550188")
        self.assertEqual(self.decision(row).state, "stale")
        refreshed = self.decision(row, "refresh")
        self.assertEqual(refreshed.before, "+12025550188")
        with self.assertRaises(ValidationError): self.decision(row)
        self.decision(refreshed)
        self.customer.refresh_from_db(); self.assertEqual(self.customer.phone, refreshed.after)

    def test_staged_source_changes_block_acceptance_before_apply(self):
        apply_candidate(self.candidate); row = self.proposal()
        ClientImportCandidate.objects.filter(pk=self.candidate.pk).update(phone="+12025550177")
        self.assertEqual(self.decision(row).state, "stale")
        with self.assertRaises(ValidationError): self.decision(row, "refresh")

    def test_email_case_only_source_change_does_not_stale_same_version(self):
        apply_candidate(self.candidate); row = self.proposal("email")
        self.update_source(email="NEW@EXAMPLE.TEST")
        self.assertEqual(self.proposal("email").pk, row.pk)
        self.assertEqual(self.decision(row).state, "accepted")

    def test_contact_additions_review_preserve_existing_sources_and_absence(self):
        old = ClientContact.objects.create(client=self.customer, kind="phone", value="+12025550101", match_value="12025550101",
            label="Manual", sources=["manual", "onec"], source_reference=self.candidate.source_ref, is_primary=True)
        self.update_source(payload={"phones": [{"value": "+12025550103", "label": "Source"}]})
        field = ClientImportProposal.objects.filter(client=self.customer, field__startswith="contact:phone:").get()
        self.assertEqual(ClientContact.objects.filter(client=self.customer).count(), 1)
        self.decision(field)
        self.update_source(payload={}, phone="")
        old.refresh_from_db()
        self.assertEqual((old.label, old.sources, old.source_reference), ("Manual", ["manual", "onec"], self.candidate.source_ref))
        self.assertEqual(ClientContact.objects.filter(client=self.customer).count(), 2)

    def test_foreign_org_changed_identity_and_merged_targets_fail_closed(self):
        apply_candidate(self.candidate); row = self.proposal()
        foreign = Organization.objects.create(name="Synthetic other")
        outsider = get_user_model().objects.create_user(username="synthetic-outsider")
        OrganizationAccess.objects.create(user=outsider, organization=foreign, role="owner")
        with self.assertRaises(PermissionDenied): self.decision(row, actor=outsider)
        ClientCRMProfile.objects.filter(pk=self.profile.pk).update(onec_ref="66666666-2222-3333-4444-555555555555")
        self.assertEqual(self.decision(row).state, "stale")
        with self.assertRaises(ValueError): apply_candidate(self.candidate)
        ClientCRMProfile.objects.filter(pk=self.profile.pk).update(onec_ref=self.candidate.source_ref, merged_into=self.customer)
        with self.assertRaises(ValueError): apply_candidate(self.candidate)

    def test_single_shared_phone_is_not_identity(self):
        ClientCRMProfile.objects.filter(pk=self.profile.pk).update(onec_ref=None)
        row = {"Ref_Key": "aaaaaaaa-2222-3333-4444-555555555555", "Description": "Different Person",
            "ВидКонтрагента": "ФизическоеЛицо", "НомерТелефонаДляПоиска": self.customer.phone}
        staged = _stage_recent_candidate(self.org, row, [])
        self.assertEqual(staged.status, "review"); self.assertIsNone(staged.matched_client_id)
        with self.assertRaises(ValueError): apply_candidate(staged)

    def test_source_absent_from_full_scan_is_not_refreshed(self):
        apply_candidate(self.candidate)
        with patch("pool_service.client_crm_import.config_from_settings"), patch("pool_service.client_crm_import.fetch_metadata"), \
                patch("pool_service.client_crm_import._buyer_rows", return_value=[]), patch("pool_service.client_crm_import._contact_rows", return_value={}):
            result = sync_all_onec_clients()
        self.assertEqual(result["refreshed"], 0)
        self.candidate.refresh_from_db(); self.assertFalse(self.candidate.source_available)
        self.assertFalse(ClientImportProposal.objects.filter(candidate=self.candidate, state="pending").exists())

    def test_http_csrf_permissions_methods_and_escaped_ui(self):
        apply_candidate(self.candidate)
        strict = Browser(enforce_csrf_checks=True); strict.force_login(self.actor)
        url = reverse("client_onec_proposals")
        page = strict.get(url)
        self.assertContains(page, "Предложения изменений")
        self.assertContains(page, 'name="csrfmiddlewaretoken"')
        row = self.proposal()
        data = {"proposal_id": row.pk, "decision": "accepted", "token": token(row)}
        self.assertEqual(strict.post(url, data).status_code, 403)
        self.assertEqual(strict.put(url, data, HTTP_X_CSRFTOKEN=strict.cookies["csrftoken"].value).status_code, 405)
        strict.get(url, data)
        row.refresh_from_db(); self.assertEqual(row.state, "pending")
        data["csrfmiddlewaretoken"] = strict.cookies["csrftoken"].value
        self.assertEqual(strict.post(url, data).status_code, 302)
        user = get_user_model().objects.create_user(username="synthetic-reader")
        OrganizationAccess.objects.create(user=user, organization=self.org, role="manager")
        strict.force_login(user); self.assertEqual(strict.get(url).status_code, 403)
        self.candidate.name = '<script>alert("synthetic")</script>'; self.candidate.save()
        apply_candidate(self.candidate); self.client.force_login(self.actor)
        html = self.client.get(url)
        self.assertNotContains(html, '<script>alert("synthetic")</script>')
        self.assertContains(html, '&lt;script&gt;')

    def test_editor_optimistic_revision_covers_import_and_other_manual_fields(self):
        self.client.force_login(self.actor)
        url = reverse("client_edit", args=[self.customer.pk])
        rev = self.client.get(url).context["editor_revision"]
        Client.objects.filter(pk=self.customer.pk).update(contact_position="Concurrent position")
        response = self.client.post(url, {"name": "Stale Name", "editor_revision": rev})
        self.assertEqual(response.status_code, 409)
        rev = self.client.get(url).context["editor_revision"]
        apply_candidate(self.candidate)
        row = self.proposal(); self.decision(row)
        self.assertEqual(self.client.post(url, {"name": "Stale Name", "editor_revision": rev}).status_code, 409)
        self.assertEqual(self.client.post(url, {"name": "Missing token"}).status_code, 409)

    def test_type_change_deletes_only_explicitly_reviewed_automatic_link(self):
        self.customer.client_type="legal"; self.customer.save()
        self.profile.legal_form="ip"; self.profile.save()
        person=Client.objects.create(organization=self.org, client_type="private", name="Synthetic linked person")
        link=ClientCompanyLink.objects.create(company=self.customer, person=person, source="onec_ip",
            source_reference=self.candidate.source_ref, automatic=True)
        apply_candidate(self.candidate); row=self.proposal("classification")
        self.assertEqual(len(row.before["links"]), 1)
        self.assertTrue(ClientCompanyLink.objects.filter(pk=link.pk).exists())
        self.decision(row)
        self.assertFalse(ClientCompanyLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(Client.objects.filter(pk=person.pk).exists())

    def test_type_change_manual_link_is_never_implicitly_removed(self):
        self.customer.client_type="legal"; self.customer.save()
        person=Client.objects.create(organization=self.org, name="Synthetic manual person")
        link=ClientCompanyLink.objects.create(company=self.customer, person=person)
        apply_candidate(self.candidate)
        with self.assertRaises(ValidationError): self.decision(self.proposal("classification"))
        self.assertTrue(ClientCompanyLink.objects.filter(pk=link.pk).exists())

    def test_import_and_accepted_phone_do_not_expand_private_number_registry(self):
        from pool_service.call_processing_models import CallPrivateNumber
        apply_candidate(self.candidate)
        before=list(CallPrivateNumber.objects.all().values())
        self.decision(self.proposal())
        self.assertEqual(list(CallPrivateNumber.objects.all().values()), before)

    def test_contact_labels_fill_blanks_review_differences_and_skip_invalid_phone(self):
        contact=ClientContact.objects.create(client=self.customer, kind="phone", value="+12025550101", match_value="12025550101", sources=["manual"])
        self.update_source(payload={"phones": [{"value": contact.value, "label": "Synthetic label"}, {"value": "not a phone"}]})
        contact.refresh_from_db(); self.assertEqual(contact.label, "Synthetic label")
        self.assertFalse(ClientContact.objects.filter(value="not a phone").exists())
        self.update_source(payload={"phones": [{"value": contact.value, "label": "New label"}]})
        contact.refresh_from_db(); self.assertEqual(contact.label, "Synthetic label")
        self.decision(self.proposal("contact_label:"+str(contact.pk)))
        contact.refresh_from_db(); self.assertEqual(contact.label, "New label")
        self.assertEqual(contact.sources, ["manual"])

    def test_new_candidate_rechecks_name_conflict_and_inn_guard(self):
        candidate=ClientImportCandidate.objects.create(organization=self.org, source_ref="bbbbbbbb-2222-3333-4444-555555555555",
            name=self.customer.name, source_kind="private", status="ready")
        with self.assertRaises(ValueError): apply_candidate(candidate)
        candidate.name="Synthetic legal"; candidate.source_kind="legal"; candidate.save()
        with self.assertRaises(ValueError): apply_candidate(candidate)

    def test_ip_creation_proposal_covers_all_source_values_and_depends_on_classification(self):
        self.customer.client_type="legal"; self.customer.save()
        self.profile.legal_form="entity"; self.profile.save()
        self.update_source(source_kind="ip", inn="SYNTHETIC-IP", payload={"fio":"Synthetic New Person"})
        row=self.proposal("ip_person")
        self.assertIn("phone",row.after["values"])
        with self.assertRaises(ValueError): self.decision(row)
        self.decision(self.proposal("classification"))
        ClientImportCandidate.objects.filter(pk=self.candidate.pk).update(email="changed@example.test")
        self.assertEqual(self.decision(row).state,"stale")
        self.assertFalse(ClientCompanyLink.objects.exists())

    def test_linked_person_proposal_cannot_be_accepted_after_source_ceases_to_be_ip(self):
        self.customer.client_type="legal"; self.customer.save()
        self.profile.legal_form="ip"; self.profile.save()
        person=Client.objects.create(organization=self.org, name="Synthetic Existing IP Person", phone="+12025550111")
        ClientCRMProfile.objects.create(client=person,source="onec_ip")
        ClientCompanyLink.objects.create(company=self.customer,person=person,source="onec_ip",source_reference=self.candidate.source_ref,automatic=True)
        self.update_source(source_kind="ip", payload={"fio":"Synthetic New IP Person"})
        row=self.proposal("phone",client=person)
        ClientImportCandidate.objects.filter(pk=self.candidate.pk).update(source_kind="legal")
        self.assertEqual(self.decision(row).state,"stale")
        person.refresh_from_db();self.assertEqual(person.phone,"+12025550111")

    def test_foreign_candidate_resolution_and_foreign_proposal_post_are_denied(self):
        other=Organization.objects.create(name="Synthetic foreign org",trial_started_at=timezone.now())
        other_client=Client.objects.create(organization=other,name="Synthetic foreign client")
        candidate=ClientImportCandidate.objects.create(organization=other,source_ref="cccccccc-2222-3333-4444-555555555555",name="Synthetic foreign",source_kind="private",status="review")
        proposal=ClientImportProposal.objects.create(candidate=candidate,client=other_client,field="name",source_version=1,before="old",after="new")
        self.client.force_login(self.actor)
        self.assertEqual(self.client.post(reverse("client_onec_import"),{"action":"resolve","candidate_id":candidate.pk,"resolution":"skip"}).status_code,403)
        self.assertEqual(self.client.post(reverse("client_onec_proposals"),{"proposal_id":proposal.pk,"decision":"accepted","token":token(proposal)}).status_code,403)

    def test_revoked_or_expired_access_is_rechecked_by_decision_service(self):
        apply_candidate(self.candidate); row=self.proposal()
        OrganizationAccess.objects.filter(user=self.actor,organization=self.org).update(role="manager")
        with self.assertRaises(PermissionDenied):self.decision(row)
        OrganizationAccess.objects.filter(user=self.actor,organization=self.org).update(role="owner")
        Organization.objects.filter(pk=self.org.pk).update(trial_started_at=timezone.now()-timedelta(days=30))
        with self.assertRaises(PermissionDenied):self.decision(row)

    def test_merge_never_reparents_proposal_audit_target(self):
        from pool_service.client_merge import merge_clients
        legacy=Client.objects.create(organization=self.org,name="Synthetic legacy")
        ClientCRMProfile.objects.create(client=legacy)
        proposal=ClientImportProposal.objects.create(candidate=self.candidate,client=legacy,field="name",source_version=1,before="old",after="new")
        merge_clients(legacy.pk,self.customer.pk,organization_id=self.org.pk,actor=self.actor)
        proposal.refresh_from_db()
        self.assertEqual(proposal.client_id,legacy.pk)
        self.assertEqual(proposal.state,"stale")

    def test_malformed_cross_org_link_never_leaks_foreign_names_or_gets_deleted(self):
        foreign=Organization.objects.create(name="Synthetic foreign")
        foreign_person=Client.objects.create(organization=foreign,name="FOREIGN-SYNTHETIC-SECRET")
        self.customer.client_type="legal";self.customer.save()
        self.profile.legal_form="ip";self.profile.save()
        link=ClientCompanyLink.objects.create(company=self.customer,person=foreign_person,source="onec_ip",source_reference=self.candidate.source_ref,automatic=True)
        with self.assertRaises(ValueError):apply_candidate(self.candidate)
        self.assertFalse(ClientImportProposal.objects.filter(candidate=self.candidate).exists())
        self.assertTrue(ClientCompanyLink.objects.filter(pk=link.pk).exists())
        self.client.force_login(self.actor)
        self.assertNotContains(self.client.get(reverse("client_onec_proposals")),"FOREIGN-SYNTHETIC-SECRET")

    def test_cross_org_link_added_after_comparison_blocks_acceptance(self):
        self.customer.client_type="legal";self.customer.save()
        apply_candidate(self.candidate);row=self.proposal("classification")
        foreign=Organization.objects.create(name="Synthetic foreign")
        person=Client.objects.create(organization=foreign,name="FOREIGN-SYNTHETIC-SECRET")
        link=ClientCompanyLink.objects.create(company=self.customer,person=person,source="onec_ip",source_reference=self.candidate.source_ref,automatic=True)
        with self.assertRaises(ValueError):self.decision(row)
        self.assertTrue(ClientCompanyLink.objects.filter(pk=link.pk).exists())

    def test_admin_stale_or_missing_token_cannot_overwrite_import(self):
        self.actor.is_staff=True;self.actor.is_superuser=True;self.actor.save()
        self.client.force_login(self.actor)
        url=reverse("admin:pool_service_client_change",args=[self.customer.pk])
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        rev=page.context["adminform"].form.initial["crm_revision"]
        apply_candidate(self.candidate)
        data={"name":"Synthetic stale admin edit","client_type":"private","organization":self.org.pk,
            "phone":self.customer.phone,"email":self.customer.email,"crm_revision":rev,"_save":"Save"}
        result=self.client.post(url,data)
        self.assertContains(result,"форма устарела")
        data.pop("crm_revision")
        self.assertContains(self.client.post(url,data),"форма устарела")
        data["crm_revision"]=self.client.get(url).context["adminform"].form.initial["crm_revision"]
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.customer.refresh_from_db();self.assertEqual(self.customer.name,"Synthetic stale admin edit")

    def test_stale_proposal_http_returns_conflict_and_actionable_comparison(self):
        apply_candidate(self.candidate);row=self.proposal()
        Client.objects.filter(pk=self.customer.pk).update(phone="+12025550189")
        self.client.force_login(self.actor)
        result=self.client.post(reverse("client_onec_proposals"),{"proposal_id":row.pk,"decision":"accepted","token":token(row)})
        self.assertContains(result,"Обновить сравнение",status_code=409)

    def test_incomplete_fetch_and_foreign_contact_payload_do_not_mutate_crm(self):
        from pool_service.client_crm_import import _buyer_rows, _candidate_contacts
        from pool_service.client_crm_sync import _recent_buyer_rows
        with patch("pool_service.client_crm_import.query_1c_rows",return_value={"rows":[],"complete":False}):
            with self.assertRaises(RuntimeError):list(_buyer_rows(None,None))
        with patch("pool_service.client_crm_sync.query_1c_rows",return_value={"rows":[],"complete":False}):
            with self.assertRaises(RuntimeError):_recent_buyer_rows(None,None,lookback_hours=48)
        with self.assertRaises(ValueError):_candidate_contacts({"Ref_Key":"source-a"},[{"Ref_Key":"source-b","Тип":"Телефон","НомерТелефона":"+12025550177"}])
        self.assertFalse(ClientImportProposal.objects.exists())

    def test_rejected_additional_contact_does_not_return_for_label_or_email_case(self):
        self.update_source(payload={"emails":[{"value":"extra@example.test","label":"First label"}]})
        row=ClientImportProposal.objects.get(candidate=self.candidate,field__startswith="contact:email:")
        self.decision(row,"rejected")
        self.update_source(payload={"emails":[{"value":"EXTRA@EXAMPLE.TEST","label":"Second label"}]})
        self.assertEqual(ClientImportProposal.objects.filter(candidate=self.candidate,field__startswith="contact:email:").count(),1)
        row.refresh_from_db();self.assertEqual(row.state,"rejected")

    def test_existing_empty_legal_form_and_name_structure_still_require_classification(self):
        self.customer.client_type="legal";self.customer.save()
        apply_candidate(self.candidate)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.first_name)
        with self.assertRaises(ValidationError):self.decision(self.proposal("first_name"))
        self.update_source(source_kind="legal",inn="SYNTHETIC-INN")
        self.profile.refresh_from_db();self.assertEqual(self.profile.legal_form,"")
        self.assertEqual(self.proposal("classification").state,"pending")

    def test_legal_form_confirmation_preserves_compatible_manual_contact_link(self):
        self.customer.client_type="legal";self.customer.save()
        person=Client.objects.create(organization=self.org,name="Synthetic manual contact")
        link=ClientCompanyLink.objects.create(company=self.customer,person=person,position="Synthetic role")
        self.update_source(source_kind="legal",inn="SYNTHETIC-INN")
        self.decision(self.proposal("classification"))
        self.profile.refresh_from_db();self.assertEqual(self.profile.legal_form,"entity")
        link.refresh_from_db();self.assertEqual(link.position,"Synthetic role")
