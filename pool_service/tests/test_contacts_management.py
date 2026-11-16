"""Synthetic phone CRUD and reviewed merges, including rollback and scope."""
import json
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.utils import timezone
from pool_service.models import Client, Organization, OrganizationAccess, Pool, ClientAccess
from pool_service.client_crm_models import (ClientContact, ClientCRMProfile, ClientCompanyLink,
    ClientContactExclusion, ClientMergeRecord, ClientImportCandidate)
from pool_service.client_queries import active_clients
from pool_service.communication_models import PhoneCall, Conversation, CommunicationChannel, ChannelConnection
from pool_service.crm_commercial_models import CrmLead, CrmDeal, CrmPipeline, CrmStage, CrmEvent
from pool_service.services.contact_people import manage_phone, phone_revision
from pool_service.services.contact_management import preview_merge, execute_merge
from pool_service.phone_utils import normalize_phone


class ContactManagementTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic contact management", paid_until=timezone.now()+timedelta(days=30))
        self.actor = get_user_model().objects.create_user("synthetic-contact-manager")
        self.access = OrganizationAccess.objects.create(user=self.actor, organization=self.org, role="owner")
        self.client.force_login(self.actor)
        self.a = Client.objects.create(organization=self.org, name="Synthetic Main", client_type="legal", phone="+12025550101")
        self.b = Client.objects.create(organization=self.org, name="Synthetic Duplicate", client_type="legal", phone="+12025550102")
        self.c = Client.objects.create(organization=self.org, name="Synthetic Third", client_type="legal")
        self.person = Client.objects.create(organization=self.org, name="Synthetic Person", client_type="private")

    def phone(self, client=None, action="add", **values):
        row = client or self.a
        row.refresh_from_db()
        return manage_phone(self.actor, row.pk, action=action, key=values.pop("key", ""),
            revision=values.pop("revision", phone_revision(row)), **values)

    def preview(self, ids=None, target=None):
        return preview_merge(self.actor, ids or [self.a.pk, self.b.pk], target or self.a.pk)

    def test_company_and_person_phone_edit_primary_and_last_delete(self):
        for record in (self.a, self.person):
            self.phone(record, value="+12025550103", phone_type="work", label="Office", primary="1")
            self.phone(record, action="edit", key="+12025550103", value="+12025550104", phone_type="personal", label="Direct")
            record.refresh_from_db()
            self.assertEqual(record.phone, "+12025550104")
            self.assertEqual(ClientContact.objects.get(client=record,kind="phone",is_primary=True).value,record.phone)
            self.assertTrue(ClientContactExclusion.objects.filter(client=record, match_value="+12025550103").exists())
            self.phone(record, action="delete", key="+12025550104")
            record.refresh_from_db()
            if record.pk == self.a.pk:
                self.assertEqual(record.phone, "+12025550101")
                self.phone(record, action="delete", key="+12025550101")
            record.refresh_from_db()
            self.assertEqual(record.phone, "")
            self.assertFalse(ClientContact.objects.filter(client=record, kind="phone").exists())

    def test_imported_number_delete_does_not_restore_or_match_and_keeps_call(self):
        from pool_service.client_import_review import reconcile
        from pool_service.client_phone_matching import clients_by_phone
        ClientContact.objects.create(client=self.a, kind="phone", value=self.a.phone, match_value=self.a.phone,
            label="Основной", sources=["onec"], is_primary=True)
        call=PhoneCall.objects.create(organization=self.org, client=self.a, external_id="synthetic-call", phone_number=self.a.phone,
            started_at=timezone.now(), direction="incoming", result="missed")
        profile=ClientCRMProfile.objects.create(client=self.a, onec_ref="11111111-2222-3333-4444-555555555555")
        candidate=ClientImportCandidate.objects.create(organization=self.org, source_ref=profile.onec_ref, name=self.a.name,
            source_kind="legal", phone=self.a.phone, matched_client=self.a,
            payload={"phones":[{"value":self.a.phone,"label":"Office"}]})
        self.phone(action="delete", key=self.a.phone)
        self.a.refresh_from_db(); reconcile(candidate, self.a, profile)
        self.a.refresh_from_db(); call.refresh_from_db()
        self.assertEqual(self.a.phone, "")
        self.assertFalse(ClientContact.objects.filter(client=self.a, kind="phone").exists())
        self.assertFalse(clients_by_phone(self.org, call.phone_number))
        self.assertEqual((call.client_id,call.phone_number),(self.a.pk,"+12025550101"))
        self.phone(value="+12025550101")
        self.assertFalse(ClientContactExclusion.objects.filter(client=self.a, match_value="+12025550101").exists())

    def test_phone_revision_permissions_and_duplicate_edit(self):
        previous=phone_revision(self.a)
        self.phone(value="+12025550103")
        with self.assertRaises(ValidationError): self.phone(action="delete",key=self.a.phone,revision=previous)
        with self.assertRaises(ValidationError): self.phone(action="edit",key=self.a.phone,value="+12025550103")
        self.access.role="manager";self.access.save()
        with self.assertRaises(PermissionDenied): self.phone(action="delete",key=self.a.phone)

    def test_legacy_short_numbers_have_distinct_editable_keys(self):
        from pool_service.services.contact_people import phone_rows
        for value in ("55-55-55", "доб. 123"):
            ClientContact.objects.create(client=self.a,kind="phone",value=value,sources=["onec"])
        keys={row["value"]:row["key"] for row in phone_rows(self.a)}
        self.assertNotEqual(keys["55-55-55"],keys["доб. 123"])
        self.phone(action="delete",key=keys["55-55-55"])
        self.assertTrue(ClientContact.objects.filter(client=self.a,value="доб. 123").exists())
        self.phone(action="edit",key=keys["доб. 123"],value="+12025550109")
        self.assertFalse(ClientContact.objects.filter(client=self.a,value="доб. 123").exists())

    def test_deleted_legacy_short_primary_does_not_return_from_onec(self):
        from pool_service.services.contact_people import phone_rows
        from pool_service.client_import_review import reconcile
        self.a.phone="55-55-55";self.a.save()
        profile=ClientCRMProfile.objects.create(client=self.a,onec_ref="11111111-2222-3333-4444-555555555555")
        candidate=ClientImportCandidate.objects.create(organization=self.org,source_ref=profile.onec_ref,
            source_kind="legal",name=self.a.name,phone="55-55-55",matched_client=self.a)
        self.phone(action="delete",key=phone_rows(self.a)[0]["key"])
        self.a.refresh_from_db();reconcile(candidate,self.a,profile)
        self.a.refresh_from_db();self.assertEqual(self.a.phone,"")

    def test_company_without_phone_creation_edit_and_get_are_read_only(self):
        response=self.client.post(reverse("client_create"),{"org_id":self.org.pk,
            "customer":json.dumps({"company":{"name":"Synthetic no phone"}})},HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code,200,response.content)
        record=Client.objects.get(pk=response.json()["company"]["id"])
        url=reverse("client_edit",args=[record.pk]);page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        result=self.client.post(url,{"name":"Synthetic edited no phone", "editor_revision":page.context["editor_revision"]})
        self.assertEqual(result.status_code,302)
        record.refresh_from_db();self.assertFalse(record.phone)
        before=list(ClientContact.objects.values())
        for url in (reverse("client_detail",args=[self.a.pk]),reverse("clients_list")):
            self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(before,list(ClientContact.objects.values()))

    def test_phone_endpoint_csrf_and_stale_are_controlled(self):
        url=reverse("client_phone_manage",args=[self.a.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        self.assertEqual(self.client.post(url,{"operation":"delete","phone_key":self.a.phone}).status_code,409)
        browser=HttpClient(enforce_csrf_checks=True);browser.force_login(self.actor)
        self.assertEqual(browser.post(url,{}).status_code,403)

    def test_merge_manual_companies_preserves_crm_calls_messages_links_and_scalar_contacts(self):
        pool=Pool.objects.create(organization=self.org,client=self.b,address="Synthetic pool")
        pipeline=CrmPipeline.objects.create(organization=self.org,code="synthetic",name="Synthetic")
        stage=CrmStage.objects.create(pipeline=pipeline,code="new",name="New")
        lead=CrmLead.objects.create(organization=self.org,client=self.b,title="Synthetic lead",contact=self.person)
        deal=CrmDeal.objects.create(organization=self.org,pipeline=pipeline,stage=stage,client=self.b,contact=self.person,
            title="Synthetic deal",object_kind="pool",pool=pool)
        prior=CrmEvent.objects.create(organization=self.org,lead=lead,event_type="synthetic",safe_diff={})
        ClientCompanyLink.objects.create(company=self.b,person=self.person,position="Synthetic role")
        call=PhoneCall.objects.create(organization=self.org,client=self.b,external_id="merge-synthetic",phone_number=self.b.phone,
            started_at=timezone.now(),direction="incoming",result="missed")
        channel=CommunicationChannel.objects.create(organization=self.org,kind="avito",name="Synthetic")
        connection=ChannelConnection.objects.create(channel=channel,name="Synthetic",external_id="synthetic")
        conversation=Conversation.objects.create(organization=self.org,connection=connection,external_id="synthetic",client=self.b)
        self.b.email="secondary@example.invalid";self.b.save()
        preview=self.preview([self.a.pk,self.b.pk,self.c.pk])
        self.assertEqual((preview["leads"],preview["deals"]),(1,1))
        execute_merge(self.actor,preview["token"])
        for row in (pool,lead,deal,call,conversation):row.refresh_from_db();self.assertEqual(row.client_id,self.a.pk)
        self.assertEqual((lead.revision,deal.revision),(1,1))
        self.assertEqual(lead.contact_id,self.person.pk)
        prior.refresh_from_db();self.assertEqual(prior.lead_id,lead.pk)
        self.assertEqual(CrmEvent.objects.filter(event_type="contact_merged").count(),2)
        self.assertEqual(set(ClientContact.objects.filter(client=self.a,kind="phone").values_list("value",flat=True)),{"+12025550101","+12025550102"})
        self.assertEqual(ClientContact.objects.filter(client=self.a,kind="phone",is_primary=True).count(),1)
        self.assertEqual(active_clients().filter(pk__in=[self.b.pk,self.c.pk]).count(),0)
        self.assertEqual(ClientMergeRecord.objects.count(),1)
        self.assertEqual(ClientCompanyLink.objects.get(person=self.person).company_id,self.a.pk)
        self.assertRedirects(self.client.get(reverse("client_detail",args=[self.b.pk])),reverse("client_detail",args=[self.a.pk]))

    def test_private_merge_updates_both_client_and_contact(self):
        second=Client.objects.create(organization=self.org,client_type="private",name="Synthetic Person Duplicate")
        lead=CrmLead.objects.create(organization=self.org,client=second,contact=second,title="Synthetic personal lead")
        execute_merge(self.actor,self.preview([self.person.pk,second.pk],self.person.pk)["token"])
        lead.refresh_from_db();self.assertEqual((lead.client_id,lead.contact_id),(self.person.pk,self.person.pk))

    def test_stale_preview_actor_binding_revoked_access_and_replay(self):
        preview=self.preview()
        self.b.name="Changed synthetic";self.b.save()
        with self.assertRaises(ValidationError):execute_merge(self.actor,preview["token"])
        preview=self.preview()
        other=get_user_model().objects.create_user("synthetic-other-owner")
        OrganizationAccess.objects.create(user=other,organization=self.org,role="admin")
        with self.assertRaises(PermissionDenied):execute_merge(other,preview["token"])
        self.access.role="manager";self.access.save()
        with self.assertRaises(PermissionDenied):execute_merge(self.actor,preview["token"])
        self.access.role="owner";self.access.save()
        execute_merge(self.actor,preview["token"])
        self.assertEqual(execute_merge(self.actor,preview["token"]).pk,self.a.pk)
        self.assertEqual(ClientMergeRecord.objects.count(),1)

    def test_cross_tenant_type_account_and_two_onec_identities_refused(self):
        with self.assertRaises(ValidationError):self.preview([self.a.pk,self.person.pk])
        foreign=Client.objects.create(organization=Organization.objects.create(name="Synthetic foreign"),name="Secret synthetic")
        with self.assertRaises(PermissionDenied):self.preview([self.a.pk,foreign.pk])
        access=ClientAccess.objects.create(client=self.b,user=self.actor)
        with self.assertRaises(ValidationError):self.preview()
        access.delete()
        for record,ref in ((self.a,"11111111-2222-3333-4444-555555555555"),(self.b,"22222222-2222-3333-4444-555555555555")):
            ClientCRMProfile.objects.create(client=record,onec_ref=ref)
        with self.assertRaises(ValidationError):self.preview()

    def test_onec_survivor_keeps_mapping_and_manual_source_contacts(self):
        profile=ClientCRMProfile.objects.create(client=self.b,onec_ref="11111111-2222-3333-4444-555555555555")
        with self.assertRaises(ValidationError):self.preview()
        preview=preview_merge(self.actor,[self.a.pk,self.b.pk])
        self.assertEqual(preview["target_id"],self.b.pk)
        execute_merge(self.actor,preview["token"])
        profile.refresh_from_db();self.assertEqual(profile.client_id,self.b.pk)
        self.assertEqual(ClientContact.objects.filter(client=self.b,kind="phone").count(),2)

    def test_full_set_rolls_back_when_last_merge_fails(self):
        from pool_service.client_merge import merge_clients
        preview=self.preview([self.a.pk,self.b.pk,self.c.pk])
        original=list(Client.objects.values()); calls=[]
        def checked(source,*args,**kwargs):
            calls.append(source)
            if source==self.c.pk:raise ValidationError("Synthetic final conflict")
            return merge_clients(source,*args,**kwargs)
        with patch("pool_service.services.contact_management.merge_clients",side_effect=checked):
            with self.assertRaises(ValidationError):execute_merge(self.actor,preview["token"])
        self.assertEqual(len(calls),2)
        self.assertEqual(list(Client.objects.values()),original)
        self.assertFalse(ClientMergeRecord.objects.exists())
        self.assertFalse(ClientCRMProfile.objects.filter(merged_into__isnull=False).exists())

    def test_malformed_and_foreign_merge_requests_disclose_no_names(self):
        url=reverse("client_bulk_merge")
        for data in ({"action":"preview","ids":[self.a.pk]}, {"action":"merge","token":"bad"}, []):
            self.assertEqual(self.client.post(url,data=json.dumps(data),content_type="application/json").status_code,409)
        self.assertEqual(self.client.get(url).status_code,405)

    def test_tombstones_dedupe_and_old_merge_audit_does_not_reparent(self):
        for record in (self.a,self.b):
            ClientContactExclusion.objects.create(client=record,kind="phone",match_value="+12025550999",actor=self.actor)
        old=ClientMergeRecord.objects.create(organization=self.org,target=self.b,actor=self.actor,snapshot={})
        execute_merge(self.actor,self.preview()["token"])
        old.refresh_from_db();self.assertEqual(old.target_id,self.b.pk)
        self.assertEqual(ClientContactExclusion.objects.filter(client=self.a,match_value="+12025550999").count(),1)

    def test_reward_templates_are_not_transferred_or_combined(self):
        from pool_service.reward_models import RewardParticipantTemplate
        from pool_service.models import Employee
        for index,client in enumerate((self.a,self.b)):
            employee=Employee.objects.create(organization=self.org,display_name=f"Synthetic employee {index}")
            RewardParticipantTemplate.objects.create(organization=self.org,client=client,employee=employee,
                role="client_manager",share="1.000000",effective_from=timezone.localdate(),created_by=self.actor)
        before=list(RewardParticipantTemplate.objects.values())
        with self.assertRaises(ValidationError):self.preview()
        self.assertEqual(list(RewardParticipantTemplate.objects.values()),before)
        self.assertFalse(ClientMergeRecord.objects.exists())

    def _draft(self, client, payer):
        from pool_service.crm_recurring_models import CrmRecurringDraft
        pipeline=CrmPipeline.objects.create(organization=self.org,code="recurring",name="Synthetic recurring")
        stage=CrmStage.objects.create(pipeline=pipeline,code="new",name="New")
        deal=CrmDeal.objects.create(organization=self.org,pipeline=pipeline,stage=stage,client=client,title="Synthetic recurring")
        return CrmRecurringDraft.objects.create(organization=self.org,deal=deal,client=client,payer=payer,
            object_scope={"kind":"none"},contract_basis="Synthetic terms",created_by=self.actor)

    def test_unconfirmed_recurring_draft_client_is_not_orphaned(self):
        draft=self._draft(self.b,self.b)
        with self.assertRaises(ValidationError):self.preview()
        draft.refresh_from_db();self.assertEqual(draft.client_id,self.b.pk)

    def test_unconfirmed_recurring_draft_payer_is_not_orphaned(self):
        draft=self._draft(self.c,self.b)
        with self.assertRaises(ValidationError):self.preview()
        draft.refresh_from_db();self.assertEqual(draft.payer_id,self.b.pk)

    def test_inconsistent_foreign_history_blocks_entire_merge(self):
        other=Organization.objects.create(name="Synthetic foreign history")
        call=PhoneCall.objects.create(organization=other,client=self.b,external_id="synthetic-inconsistent",phone_number=self.b.phone,
            started_at=timezone.now(),direction="incoming",result="missed")
        with self.assertRaises(ValidationError):self.preview()
        call.refresh_from_db();self.assertEqual(call.client_id,self.b.pk)

    def test_new_phone_or_link_after_preview_requires_new_review(self):
        preview=self.preview()
        self.phone(self.b,value="+12025550109")
        with self.assertRaises(ValidationError):execute_merge(self.actor,preview["token"])
        preview=self.preview()
        ClientCompanyLink.objects.create(company=self.b,person=self.person)
        with self.assertRaises(ValidationError):execute_merge(self.actor,preview["token"])

    def test_merge_csrf_and_receipt_recheck_current_permissions(self):
        browser=HttpClient(enforce_csrf_checks=True);browser.force_login(self.actor)
        self.assertEqual(browser.post(reverse("client_bulk_merge"),data=json.dumps({"action":"preview","ids":[self.a.pk,self.b.pk]}),content_type="application/json").status_code,403)
        preview=self.preview();execute_merge(self.actor,preview["token"])
        self.access.role="manager";self.access.save()
        with self.assertRaises(PermissionDenied):execute_merge(self.actor,preview["token"])
