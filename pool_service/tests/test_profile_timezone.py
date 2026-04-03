from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess, Profile


class ProfileTimezoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tzuser", password="pass")
        self.org = Organization.objects.create(
            name="TZ Org",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role="admin")

    def test_profile_timezone_can_be_updated(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("profile"),
            {
                "profile_settings": "1",
                "timezone": "Asia/Barnaul",
            },
        )

        self.assertEqual(response.status_code, 302)
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.timezone, "Asia/Barnaul")
