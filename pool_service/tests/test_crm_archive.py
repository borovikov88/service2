from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from io import StringIO

from pool_service.models import Client, CrmItem, Organization, OrganizationAccess, Pool, ServiceTask


class CrmArchiveTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Archive CRM Org",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        self.admin = User.objects.create_user(username="archive_admin", password="pass")
        self.manager = User.objects.create_user(username="archive_manager", password="pass")
        self.service = User.objects.create_user(username="archive_service", password="pass")

        OrganizationAccess.objects.create(user=self.admin, organization=self.organization, role="admin")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.service, organization=self.organization, role="service")

        self.client_record = Client.objects.create(
            organization=self.organization,
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Школа № 137",
            phone="+79000000000",
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            address="ул. Шумакова, 78",
            organization=self.organization,
        )
        self.item = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Насос"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_IN_PROGRESS,
            responsible=self.manager,
            created_by=self.service,
        )
        self.task = ServiceTask.objects.create(
            organization=self.organization,
            title="Заявка Школа № 137: насос",
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
            task_type=ServiceTask.TYPE_SUPPLY_REQUEST,
            status=ServiceTask.STATUS_NEW,
            client=self.client_record,
            pool=self.pool,
            crm_item=self.item,
            primary_responsible=self.manager,
            created_by=self.service,
        )
        self.task.responsibles.add(self.service, self.manager)

    def test_bulk_archive_crm_item_archives_linked_task(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("crm_bulk_update", kwargs={"direction": CrmItem.DIRECTION_SERVICE}),
            {
                "item_ids": [self.item.id],
                "bulk_action": "archive",
                "return_query": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.task.refresh_from_db()
        self.assertTrue(self.item.is_archived)
        self.assertEqual(self.item.archived_reason, CrmItem.ARCHIVE_REASON_DELETED)
        self.assertTrue(self.task.is_archived)
        self.assertEqual(self.task.archived_reason, ServiceTask.ARCHIVE_REASON_DELETED)

    def test_archive_page_lists_archived_entities_and_can_restore(self):
        self.item.is_archived = True
        self.item.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.item.archived_at = timezone.now()
        self.item.archived_by = self.admin
        self.item.save(update_fields=["is_archived", "archived_reason", "archived_at", "archived_by", "updated_at"])

        self.task.is_archived = True
        self.task.archived_reason = ServiceTask.ARCHIVE_REASON_DELETED
        self.task.archived_at = timezone.now()
        self.task.archived_by = self.admin
        self.task.save(update_fields=["is_archived", "archived_reason", "archived_at", "archived_by", "updated_at"])

        self.client.force_login(self.admin)
        response = self.client.get(reverse("archive_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Архив")
        self.assertContains(response, "Насос")
        self.assertContains(response, self.task.title)

        restore_response = self.client.post(reverse("archive_restore_crm_item", kwargs={"item_id": self.item.id}))
        self.assertEqual(restore_response.status_code, 302)
        self.item.refresh_from_db()
        self.task.refresh_from_db()
        self.assertFalse(self.item.is_archived)
        self.assertFalse(self.task.is_archived)

    def test_archive_bulk_restore_restores_deleted_task_and_crm(self):
        self.item.is_archived = True
        self.item.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.item.archived_at = timezone.now()
        self.item.archived_by = self.admin
        self.item.save(update_fields=["is_archived", "archived_reason", "archived_at", "archived_by", "updated_at"])

        self.task.is_archived = True
        self.task.archived_reason = ServiceTask.ARCHIVE_REASON_DELETED
        self.task.archived_at = timezone.now()
        self.task.archived_by = self.admin
        self.task.save(update_fields=["is_archived", "archived_reason", "archived_at", "archived_by", "updated_at"])

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("archive_bulk_update"),
            {
                "archive_action": "restore",
                "task_ids": [self.task.id],
                "item_ids": [self.item.id],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.task.refresh_from_db()
        self.assertFalse(self.item.is_archived)
        self.assertFalse(self.task.is_archived)

    def test_archive_bulk_delete_forever_deletes_only_deleted_records(self):
        self.item.is_archived = True
        self.item.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.item.archived_at = timezone.now()
        self.item.save(update_fields=["is_archived", "archived_reason", "archived_at", "updated_at"])

        self.task.is_archived = True
        self.task.archived_reason = ServiceTask.ARCHIVE_REASON_DELETED
        self.task.archived_at = timezone.now()
        self.task.save(update_fields=["is_archived", "archived_reason", "archived_at", "updated_at"])

        item_id = self.item.id
        task_id = self.task.id

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("archive_bulk_update"),
            {
                "archive_action": "delete_forever",
                "task_ids": [task_id],
                "item_ids": [item_id],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CrmItem.objects.filter(id=item_id).exists())
        self.assertFalse(ServiceTask.objects.filter(id=task_id).exists())

    def test_cleanup_archive_command_removes_only_deleted_older_than_threshold(self):
        old_deleted_item = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Старый насос"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_IN_PROGRESS,
            responsible=self.manager,
            created_by=self.service,
            is_archived=True,
            archived_reason=CrmItem.ARCHIVE_REASON_DELETED,
            archived_at=timezone.now() - timedelta(days=31),
        )
        old_deleted_task = ServiceTask.objects.create(
            organization=self.organization,
            title="Старая удалённая задача",
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
            task_type=ServiceTask.TYPE_MANUAL,
            status=ServiceTask.STATUS_NEW,
            is_archived=True,
            archived_reason=ServiceTask.ARCHIVE_REASON_DELETED,
            archived_at=timezone.now() - timedelta(days=31),
            created_by=self.service,
        )
        completed_item = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Выполненная"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_DONE,
            responsible=self.manager,
            created_by=self.service,
            is_archived=True,
            archived_reason=CrmItem.ARCHIVE_REASON_COMPLETED,
            archived_at=timezone.now() - timedelta(days=60),
        )
        completed_task = ServiceTask.objects.create(
            organization=self.organization,
            title="Старая выполненная задача",
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
            task_type=ServiceTask.TYPE_MANUAL,
            status=ServiceTask.STATUS_DONE,
            is_archived=True,
            archived_reason=ServiceTask.ARCHIVE_REASON_COMPLETED,
            archived_at=timezone.now() - timedelta(days=60),
            completed_at=timezone.now() - timedelta(days=60),
            created_by=self.service,
        )

        output = StringIO()
        call_command("cleanup_archive", stdout=output)

        self.assertFalse(CrmItem.objects.filter(id=old_deleted_item.id).exists())
        self.assertFalse(ServiceTask.objects.filter(id=old_deleted_task.id).exists())
        self.assertTrue(CrmItem.objects.filter(id=completed_item.id).exists())
        self.assertTrue(ServiceTask.objects.filter(id=completed_task.id).exists())
        self.assertIn("Удалено из архива", output.getvalue())

    def test_cleanup_archive_dry_run_does_not_delete_records(self):
        item = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Dry run"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_IN_PROGRESS,
            responsible=self.manager,
            created_by=self.service,
            is_archived=True,
            archived_reason=CrmItem.ARCHIVE_REASON_DELETED,
            archived_at=timezone.now() - timedelta(days=31),
        )
        task = ServiceTask.objects.create(
            organization=self.organization,
            title="Dry run task",
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
            task_type=ServiceTask.TYPE_MANUAL,
            status=ServiceTask.STATUS_NEW,
            is_archived=True,
            archived_reason=ServiceTask.ARCHIVE_REASON_DELETED,
            archived_at=timezone.now() - timedelta(days=31),
            created_by=self.service,
        )

        output = StringIO()
        call_command("cleanup_archive", "--dry-run", stdout=output)

        self.assertTrue(CrmItem.objects.filter(id=item.id).exists())
        self.assertTrue(ServiceTask.objects.filter(id=task.id).exists())
        self.assertIn("Dry run", output.getvalue())
