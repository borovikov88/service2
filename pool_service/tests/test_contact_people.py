from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.client_crm_models import ClientContact, OrganizationPersonLink
from pool_service.services.contact_people import save_phone, phone_revision, link_person
from pool_service.client_merge import merge_clients


class ContactPeopleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic directory", paid_until=timezone.now()+timedelta(days=30))
        self.actor = get_user_model().objects.create_user(username="synthetic-owner")
        self.access = OrganizationAccess.objects.create(organization=self.org, user=self.actor, role="owner")
        self.person = Client.objects.create(organization=self.org, name="Synthetic Person", client_type="private", phone="+7 900 111 22 33")
        self.user = get_user_model().objects.create_user(username="synthetic-staff")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic Staff")

    def save(self, value="+7 900 444 55 66", kind="personal", primary="1"):
        self.person.refresh_from_db()
        return save_phone(self.actor, self.person.pk, value=value, phone_type=kind, primary=primary, revision=phone_revision(self.person))

    def test_primary_preserves_legacy_number_and_personal_is_not_privacy(self):
        from pool_service.call_processing_models import CallPrivateNumber
        row = self.save()
        self.person.refresh_from_db()
        self.assertEqual(self.person.phone, row.value)
        self.assertEqual(ClientContact.objects.filter(client=self.person).count(), 2)
        self.assertEqual(ClientContact.objects.filter(client=self.person, is_primary=True).count(), 1)
        self.assertFalse(CallPrivateNumber.objects.exists())
        self.save(value="89004445566", kind="work", primary="0")
        self.assertEqual(ClientContact.objects.filter(client=self.person).count(), 2)
        row.refresh_from_db()
        self.assertEqual(row.phone_type, "work")
        self.assertTrue(row.is_primary)

    def test_stale_post_does_not_duplicate_or_replace_primary(self):
        revision = phone_revision(self.person)
        self.save()
        with self.assertRaises(ValidationError):
            save_phone(self.actor, self.person.pk, value="+7 900 777 88 99", phone_type="other", primary="1", revision=revision)
        self.assertEqual(ClientContact.objects.count(), 2)

    def test_link_is_explicit_repeat_safe_and_never_changes_portal_user(self):
        link = link_person(self.actor, self.person.pk, self.user.pk)
        self.assertEqual(link.employee, self.employee)
        self.assertEqual(link_person(self.actor, self.person.pk, self.user.pk).pk, link.pk)
        self.person.refresh_from_db()
        self.assertIsNone(self.person.user_id)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(OrganizationAccess.objects.count(), 2)
        other = Client.objects.create(organization=self.org, name="Other", client_type="private")
        with self.assertRaises(ValidationError):
            link_person(self.actor, other.pk, self.user.pk)
        with self.assertRaises(ValueError):
            merge_clients(self.person.pk, other.pk, organization_id=self.org.pk, actor=self.actor)
        self.assertEqual(OrganizationPersonLink.objects.count(), 1)

    def test_current_roles_and_org_boundaries(self):
        other = Organization.objects.create(name="Other")
        foreign = Client.objects.create(organization=other, name="Foreign", client_type="private")
        with self.assertRaises(PermissionDenied):
            link_person(self.actor, foreign.pk, self.user.pk)
        self.access.role = "manager"
        self.access.save()
        with self.assertRaises(PermissionDenied):
            self.save()
        with self.assertRaises(PermissionDenied):
            link_person(self.actor, self.person.pk, self.user.pk)

    def test_http_forms_and_person_link_not_exposed_to_manager(self):
        self.client.force_login(self.actor)
        url = reverse("client_detail", args=[self.person.pk])
        self.assertContains(self.client.get(url), "data-person-phone-form")
        response = self.client.post(url, {"action":"save_person_phone", "phone":"+7 900 444 55 66", "phone_type":"personal", "primary":"1", "contacts_revision":phone_revision(self.person)})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(url), "Личный")
        link_person(self.actor, self.person.pk, self.user.pk)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(url), "data-person-user-form")
        self.assertEqual(self.client.post(url, {"action":"link_person_user", "user_id":self.user.pk}).status_code, 403)

    def test_foreign_user_and_dismissed_employee_are_rejected(self):
        foreign = get_user_model().objects.create_user(username="foreign")
        with self.assertRaises(PermissionDenied):
            link_person(self.actor, self.person.pk, foreign.pk)
        self.employee.is_active = False
        self.employee.save()
        with self.assertRaises(ValidationError):
            link_person(self.actor, self.person.pk, self.user.pk)

    def test_legacy_primary_editor_keeps_all_directory_numbers(self):
        from pool_service.forms import ClientCreateForm
        old = ClientContact.objects.create(client=self.person, kind="phone", value=self.person.phone,
            match_value="9001112233", is_primary=True, sources=["manual"])
        self.save()
        self.person.refresh_from_db()
        form = ClientCreateForm({"client_type":"private", "first_name":"Synthetic", "last_name":"Person", "phone":"+7 900 777 88 99"}, instance=self.person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertTrue(ClientContact.objects.filter(pk=old.pk).exists())
        self.assertEqual(ClientContact.objects.filter(client=self.person).count(), 3)

    def test_merge_transfers_known_type_and_rolls_back_conflicting_type(self):
        from pool_service.client_merge import _merge_contacts
        from django.db import transaction
        other = Client.objects.create(organization=self.org, name="Other", client_type="private")
        source = ClientContact.objects.create(client=self.person, kind="phone", value="+79001112233", match_value="9001112233", phone_type="personal")
        target = ClientContact.objects.create(client=other, kind="phone", value="+79001112233", match_value="9001112233", phone_type="work")
        with self.assertRaises(ValueError), transaction.atomic():
            _merge_contacts(self.person, other)
        self.assertTrue(ClientContact.objects.filter(pk=source.pk).exists())
        target.phone_type = "other"; target.save()
        with transaction.atomic():
            _merge_contacts(self.person, other)
        target.refresh_from_db(); self.assertEqual(target.phone_type, "personal")

    def test_import_preserves_phone_type_and_directory_source(self):
        from types import SimpleNamespace
        from pool_service.client_crm_import import _sync_contacts
        row = self.save()
        candidate = SimpleNamespace(source_ref="synthetic-ref", payload={"phones":[{"value":row.value}]})
        _sync_contacts(self.person, candidate)
        row.refresh_from_db(); self.assertEqual(row.phone_type,"personal")
        candidate.payload = {}; _sync_contacts(self.person,candidate)
        self.assertTrue(ClientContact.objects.filter(pk=row.pk).exists())
