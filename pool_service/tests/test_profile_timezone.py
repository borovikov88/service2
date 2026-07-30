from datetime import datetime, timezone as datetime_timezone

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.template import Context, Template
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.middleware import TimezoneMiddleware
from pool_service.models import Organization, OrganizationAccess, Profile


class ProfileTimezoneTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
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

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_profile_timezone_is_used_for_datetime_output(self):
        profile = Profile.objects.get(user=self.user)
        profile.timezone = "Asia/Barnaul"
        profile.save(update_fields=["timezone"])

        utc_time = datetime(2026, 7, 30, 4, 53, tzinfo=datetime_timezone.utc)
        request = self.factory.get("/")
        request.user = User.objects.get(id=self.user.id)

        def get_response(_request):
            content = Template("{{ value|date:'d.m.Y H:i' }}").render(Context({"value": utc_time}))
            return HttpResponse(content)

        response = TimezoneMiddleware(get_response)(request)

        self.assertEqual(response.content.decode(), "30.07.2026 11:53")
