import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from pool_service.forms import WaterReadingForm
from pool_service.models import Notification, PushSubscription
from pool_service.services.push_notifications import send_push_to_users


class WaterReadingFormTests(TestCase):
    def test_blank_reading_is_rejected(self):
        form = WaterReadingForm(
            data={
                "date": "2026-03-30T08:13:27",
                "temperature": "",
                "ph": "",
                "cl_free": "",
                "cl_total": "",
                "ph_dosing_station": "",
                "cl_free_dosing_station": "",
                "redox_dosing_station": "",
                "comment": "",
                "required_materials": "",
                "performed_works": "",
                "consumables_replaced": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Заполните хотя бы одно поле записи.", form.non_field_errors())


class PushDeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="push-user", password="pass")
        self.notification = Notification.objects.create(
            user=self.user,
            kind="limits",
            title="Показатели вне нормы",
            message="Тест",
            action_url="/pools/test/",
        )

    @override_settings(VAPID_PUBLIC_KEY="test-public", VAPID_PRIVATE_KEY="test-private")
    @patch("pool_service.services.push_notifications.webpush")
    def test_push_uses_subscription_host_for_url_and_icon(self, webpush_mock):
        PushSubscription.objects.create(
            user=self.user,
            endpoint="https://fcm.googleapis.com/fcm/send/service2",
            host="service2.aqualine22.ru",
            p256dh="k1",
            auth="a1",
        )
        PushSubscription.objects.create(
            user=self.user,
            endpoint="https://fcm.googleapis.com/fcm/send/rovik",
            host="rovikpool.ru",
            p256dh="k2",
            auth="a2",
        )

        sent = send_push_to_users(
            [self.user],
            title="Показатели вне нормы",
            message="Тест",
            action_url=self.notification.action_url,
            notification=self.notification,
        )

        self.assertEqual(sent, 2)
        self.assertEqual(webpush_mock.call_count, 2)
        payloads = [json.loads(call.kwargs["data"]) for call in webpush_mock.call_args_list]
        urls = {payload["url"] for payload in payloads}
        icons = {payload["icon"] for payload in payloads}
        self.assertTrue(any(url.startswith("https://service2.aqualine22.ru/notifications/open/") for url in urls))
        self.assertTrue(any(url.startswith("https://rovikpool.ru/notifications/open/") for url in urls))
        self.assertIn("https://service2.aqualine22.ru/static/assets/images/aqualine-favicon.png", icons)
        self.assertIn("https://rovikpool.ru/static/assets/images/rovikpool-favicon.png", icons)

    def test_signed_push_open_marks_notification_read(self):
        from django.core import signing

        token = signing.dumps({"notification_id": self.notification.id, "user_id": self.user.id})
        self.client.force_login(self.user)

        response = self.client.get(reverse("notification_push_open_token", kwargs={"token": token}))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.notification.action_url)
        self.notification.refresh_from_db()
        self.assertTrue(self.notification.is_read)
