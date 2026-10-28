from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientCompanyLink, ClientCRMProfile
from pool_service.client_queries import readable_clients
from pool_service.services.contact_roles import set_supplier
from pool_service.client_merge import merge_clients


class ContactsRolesTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic role org", paid_until=timezone.now()+timedelta(days=30))
        self.other = Organization.objects.create(name="Synthetic other")
        self.owner = get_user_model().objects.create_user(username="supplier-owner")
        self.membership = OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.client.force_login(self.owner)
        self.company = Client.objects.create(name="Synthetic Company", organization=self.org, client_type="legal")
        self.person = Client.objects.create(name="Synthetic Person", organization=self.org, client_type="private")
        self.foreign = Client.objects.create(name="Foreign secret identity", organization=self.other, client_type="private")

    def post_role(self, enabled="1", revision=0):
        return self.client.post(reverse("client_detail", args=[self.company.pk]), {"action":"set_supplier", "enabled":enabled, "revision":revision})

    def test_explicit_add_remove_audit_and_stale_submission(self):
        self.assertEqual(self.post_role().status_code,302)
        self.assertEqual(self.post_role().status_code,409)
        self.assertEqual(self.post_role(revision=1).status_code,302)
        self.assertEqual(ClientBusinessRoleEvent.objects.count(),1)
        self.assertEqual(self.post_role("0",1).status_code,302)
        role=ClientBusinessRole.objects.get(client=self.company)
        self.assertEqual((role.active,role.revision),(False,2))
        self.assertEqual(list(ClientBusinessRoleEvent.objects.values_list("active",flat=True)),[True,False])
        self.assertEqual(Client.objects.count(),3)

    def test_unknown_state_and_revision_rejected(self):
        for enabled,rev in [("yes",0),("1","invalid"),("1",-1)]:
            self.assertEqual(self.post_role(enabled,rev).status_code,409)
        self.assertFalse(ClientBusinessRole.objects.exists())

    def test_manager_read_only_role_and_no_lookup(self):
        self.membership.role="manager";self.membership.save()
        self.assertEqual(self.post_role().status_code,403)
        response=self.client.get(reverse("client_detail",args=[self.company.pk]))
        self.assertNotContains(response,'data-supplier-form')
        self.assertEqual(self.client.get(reverse("client_detail",args=[self.company.pk]),{"lookup":"relationships","q":"Person"}).status_code,403)

    def test_role_service_denies_foreign_inactive_revoked_and_merged(self):
        with self.assertRaises(PermissionDenied):set_supplier(self.owner,self.foreign.pk,"1",0)
        self.owner.is_active=False;self.owner.save()
        with self.assertRaises(PermissionDenied):set_supplier(self.owner,self.company.pk,"1",0)
        self.assertFalse(readable_clients(self.owner).exists())
        self.owner.is_active=True;self.owner.save()
        self.membership.delete()
        with self.assertRaises(PermissionDenied):set_supplier(self.owner,self.company.pk,"1",0)
        OrganizationAccess.objects.create(user=self.owner,organization=self.org,role="owner")
        ClientCRMProfile.objects.create(client=self.company,merged_into=self.person)
        with self.assertRaises(PermissionDenied):set_supplier(self.owner,self.company.pk,"1",0)

    def test_empty_supplier_and_dual_tabs_keep_same_ids(self):
        url=reverse("clients_list")
        self.assertContains(self.client.get(url),"Поставщики ещё не отмечены")
        self.assertFalse(ClientBusinessRole.objects.exists())
        set_supplier(self.owner,self.company.pk,"1",0)
        set_supplier(self.owner,self.person.pk,"1",0)
        response=self.client.get(url)
        self.assertEqual([c.pk for c in response.context["companies"]],[self.company.pk])
        self.assertEqual({c.pk for c in response.context["suppliers"]},{self.company.pk,self.person.pk})
        self.assertEqual([c.pk for c in response.context["private_contacts"]],[self.person.pk])

    def test_malformed_links_never_disclose_other_side_or_allow_edit(self):
        link=ClientCompanyLink.objects.create(company=self.company,person=self.foreign)
        for url in [reverse("clients_list"),reverse("client_detail",args=[self.company.pk])]:
            self.assertNotContains(self.client.get(url),self.foreign.name)
        for action in ["update_company_link","delete_company_link"]:
            response=self.client.post(reverse("client_detail",args=[self.company.pk]),{"action":action,"link_id":link.pk,"position":"changed"})
            self.assertEqual(response.status_code,403)
        link.refresh_from_db();self.assertEqual(link.position,"")

    def test_mixed_org_roles_do_not_authorize_second_org(self):
        OrganizationAccess.objects.create(user=self.owner,organization=self.other,role="client")
        self.assertNotContains(self.client.get(reverse("clients_list")),self.foreign.name)

    def test_merged_link_hidden_from_list_and_card(self):
        ClientCompanyLink.objects.create(company=self.company,person=self.person)
        ClientCRMProfile.objects.create(client=self.person,merged_into=self.company)
        for url in [reverse("clients_list"),reverse("client_detail",args=[self.company.pk])]:
            self.assertNotContains(self.client.get(url),self.person.name)

    def test_role_survives_profile_save_and_merge_preserves_audit(self):
        set_supplier(self.owner,self.company.pk,"1",0)
        target=Client.objects.create(name="Synthetic Target",organization=self.org,client_type="legal")
        ClientCRMProfile.objects.create(client=target,onec_ref="00000000-0000-0000-0000-000000000789")
        merge_clients(self.company.pk,target.pk,organization_id=self.org.pk,actor=self.owner)
        self.assertTrue(ClientBusinessRole.objects.get(client=target).active)
        self.assertEqual(ClientBusinessRoleEvent.objects.filter(client=self.company).count(),1)
        event=ClientBusinessRoleEvent.objects.get(client=target)
        self.assertEqual(event.source_client_id,self.company.pk)

    def test_csrf_required_for_role_write(self):
        from django.test import Client as HttpClient
        browser=HttpClient(enforce_csrf_checks=True);browser.force_login(self.owner)
        self.assertEqual(browser.post(reverse("client_detail",args=[self.company.pk]),{"action":"set_supplier","enabled":"1","revision":0}).status_code,403)
        self.assertFalse(ClientBusinessRole.objects.exists())

    def test_disabled_absent_role_is_true_noop(self):
        self.assertEqual(self.post_role("0",0).status_code,302)
        self.assertFalse(ClientBusinessRole.objects.exists())
        self.assertFalse(ClientBusinessRoleEvent.objects.exists())

    def test_fresh_user_and_target_organization_access(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):set_supplier(self.owner,self.company.pk,"1",0)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True)
        OrganizationAccess.objects.create(user=self.owner,organization=self.other,role="owner")
        self.other.trial_started_at=timezone.now()-timedelta(days=60);self.other.save()
        with self.assertRaises(PermissionDenied):set_supplier(self.owner,self.foreign.pk,"1",0)
        self.org.paid_until=timezone.now()-timedelta(days=1)
        self.org.trial_started_at=timezone.now()-timedelta(days=60);self.org.save()
        self.other.paid_until=timezone.now()+timedelta(days=30);self.other.save()
        set_supplier(self.owner,self.foreign.pk,"1",0)
        self.assertTrue(ClientBusinessRole.objects.get(client=self.foreign).active)

    def test_primary_update_does_not_rewrite_hidden_malformed_link(self):
        hidden=ClientCompanyLink.objects.create(company=self.company,person=self.foreign,is_primary=True)
        response=self.client.post(reverse("client_detail",args=[self.company.pk]),{
            "action":"add_company_link","related_client":self.person.pk,"is_primary":"1"})
        self.assertEqual(response.status_code,302)
        hidden.refresh_from_db();self.assertTrue(hidden.is_primary)
        valid=ClientCompanyLink.objects.get(company=self.company,person=self.person)
        self.client.post(reverse("client_detail",args=[self.company.pk]),{
            "action":"update_company_link","link_id":valid.pk,"is_primary":"1"})
        hidden.refresh_from_db();self.assertTrue(hidden.is_primary)

    def test_unknown_role_does_not_imply_supplier(self):
        ClientBusinessRole.objects.create(client=self.company,role="unknown",active=True)
        response=self.client.get(reverse("clients_list"))
        self.assertEqual(response.context["suppliers"],[])

    def test_role_history_prevents_delete_without_server_error(self):
        set_supplier(self.owner,self.company.pk,"1",0)
        response=self.client.post(reverse("client_delete",args=[self.company.pk]))
        self.assertEqual(response.status_code,302)
        self.assertTrue(Client.objects.filter(pk=self.company.pk).exists())
        self.assertEqual(ClientBusinessRoleEvent.objects.count(),1)
