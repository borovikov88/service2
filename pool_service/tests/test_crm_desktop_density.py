from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess


class CrmDesktopDensityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Aqualine CRM UI",
            plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.user = User.objects.create_user(
            username="crm_ui_manager",
            password="test-pass-123",
        )
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="manager",
        )
        self.client.force_login(self.user)

    def test_crm_index_uses_compact_desktop_cards(self):
        response = self.client.get(reverse("crm_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "crm-index-card")
        self.assertContains(response, "crm-index-grid crm-index-primary", html=False)
        self.assertContains(response, "@media (min-width: 992px)", html=False)

    def test_crm_service_list_uses_compact_desktop_table(self):
        response = self.client.get(reverse("crm_list", kwargs={"direction": "service"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "crm-table-wrap")
        self.assertContains(response, "Desktop CRM density")
        self.assertContains(response, "min-height: 34px", html=False)

    def test_crm_tasks_use_compact_desktop_table(self):
        response = self.client.get(reverse("crm_tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "crm-tasks-table")
        self.assertContains(response, "Desktop task density")
        self.assertContains(response, "min-height: 34px", html=False)

    def test_crm_create_form_uses_compact_desktop_form(self):
        response = self.client.get(reverse("crm_create", kwargs={"direction": "service"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "crm-form-page")
        self.assertContains(response, "crm-form-card")
        self.assertContains(response, "crm-form-actions")

    def test_crm_detail_uses_compact_desktop_cards(self):
        from pool_service.models import CrmItem

        item = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title="Тестовая заявка",
            stage=CrmItem.STAGE_SERVICE_NEW,
            created_by=self.user,
        )
        response = self.client.get(
            reverse("crm_view", kwargs={"direction": "service", "item_id": item.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "crm-view-shell")
        self.assertContains(response, "@media (min-width: 992px)", html=False)
        self.assertContains(response, "border-radius: 10px", html=False)
        self.assertContains(response, "align-content: start", html=False)

