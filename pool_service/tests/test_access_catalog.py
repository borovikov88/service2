from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from pool_service.models import Organization, OrganizationAccess
from pool_service.services.access_catalog import ROLES, RULES, RULE_BY_KEY, SCOPES, role_scope
from pool_service.staff_call_models import StaffAccount


class AccessCatalogTests(SimpleTestCase):
    def test_action_keys_and_role_scopes_are_consistent(self):
        self.assertEqual(len(RULES), len(RULE_BY_KEY))
        for rule in RULES:
            self.assertLessEqual(set(rule.roles), set(ROLES))
            self.assertLessEqual(set(rule.roles.values()), set(SCOPES))
        self.assertEqual(role_scope('sales_head', 'sales.edit'), 'team')
        self.assertEqual(role_scope('manager', 'sales.read'), 'organization')
        self.assertEqual(role_scope('manager', 'sales.edit'), 'own')
        self.assertEqual(role_scope('manager', 'calls.leader'), 'responsible')
        self.assertEqual(role_scope('technical_admin', 'calls.download'), 'none')
        self.assertEqual(role_scope('manager', 'finance.payroll'), 'none')
        self.assertEqual(role_scope('owner', 'staff.roles'), 'explicit')
        self.assertEqual(role_scope('accountant', 'finance.report'), 'explicit')
        self.assertEqual(role_scope('accountant', 'finance.register'), 'register')
        self.assertEqual(role_scope('manager', 'sales.request_transfer'), 'own')
        self.assertEqual(role_scope('manager', 'sales.transfer'), 'none')
        self.assertEqual(role_scope('unknown', 'unknown'), 'none')


class AccessCatalogViewTests(TestCase):
    def test_matrix_is_director_only_and_does_not_change_role_assignments(self):
        user = get_user_model().objects.create_user('synthetic-catalog-director')
        org = Organization.objects.create(name='Synthetic organization')
        OrganizationAccess.objects.create(user=user, organization=org, role='owner')
        self.client.force_login(user)
        url = reverse('staff_role_catalog', args=[org.pk])
        self.assertContains(self.client.get(url, {'role':'sales_head'}), 'Свои и закреплённый отдел')
        self.assertEqual(self.client.post(url, {'role':'sales_head'}).status_code, 405)
        StaffAccount.objects.create(user=user, organization=org, kind='service')
        self.assertEqual(self.client.get(url).status_code, 403)
