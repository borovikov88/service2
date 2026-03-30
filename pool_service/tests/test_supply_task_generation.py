from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, CrmItem, Notification, Organization, OrganizationAccess, Pool, ServiceTask, WaterReading
from pool_service.services.task_generation import (
    create_supply_task_from_reading,
    sync_crm_item_for_task,
    sync_task_with_crm_item,
)


class SupplyTaskGenerationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Supply Org",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        self.service_user = User.objects.create_user(username="service", password="pass")
        self.manager = User.objects.create_user(username="manager", password="pass")
        self.manager_two = User.objects.create_user(username="manager_two", password="pass")
        self.admin = User.objects.create_user(username="admin_user", password="pass")
        OrganizationAccess.objects.create(user=self.service_user, organization=self.organization, role="service")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.manager_two, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.admin, organization=self.organization, role="admin")
        self.client_user = User.objects.create_user(username="client", password="pass")
        self.pool_client = Client.objects.create(
            user=self.client_user,
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Детский сад № 250",
            phone="+79000000000",
            organization=self.organization,
        )
        self.pool = Pool.objects.create(
            client=self.pool_client,
            address="ул. Попова, 182",
            organization=self.organization,
        )

    def test_supply_task_created_from_reading_signal(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            temperature=31.5,
            required_materials="Хлор, коагулянт",
        )

        task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)

        self.assertEqual(task.organization, self.organization)
        self.assertEqual(task.pool, self.pool)
        self.assertEqual(task.client, self.pool_client)
        self.assertEqual(task.created_by, self.service_user)
        self.assertEqual(task.primary_responsible, self.manager)
        self.assertTrue(task.auto_created)
        self.assertEqual(task.source_type, ServiceTask.SOURCE_SERVICE_STAFF)
        self.assertEqual(task.status, ServiceTask.STATUS_NEW)
        self.assertIn("Хлор", task.description)
        self.assertEqual(set(task.responsibles.values_list("id", flat=True)), {self.service_user.id, self.manager.id})
        self.assertIsNotNone(task.crm_item)
        self.assertEqual(task.crm_item.direction, CrmItem.DIRECTION_SERVICE)
        self.assertEqual(task.crm_item.pool, self.pool)
        self.assertEqual(task.crm_item.responsible, self.manager)
        self.assertEqual(task.crm_item.title, f'Заявка: "{reading.required_materials}"')

    def test_pool_staff_reading_assigns_all_managers(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.client_user,
            date=timezone.now(),
            required_materials="Хлор",
        )

        task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)

        self.assertEqual(task.source_type, ServiceTask.SOURCE_POOL_STAFF)
        self.assertEqual(
            set(task.responsibles.values_list("id", flat=True)),
            {self.client_user.id, self.manager.id, self.manager_two.id},
        )
        self.assertTrue(
            Notification.objects.filter(
                user=self.admin,
                kind="task_assignment",
                action_url=f"/tasks/{task.id}/",
            ).exists()
        )

    def test_supply_task_not_duplicated_for_same_reading(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=7.1,
            required_materials="Хлор",
        )

        first_task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)
        second_task = create_supply_task_from_reading(reading)

        self.assertEqual(first_task.id, second_task.id)
        self.assertEqual(
            ServiceTask.objects.filter(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST).count(),
            1,
        )
        self.assertEqual(CrmItem.objects.filter(pool=self.pool, title=f'Заявка: "{reading.required_materials}"').count(), 1)

    def test_manager_can_access_crm_index(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("crm_index"))

        self.assertEqual(response.status_code, 200)

    def test_admin_can_open_supply_task_without_being_responsible(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.client_user,
            date=timezone.now(),
            required_materials="Коагулянт",
        )
        task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)

        self.client.force_login(self.admin)
        response = self.client.get(reverse("task_edit", kwargs={"task_id": task.id}))

        self.assertEqual(response.status_code, 200)

    def test_task_creator_can_open_supply_task_without_being_responsible(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.client_user,
            date=timezone.now(),
            required_materials="Альгицид",
        )
        task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)
        task.responsibles.set([self.manager, self.manager_two])

        self.client.force_login(self.client_user)
        response = self.client.get(reverse("task_edit", kwargs={"task_id": task.id}))

        self.assertEqual(response.status_code, 200)

    def test_sync_crm_stage_when_task_completed_and_reopened(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            required_materials="Хлор",
        )
        task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)

        task.completed_at = timezone.now()
        task.save(update_fields=["completed_at", "updated_at"])
        sync_crm_item_for_task(task)
        task.crm_item.refresh_from_db()
        self.assertEqual(task.crm_item.stage, CrmItem.STAGE_SERVICE_DONE)

        task.completed_at = None
        task.save(update_fields=["completed_at", "updated_at"])
        sync_crm_item_for_task(task)
        task.crm_item.refresh_from_db()
        self.assertEqual(task.crm_item.stage, CrmItem.STAGE_SERVICE_IN_PROGRESS)

    def test_sync_task_completion_from_crm_stage(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            required_materials="Датчик хлора",
        )
        task = ServiceTask.objects.get(water_reading=reading, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)

        task.crm_item.stage = CrmItem.STAGE_SERVICE_DONE
        task.crm_item.save(update_fields=["stage", "updated_at"])
        sync_task_with_crm_item(task)
        task.refresh_from_db()
        self.assertIsNotNone(task.completed_at)

        task.crm_item.stage = CrmItem.STAGE_SERVICE_IN_PROGRESS
        task.crm_item.save(update_fields=["stage", "updated_at"])
        sync_task_with_crm_item(task)
        task.refresh_from_db()
        self.assertIsNone(task.completed_at)
