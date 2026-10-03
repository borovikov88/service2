from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from pool_service.theme import THEME_COOKIE_MAX_AGE, THEME_COOKIE_NAME


class ThemePreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="theme-user", password="pass")
        self.client.force_login(self.user)

    def test_profile_defaults_to_auto_theme(self):
        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-preference="auto"')
        self.assertContains(response, 'name="theme" value="auto" checked')
        self.assertContains(response, "assets/css/theme.css")

    def test_theme_preference_is_saved_in_device_cookie(self):
        response = self.client.post(
            reverse("profile"),
            {
                "theme_settings": "1",
                "theme": "dark",
            },
        )

        self.assertEqual(response.status_code, 302)
        cookie = response.cookies[THEME_COOKIE_NAME]
        self.assertEqual(cookie.value, "dark")
        self.assertEqual(int(cookie["max-age"]), THEME_COOKIE_MAX_AGE)
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["httponly"])

        follow_up = self.client.get(reverse("profile"))
        self.assertContains(follow_up, 'data-theme-preference="dark"')
        self.assertContains(follow_up, 'name="theme" value="dark" checked')

    def test_invalid_theme_is_rejected_without_setting_cookie(self):
        response = self.client.post(
            reverse("profile"),
            {
                "theme_settings": "1",
                "theme": "neon",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(THEME_COOKIE_NAME, response.cookies)
