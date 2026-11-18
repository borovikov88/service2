from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib import admin
from pool_service.models import Organization, OrganizationAccess, DataAuditLog
from pool_service.staff_call_models import StaffAccount
from pool_service.crm_commercial_models import CrmAccess
from pool_service.communication_models import CommunicationAccess
from pool_service.services.crm_role_policy_preview import CAPABILITIES
from pool_service.admin import OrganizationAccessInline
from django.test import RequestFactory


@override_settings(CRM_ROLE_TRANSITION_ENABLED=True)
class RoleReviewUiTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic', crm_v2_read=True, crm_v2_write=True)
        self.owner = get_user_model().objects.create_user('owner')
        self.target = get_user_model().objects.create_user('member')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        self.access = OrganizationAccess.objects.create(organization=self.org, user=self.target, role='service')
        StaffAccount.objects.create(organization=self.org, user=self.target, kind='person')
        self.client.force_login(self.owner)
        self.url = reverse('crm_role_review', args=[self.org.pk, self.target.pk])

    def preview(self):
        return self.client.post(self.url, {'operation': 'preview', 'roles': ['manager'], 'reviewed': 'on',
            **{'cap_' + f: 'inherit' for f in CAPABILITIES}})

    def test_preview_never_writes_then_explicit_confirmation_applies(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DataAuditLog.objects.exists())
        self.assertFalse(CrmAccess.objects.filter(user=self.target).exists())
        self.assertContains(response, 'Подтвердить изменение прав')
        token = response.context['confirmation']
        result = self.client.post(self.url, {'operation': 'apply', 'confirmation': token, 'call_visibility_ack': 'yes'})
        self.assertEqual(result.status_code, 302)
        self.assertTrue(CrmAccess.objects.get(user=self.target).view_leads)
        self.assertEqual(DataAuditLog.objects.filter(entity_type='crm_role_transition_v1').count(), 1)
        self.assertEqual(self.client.post(self.url, {'operation': 'apply', 'confirmation': token, 'call_visibility_ack': 'yes'}).status_code, 409)

    def test_missing_ack_tamper_stale_and_cross_target_do_not_apply(self):
        token = self.preview().context['confirmation']
        for data in ({'confirmation': token}, {'confirmation': token + 'tamper', 'call_visibility_ack': 'yes'}):
            response = self.client.post(self.url, {'operation': 'apply', **data})
            self.assertEqual(response.status_code, 409)
        StaffAccount.objects.filter(user=self.target).update(revision=1)
        self.assertEqual(self.client.post(self.url, {'operation': 'apply', 'confirmation': token, 'call_visibility_ack': 'yes'}).status_code, 409)
        self.assertFalse(DataAuditLog.objects.exists())

    def test_disabled_nonowner_and_csrf(self):
        with override_settings(CRM_ROLE_TRANSITION_ENABLED=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.target)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        client = Client(enforce_csrf_checks=True); client.force_login(self.owner)
        self.assertEqual(client.post(self.url, {'operation': 'preview'}).status_code, 403)

    def test_incomplete_review_does_not_generate_confirmation(self):
        response = self.client.post(self.url, {'operation': 'preview', 'roles': ['manager']})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('confirmation', response.context)
        self.assertFalse(DataAuditLog.objects.exists())

    def test_legacy_role_editor_redirects_owner_without_writing_and_denies_manager(self):
        url = reverse('staff_change_role', args=[self.access.pk])
        self.assertEqual(self.client.post(url, {'roles': ['manager']}).status_code, 302)
        self.assertEqual(OrganizationAccess.objects.get(pk=self.access.pk).role, 'service')
        self.client.force_login(self.target)
        self.assertEqual(self.client.post(url, {'roles': ['manager']}).status_code, 403)

    def test_admin_raw_grant_and_inline_writers_closed_in_opt_in_mode(self):
        request = RequestFactory().post('/admin/')
        request.user = get_user_model().objects.create_superuser('synthetic-admin', 'test@example.invalid', 'synthetic-password')
        inline = OrganizationAccessInline(Organization, admin.site)
        grants = admin.site._registry[CommunicationAccess]
        self.assertFalse(inline.has_add_permission(request, self.org))
        self.assertFalse(inline.has_change_permission(request, self.org))
        self.assertFalse(inline.has_delete_permission(request, self.org))
        self.assertFalse(grants.has_add_permission(request))
        self.assertFalse(grants.has_change_permission(request))
        self.assertFalse(grants.has_delete_permission(request))

    def test_cross_target_expiry_and_owner_revocation(self):
        from unittest.mock import patch
        from django.core import signing
        token = self.preview().context['confirmation']
        other = get_user_model().objects.create_user('other')
        OrganizationAccess.objects.create(organization=self.org, user=other, role='service')
        StaffAccount.objects.create(organization=self.org, user=other, kind='person')
        data = {'operation': 'apply', 'confirmation': token, 'call_visibility_ack': 'yes'}
        self.assertEqual(self.client.post(reverse('crm_role_review', args=[self.org.pk, other.pk]), data).status_code, 403)
        with patch('pool_service.crm_role_views._load_confirmation', side_effect=signing.SignatureExpired()):
            self.assertEqual(self.client.post(self.url, data).status_code, 409)
        OrganizationAccess.objects.filter(user=self.owner).delete()
        self.assertEqual(self.client.post(self.url, data).status_code, 403)
        OrganizationAccess.objects.create(organization=self.org, user=other, role='owner')
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, data).status_code, 403)
        self.assertFalse(DataAuditLog.objects.exists())

    def test_locked_legacy_action_rejects_stale_inactive_actor(self):
        from django.core.exceptions import PermissionDenied
        from pool_service.views import staff_delete, staff_toggle_block
        request = RequestFactory().post('/')
        request.user = self.owner
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        for view in (staff_delete, staff_toggle_block):
            with self.assertRaises(PermissionDenied):
                view(request, self.access.pk)
        self.assertTrue(OrganizationAccess.objects.filter(pk=self.access.pk).exists())
        self.target.refresh_from_db(); self.assertTrue(self.target.is_active)

    def test_forged_admin_grant_post_denied(self):
        technical = get_user_model().objects.create_superuser('technical', 'technical@example.invalid', 'synthetic-password')
        self.client.force_login(technical)
        grant = CommunicationAccess.objects.get(user=self.target)
        url = reverse('admin:pool_service_communicationaccess_change', args=[grant.pk])
        self.assertEqual(self.client.post(url, {'can_listen_calls': 'on', '_save': 'Save'}).status_code, 403)
        grant.refresh_from_db(); self.assertFalse(grant.can_listen_calls)

    def test_saved_explicit_deny_is_form_default_on_next_visit(self):
        from pool_service.services.crm_role_transition import prepare_role_transition, apply_role_transition
        ids = dict(actor_id=self.owner.pk, organization_id=self.org.pk, target_id=self.target.pk)
        overrides = dict.fromkeys(CAPABILITIES, 'inherit'); overrides['view_leads'] = 'deny'
        apply_role_transition(**ids, proposed_roles=['manager'], overrides=overrides,
            approved_increases=[f for f in CAPABILITIES if f != 'view_leads'],
            acknowledge_assigned_call_visibility=True, expected_digest=prepare_role_transition(**ids)['expected_digest'])
        response = self.client.get(self.url)
        self.assertEqual(response.context['form']['cap_view_leads'].value(), 'deny')

    def test_corrupt_policy_is_sanitized_and_denied_before_form_or_write(self):
        from django.db import connection
        from pool_service.crm_role_models import CrmRolePolicy
        response = self.preview()
        self.client.post(self.url, {'operation': 'apply', 'confirmation': response.context['confirmation'], 'call_visibility_ack': 'yes'})
        table = connection.ops.quote_name(CrmRolePolicy._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ' + table + ' SET overrides=%s WHERE user_id=%s', ['{}', self.target.pk])
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(DataAuditLog.objects.filter(entity_type='crm_role_transition_v1').count(), 1)

    def test_admin_cannot_reparent_unmanaged_grant_into_managed_scope(self):
        from django.core.exceptions import PermissionDenied
        from pool_service.services.crm_role_transition import prepare_role_transition, apply_role_transition
        ids = dict(actor_id=self.owner.pk, organization_id=self.org.pk, target_id=self.target.pk)
        apply_role_transition(**ids, proposed_roles=['manager'], overrides=dict.fromkeys(CAPABILITIES, 'inherit'),
            approved_increases=list(CAPABILITIES), acknowledge_assigned_call_visibility=True,
            expected_digest=prepare_role_transition(**ids)['expected_digest'])
        outsider = get_user_model().objects.create_user('outside')
        other = Organization.objects.create(name='Other')
        grant = CommunicationAccess.objects.create(organization=other, user=outsider)
        grant.organization_id = self.org.pk; grant.user_id = self.target.pk
        request = RequestFactory().post('/admin/'); request.user = self.owner
        with override_settings(CRM_ROLE_TRANSITION_ENABLED=False), self.assertRaises(PermissionDenied):
            admin.site._registry[CommunicationAccess].save_model(request, grant, None, True)
        grant.refresh_from_db(); self.assertEqual(grant.organization_id, other.pk)

    def test_admin_save_delete_and_inline_recheck_policy_after_permission_phase(self):
        from django.core.exceptions import PermissionDenied
        from types import SimpleNamespace
        from pool_service.services.crm_role_transition import prepare_role_transition, apply_role_transition
        ids = dict(actor_id=self.owner.pk, organization_id=self.org.pk, target_id=self.target.pk)
        apply_role_transition(**ids, proposed_roles=['manager'], overrides=dict.fromkeys(CAPABILITIES, 'inherit'),
            approved_increases=list(CAPABILITIES), acknowledge_assigned_call_visibility=True,
            expected_digest=prepare_role_transition(**ids)['expected_digest'])
        request = RequestFactory().post('/admin/'); request.user = self.owner
        grants = admin.site._registry[CommunicationAccess]
        grant = CommunicationAccess.objects.get(user=self.target)
        with override_settings(CRM_ROLE_TRANSITION_ENABLED=False):
            with self.assertRaises(PermissionDenied): grants.save_model(request, grant, None, True)
            with self.assertRaises(PermissionDenied): grants.delete_model(request, grant)
            with self.assertRaises(PermissionDenied): grants.delete_queryset(request, CommunicationAccess.objects.filter(pk=grant.pk))
            with self.assertRaises(PermissionDenied):
                admin.site._registry[Organization].save_formset(request, SimpleNamespace(instance=self.org), SimpleNamespace(model=OrganizationAccess, has_changed=lambda: True), True)
        self.assertTrue(CommunicationAccess.objects.filter(pk=grant.pk).exists())


    def test_admin_name_only_post_preserves_readonly_memberships(self):
        response = self.preview()
        self.client.post(self.url, {'operation': 'apply',
            'confirmation': response.context['confirmation'], 'call_visibility_ack': 'yes'})
        technical = get_user_model().objects.create_superuser(
            'name-editor', 'name-editor@example.invalid', 'synthetic-password')
        self.client.force_login(technical)
        url = reverse('admin:pool_service_organization_change', args=[self.org.pk])
        before = list(OrganizationAccess.objects.filter(organization=self.org).order_by('pk').values())
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(CRM_ROLE_TRANSITION_ENABLED=enabled):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                data = {'_save': 'Save'}
                forms = [response.context['adminform'].form]
                for inline in response.context['inline_admin_formsets']:
                    forms.append(inline.formset.management_form)
                    forms.extend(inline.formset.forms)
                for form in forms:
                    for field in form:
                        value = field.value()
                        if value is not None:
                            data[field.html_name] = value
                data['name'] = 'Renamed synthetic ' + str(enabled)
                result = self.client.post(url, data)
                self.assertEqual(result.status_code, 302)
                self.org.refresh_from_db()
                self.assertEqual(self.org.name, data['name'])
                self.assertEqual(list(OrganizationAccess.objects.filter(
                    organization=self.org).order_by('pk').values()), before)

    def test_changed_or_deleted_inline_still_rejected(self):
        from django.core.exceptions import PermissionDenied
        from types import SimpleNamespace
        request = RequestFactory().post('/admin/')
        request.user = get_user_model().objects.create_superuser(
            'inline-editor', 'inline-editor@example.invalid', 'synthetic-password')
        inline = OrganizationAccessInline(Organization, admin.site)
        with override_settings(CRM_ROLE_TRANSITION_ENABLED=False):
            formset_class = inline.get_formset(request, self.org)
            blank = formset_class(instance=self.org)
            prefix = blank.prefix
            data = {prefix + '-TOTAL_FORMS': str(blank.total_form_count()),
                prefix + '-INITIAL_FORMS': str(blank.initial_form_count())}
            for index, form in enumerate(blank.initial_forms):
                for field in form:
                    if field.value() is not None:
                        data[field.html_name] = field.value()
            for action in ('role', 'DELETE'):
                with self.subTest(action=action):
                    submitted = dict(data)
                    submitted[prefix + '-0-' + action] = 'installer' if action == 'role' else 'on'
                    formset = formset_class(submitted, instance=self.org)
                    self.assertTrue(formset.is_valid(), formset.errors)
                    self.assertTrue(formset.has_changed())
                    with override_settings(CRM_ROLE_TRANSITION_ENABLED=True), self.assertRaises(PermissionDenied):
                        admin.site._registry[Organization].save_formset(
                            request, SimpleNamespace(instance=self.org), formset, True)
        self.assertEqual(OrganizationAccess.objects.filter(organization=self.org).count(), 2)
