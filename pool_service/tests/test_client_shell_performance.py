from datetime import timedelta

from django.contrib.auth.models import User
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess
from pool_service.services.finance import finance_navigation


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1", "service2.aqualine22.ru", "rovikpool.ru"])
class ClientShellPerformanceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Shell Performance",
            paid_until=timezone.now() + timedelta(days=60),
        )
        self.user = User.objects.create_user(
            username="shell-owner",
            password="pass",
        )
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="owner",
        )
        self.client.force_login(self.user)

    def test_internal_app_uses_cached_shell_assets_without_metrika(self):
        response = self.client.get(
            reverse("pool_list"),
            HTTP_HOST="service2.aqualine22.ru",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "assets/css/base-shell.css")
        self.assertContains(response, "assets/js/base-shell-pre.js")
        self.assertContains(response, "assets/js/base-shell-post-a.js")
        self.assertContains(response, "assets/js/base-shell-post-b.js")
        self.assertNotContains(response, "mc.yandex.ru/metrika")

    def test_desktop_sidebar_submenus_expand_without_navigation(self):
        response = self.client.get(
            reverse("pool_list"),
            HTTP_HOST="service2.aqualine22.ru",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="desktop-sidebar__group"', count=2)
        self.assertContains(response, "Обзор CRM")
        self.assertContains(response, "АНАЛИТИКА")

        source = get_template("pool_service/base.html").template.source
        self.assertIn('class="desktop-sidebar__group"', source)
        self.assertIn("<summary", source)
        self.assertIn("desktop-sidebar__group-chevron", source)

    def test_gross_profit_navigation_opens_current_month_by_default(self):
        navigation = finance_navigation(self.user, self.organization)
        item = next(
            item
            for group in navigation
            for item in group["items"]
            if item["route_name"] == "finance_onec_profit_dashboard"
        )

        self.assertEqual(
            item["url"],
            f"{reverse('finance_onec_profit_dashboard')}?period=current_month",
        )

        response = self.client.get(
            reverse("pool_list"),
            HTTP_HOST="service2.aqualine22.ru",
        )
        self.assertContains(response, item["url"])

    def test_public_indexable_host_keeps_metrika(self):
        response = self.client.get(
            reverse("pool_list"),
            HTTP_HOST="rovikpool.ru",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mc.yandex.ru/metrika")

    def test_base_template_does_not_regress_to_large_inline_shell(self):
        source = get_template("pool_service/base.html").template.source
        self.assertLess(len(source), 100_000)
