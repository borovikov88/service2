from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse


class HealthEndpointTests(TestCase):
    def test_liveness_is_public_and_sanitized(self):
        response = self.client.get(reverse("health_live"))

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ok"})
        self.assertEqual(response.headers["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private")

    def test_readiness_returns_ok_when_database_responds(self):
        with patch("service_site.health_views.connection.cursor") as cursor:
            context = cursor.return_value.__enter__.return_value

            response = self.client.get(reverse("health_ready"))

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ok"})
        context.execute.assert_called_once_with("SELECT 1")
        context.fetchone.assert_called_once_with()

    def test_readiness_hides_database_error_details(self):
        with patch(
            "service_site.health_views.connection.cursor",
            side_effect=DatabaseError("secret-host.example.invalid password=do-not-leak"),
        ):
            response = self.client.get(reverse("health_ready"))

        self.assertEqual(response.status_code, 503)
        self.assertJSONEqual(response.content, {"status": "unavailable"})
        self.assertNotIn(b"secret-host", response.content)
        self.assertNotIn(b"password", response.content)
