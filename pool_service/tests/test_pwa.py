import json

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(ALLOWED_HOSTS=["testserver", "rovikpool.ru"])
class PwaTests(TestCase):
    def test_manifest_has_installable_metadata(self):
        response = self.client.get(reverse("manifest"), HTTP_HOST="rovikpool.ru")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        manifest = json.loads(response.content)
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})

    def test_service_worker_is_available_at_root_scope(self):
        response = self.client.get(reverse("service_worker"), HTTP_HOST="rovikpool.ru")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")
        self.assertContains(response, "CACHE_VERSION = 'v10'")
        self.assertContains(response, "const OFFLINE_URLS = []")
        self.assertContains(response, "/finance/receipts/")
        self.assertContains(response, "event.request.mode === 'navigate'")
        self.assertContains(response, "return await fetch(event.request)")
        self.assertNotContains(response, "return cached || caches.match('/')")
        self.assertContains(response, "self.addEventListener('fetch'")

    def test_profile_contains_install_button_and_install_api(self):
        user = User.objects.create_user(username="pwa-user", password="pass")
        self.client.force_login(user)

        response = self.client.get(reverse("profile"), HTTP_HOST="rovikpool.ru")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pwa-install-profile-btn"')
        self.assertContains(response, "window.RovikPWA")
        self.assertNotContains(response, "getStoredInstalled")
