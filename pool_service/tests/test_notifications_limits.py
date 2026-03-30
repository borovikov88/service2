from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, Notification, Organization, OrganizationAccess, Pool, WaterReading
from pool_service.services.notifications import notify_reading_out_of_range


class LimitsNotificationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Аквалайн",
            city="Москва",
            trial_started_at=timezone.now(),
            notify_limits_pool_staff=True,
            notify_limits_pool_staff_push=True,
            notify_limits_service_staff=True,
            notify_limits_service_staff_push=True,
        )
        self.org_admin = User.objects.create_user(username="orgadmin", password="pass")
        self.service_user = User.objects.create_user(username="serviceuser", password="pass")
        self.pool_staff_user = User.objects.create_user(username="poolstaff", password="pass")
        OrganizationAccess.objects.create(user=self.org_admin, organization=self.org, role="admin")
        OrganizationAccess.objects.create(user=self.service_user, organization=self.org, role="service")
        self.client = Client.objects.create(
            user=self.pool_staff_user,
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Тест Клиент",
            phone="+79000000000",
            organization=self.org,
        )
        self.pool = Pool.objects.create(client=self.client, address="Тестовый бассейн", organization=self.org)

    def test_out_of_range_reading_from_org_employee_notifies_org_admin(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=6.4,
        )

        notify_reading_out_of_range(reading)

        self.assertTrue(Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).exists())

    def test_out_of_range_reading_create_signal_notifies_org_admin(self):
        WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=6.4,
        )

        self.assertTrue(Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).exists())

    def test_out_of_range_reading_from_pool_staff_notifies_org_admin(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.pool_staff_user,
            date=timezone.now(),
            ph=6.4,
        )

        notify_reading_out_of_range(reading)

        self.assertTrue(Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).exists())

    def test_limits_notification_uses_human_readable_message(self):
        self.client.name = "Школа № 137"
        self.client.save(update_fields=["name"])
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            cl_free=5.0,
        )

        notify_reading_out_of_range(reading)

        notification = Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).latest("id")
        self.assertEqual(notification.message, 'В "Школе № 137" высокий уровень свободного хлора - 5.0')

    def test_service_staff_limits_can_be_disabled_separately(self):
        self.org.notify_limits_service_staff = False
        self.org.notify_limits_service_staff_push = False
        self.org.save(update_fields=["notify_limits_service_staff", "notify_limits_service_staff_push"])
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=6.4,
        )

        created = notify_reading_out_of_range(reading)

        self.assertFalse(created)
        self.assertFalse(Notification.objects.filter(kind="limits", pool=self.pool).exists())


class NotificationDisplayFormattingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Display Org",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        self.user = User.objects.create_user(username="viewer", password="pass")
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role="admin")
        client_user = User.objects.create_user(username="poolviewer", password="pass")
        self.pool_client = Client.objects.create(
            user=client_user,
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Школа № 137",
            phone="+79000000001",
            organization=self.org,
        )
        self.pool = Pool.objects.create(client=self.pool_client, address="ул. Шумакова, 78", organization=self.org)

    def test_notifications_page_strips_legacy_task_prefix(self):
        Notification.objects.create(
            user=self.user,
            title="Новая заявка",
            message='Вас добавили участником в задачу "Заявка Школа № 137: реагент"',
            kind="task_assignment",
            action_url="/tasks/123/",
            organization=self.org,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications"))

        task_note = response.context["task_notifications"][0]
        self.assertEqual(task_note.task_title, "Новая заявка Школа № 137")
        self.assertEqual(task_note.task_details, "Реагент")

    def test_notifications_page_strips_duplicated_pool_name_for_limits(self):
        Notification.objects.create(
            user=self.user,
            title="Показатели вне нормы",
            message='"Школа № 137" В "Школе № 137" высокий уровень свободного хлора - 3.65',
            kind="limits",
            action_url=reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}),
            organization=self.org,
            pool=self.pool,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications"))

        note = response.context["deviation_notifications"][0]
        self.assertEqual(note.deviation_title, "Школа № 137")
        self.assertEqual(note.deviation_details, "Высокий уровень свободного хлора - 3.65")
