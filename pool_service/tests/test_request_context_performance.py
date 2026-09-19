from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pool_service.context_processors import plan_status_context
from pool_service.models import Organization, OrganizationAccess, Profile
from pool_service.services.finance import (
    can_access_finance_section,
    can_import_payroll,
    can_manage_employee_mapping,
    can_view_payroll_summary,
    finance_navigation,
)
from pool_service.services.permissions import (
    is_personal_free,
    is_personal_user,
    organization_for_user,
)


class RequestContextPerformanceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Performance Org",
            paid_until=timezone.now() + timedelta(days=90),
        )
        self.user = User.objects.create_user(
            username="perf-owner",
            password="pass",
            first_name="Perf",
            last_name="Owner",
        )
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="owner",
        )
        Profile.objects.get_or_create(user=self.user)
        self.factory = RequestFactory()

    def _fresh_user(self):
        return User.objects.select_related("profile").get(pk=self.user.pk)

    def test_company_identity_helpers_share_one_access_query(self):
        user = self._fresh_user()
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(is_personal_user(user))
            self.assertFalse(is_personal_free(user))
            self.assertEqual(organization_for_user(user), self.organization)

        access_queries = [
            query for query in queries.captured_queries
            if "organizationaccess" in query["sql"].lower()
        ]
        self.assertEqual(len(access_queries), 1)

    def test_finance_permissions_and_navigation_share_access_cache(self):
        user = self._fresh_user()
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(can_access_finance_section(user, self.organization))
            self.assertTrue(can_view_payroll_summary(user, self.organization))
            self.assertTrue(can_import_payroll(user, self.organization))
            self.assertTrue(can_manage_employee_mapping(user, self.organization))
            navigation = finance_navigation(user, self.organization)

        self.assertTrue(any(group["items"] for group in navigation))
        access_queries = [
            query for query in queries.captured_queries
            if "organizationaccess" in query["sql"].lower()
        ]
        self.assertEqual(len(access_queries), 1)

    def test_plan_context_does_not_repeat_access_queries(self):
        user = self._fresh_user()
        user._has_passkey_cache = False
        request = self.factory.get("/pools/")
        request.user = user
        request.session = {}
        request.resolver_match = None

        with CaptureQueriesContext(connection) as queries:
            context = plan_status_context(request)

        self.assertTrue(context["can_access_finance"])
        self.assertTrue(context["can_access_development"])
        access_queries = [
            query for query in queries.captured_queries
            if "organizationaccess" in query["sql"].lower()
        ]
        self.assertEqual(len(access_queries), 1)
