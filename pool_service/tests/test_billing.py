from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Notification, Organization, OrganizationAccess, OrganizationPaymentRequest


class BillingFlowTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username="admin", email="", password="pass12345")
        self.owner = User.objects.create_user(username="owner", password="pass12345")
        self.organization = Organization.objects.create(
            name="Test Org",
            trial_started_at=timezone.now(),
        )
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )

    def test_billing_request_creates_superuser_notification(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("billing_request"),
            {"months": "3", "note": "Оплата согласована"},
        )

        self.assertRedirects(response, reverse("billing"))
        payment_request = OrganizationPaymentRequest.objects.get()
        self.assertEqual(payment_request.organization, self.organization)
        self.assertEqual(payment_request.months, 3)
        self.assertEqual(payment_request.status, OrganizationPaymentRequest.STATUS_PENDING)

        notification = Notification.objects.get(user=self.superuser)
        self.assertEqual(notification.kind, "billing_request")
        self.assertEqual(notification.action_url, reverse("billing_admin"))
        self.assertIn(self.organization.name, notification.message)
        self.assertIn("3 мес", notification.message)

    def test_billing_admin_requires_superuser(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("billing_admin"))

        self.assertEqual(response.status_code, 403)

    def test_billing_admin_shows_pending_request_for_superuser(self):
        OrganizationPaymentRequest.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            months=6,
            status=OrganizationPaymentRequest.STATUS_PENDING,
        )
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("billing_admin"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.organization.name)
        self.assertContains(response, "6")

    def test_billing_admin_approve_updates_paid_until(self):
        payment_request = OrganizationPaymentRequest.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            months=1,
            status=OrganizationPaymentRequest.STATUS_PENDING,
        )
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("billing_admin"),
            {"action": "approve_request", "request_id": str(payment_request.id)},
        )

        self.assertRedirects(response, reverse("billing_admin"))
        payment_request.refresh_from_db()
        self.organization.refresh_from_db()
        self.assertEqual(payment_request.status, OrganizationPaymentRequest.STATUS_APPROVED)
        self.assertEqual(payment_request.decided_by, self.superuser)
        self.assertIsNotNone(payment_request.paid_until_after)
        self.assertEqual(self.organization.plan_type, Organization.PLAN_COMPANY_PAID)
        self.assertIsNotNone(self.organization.paid_until)
