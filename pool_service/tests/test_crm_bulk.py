from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, CrmItem, Organization, OrganizationAccess, Pool, ServiceTask
from pool_service.services.crm_locking import lock_crm_graph as real_lock_crm_graph


class CrmBulkUpdateTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Bulk CRM Org",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        self.admin = User.objects.create_user(username="bulk_admin", password="pass")
        self.manager = User.objects.create_user(username="bulk_manager", password="pass")
        self.service = User.objects.create_user(username="bulk_service", password="pass")
        self.new_responsible = User.objects.create_user(username="bulk_new_resp", password="pass")

        OrganizationAccess.objects.create(user=self.admin, organization=self.organization, role="admin")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.service, organization=self.organization, role="service")
        OrganizationAccess.objects.create(user=self.new_responsible, organization=self.organization, role="manager")

        self.client_record = Client.objects.create(
            organization=self.organization,
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Детский сад № 250",
            phone="+79000000000",
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            address="ул. Попова, 182",
            organization=self.organization,
        )
        self.item_a = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Хлор"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_NEW,
            responsible=self.manager,
            created_by=self.service,
        )
        self.item_b = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Фильтр"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_NEW,
            responsible=self.manager,
            created_by=self.service,
        )
        self.task = ServiceTask.objects.create(
            organization=self.organization,
            title="Поставка материалов: Детский сад № 250",
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
            task_type=ServiceTask.TYPE_SUPPLY_REQUEST,
            status=ServiceTask.STATUS_NEW,
            client=self.client_record,
            pool=self.pool,
            crm_item=self.item_a,
            primary_responsible=self.manager,
            created_by=self.service,
        )
        self.task.responsibles.add(self.service, self.manager)

    def test_bulk_stage_update_updates_selected_items_and_linked_task(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("crm_bulk_update", kwargs={"direction": CrmItem.DIRECTION_SERVICE}),
            {
                "item_ids": [self.item_a.id, self.item_b.id],
                "bulk_action": "set_stage",
                "bulk_stage": CrmItem.STAGE_SERVICE_DONE,
                "return_query": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.item_a.stage, CrmItem.STAGE_SERVICE_DONE)
        self.assertEqual(self.item_b.stage, CrmItem.STAGE_SERVICE_DONE)
        self.assertTrue(self.item_a.is_archived)
        self.assertEqual(self.item_a.archived_reason, CrmItem.ARCHIVE_REASON_COMPLETED)
        self.assertTrue(self.item_b.is_archived)
        self.assertEqual(self.item_b.archived_reason, CrmItem.ARCHIVE_REASON_COMPLETED)
        self.assertTrue(self.task.is_archived)
        self.assertEqual(self.task.archived_reason, ServiceTask.ARCHIVE_REASON_COMPLETED)

    def test_bulk_responsible_update_updates_selected_items_and_linked_task(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("crm_bulk_update", kwargs={"direction": CrmItem.DIRECTION_SERVICE}),
            {
                "item_ids": [self.item_a.id, self.item_b.id],
                "bulk_action": "set_responsible",
                "bulk_responsible": str(self.new_responsible.id),
                "return_query": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.item_a.responsible, self.new_responsible)
        self.assertEqual(self.item_b.responsible, self.new_responsible)
        self.assertEqual(self.task.primary_responsible, self.new_responsible)


    @patch("pool_service.views.lock_crm_graph", wraps=real_lock_crm_graph)
    def test_task_bulk_update_uses_shared_crm_graph_lock_order(self, lock_graph):
        second_task = ServiceTask.objects.create(
            organization=self.organization,
            title="Вторая CRM-задача",
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            status=ServiceTask.STATUS_NEW,
            client=self.client_record,
            pool=self.pool,
            crm_item=self.item_b,
            primary_responsible=self.manager,
            created_by=self.service,
        )
        second_task.responsibles.add(self.manager)

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("crm_tasks_bulk_update"),
            {
                "task_ids": [self.task.id, second_task.id],
                "bulk_action": "set_status",
                "bulk_status": ServiceTask.STATUS_IN_PROGRESS,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        second_task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_IN_PROGRESS)
        self.assertEqual(second_task.status, ServiceTask.STATUS_IN_PROGRESS)

        lock_graph.assert_called_once()
        args, kwargs = lock_graph.call_args
        self.assertEqual(set(args[0]), {self.item_a.id, self.item_b.id})
        self.assertEqual(
            set(kwargs["extra_task_ids"]),
            {self.task.id, second_task.id},
        )
