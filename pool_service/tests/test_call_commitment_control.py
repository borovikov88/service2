from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from pool_service.models import Client, Employee, Notification, Organization, OrganizationAccess, ServiceTask
from pool_service.services.call_commitment_control import (
    _control_key, _deliver_control_push, _effective_deadline, process_call_commitment_controls,
)


BARNAUL = ZoneInfo("Asia/Barnaul")


@override_settings(COMMUNICATION_TIME_ZONE="Asia/Barnaul")
class CallCommitmentControlTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Control org")
        self.owner = User.objects.create_user("owner-control", password="test")
        self.manager = User.objects.create_user("manager-control", password="test")
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )
        OrganizationAccess.objects.create(
            user=self.manager,
            organization=self.organization,
            role="manager",
        )
        self.client_entity = Client.objects.create(
            organization=self.organization,
            name="Клиент контроля",
            phone="+79000000001",
        )

    def _task(
        self,
        *,
        actor="employee",
        deadline=None,
        priority=ServiceTask.PRIORITY_NORMAL,
        needs_due_date=False,
    ):
        deadline = deadline or datetime(2026, 10, 6, 10, 0, tzinfo=BARNAUL)
        end_date = None if needs_due_date else deadline.date()
        end_time = None if needs_due_date else deadline.time().replace(tzinfo=None)
        task = ServiceTask.objects.create(
            organization=self.organization,
            title=(
                "Ждём клиента: Оплатить счёт"
                if actor == "client"
                else "Отправить клиенту расчёт"
            ),
            description="Тестовая договорённость из звонка",
            start_date=deadline.date(),
            end_date=end_date,
            start_time=end_time,
            end_time=end_time,
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_SYSTEM,
            status=(
                ServiceTask.STATUS_WAITING
                if actor == "client"
                else ServiceTask.STATUS_NEW
            ),
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            priority=priority,
            client=self.client_entity,
            primary_responsible=self.manager,
            auto_created=True,
            is_editable=True,
            payload_json={
                "source": "call_analysis",
                "source_call_id": 777,
                "commitment_index": 0,
                "actor": actor,
                "kind": "payment" if actor == "client" else "proposal",
                "confidence": "high",
                "needs_due_date": needs_due_date,
            },
        )
        task.responsibles.add(self.manager)
        return task

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_employee_due_reminder_is_sent_once(self, _send_push):
        task = self._task()
        now = datetime(2026, 10, 6, 10, 15, tzinfo=BARNAUL)

        first = process_call_commitment_controls(now=now)
        second = process_call_commitment_controls(now=now)

        self.assertEqual(first["due_reminders"], 1)
        self.assertEqual(second["due_reminders"], 0)
        notifications = Notification.objects.filter(
            user=self.manager,
            dedupe_key__startswith=f"call_control:{task.id}:due:",
        )
        self.assertEqual(notifications.count(), 1)
        self.assertEqual(notifications.get().title, "Срок задачи наступил")
        task.refresh_from_db()
        self.assertTrue(task.payload_json["control_state"]["due_reminder_sent_at"])

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_client_promise_notifies_responsible_after_deadline(self, _send_push):
        task = self._task(actor="client")
        now = datetime(2026, 10, 6, 10, 15, tzinfo=BARNAUL)

        result = process_call_commitment_controls(now=now)

        self.assertEqual(result["due_reminders"], 1)
        notification = Notification.objects.get(
            user=self.manager,
            dedupe_key__startswith=f"call_control:{task.id}:due:",
        )
        self.assertEqual(notification.title, "Проверьте обещание клиента")
        self.assertFalse(
            Notification.objects.filter(
                user=self.owner,
                dedupe_key__startswith=f"call_control:{task.id}:escalation:",
            ).exists()
        )

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_normal_overdue_commitment_escalates_to_owner_after_24_hours(self, _send_push):
        task = self._task()
        now = datetime(2026, 10, 7, 11, 0, tzinfo=BARNAUL)

        first = process_call_commitment_controls(now=now)
        second = process_call_commitment_controls(now=now)

        self.assertEqual(first["due_reminders"], 1)
        self.assertEqual(first["escalations"], 1)
        self.assertEqual(second["escalations"], 0)
        escalation = Notification.objects.get(
            user=self.owner,
            dedupe_key__startswith=f"call_control:{task.id}:escalation:",
        )
        self.assertEqual(escalation.level, "critical")
        self.assertEqual(escalation.title, "Просрочена договорённость")

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_important_commitment_escalates_after_four_hours(self, _send_push):
        task = self._task(priority=ServiceTask.PRIORITY_HIGH)
        now = datetime(2026, 10, 6, 14, 5, tzinfo=BARNAUL)

        result = process_call_commitment_controls(now=now)

        self.assertEqual(result["escalations"], 1)
        self.assertTrue(
            Notification.objects.filter(
                user=self.owner,
                dedupe_key__startswith=f"call_control:{task.id}:escalation:",
            ).exists()
        )

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_task_without_explicit_deadline_is_not_marked_overdue(self, _send_push):
        task = self._task(needs_due_date=True)
        now = datetime(2026, 10, 10, 12, 0, tzinfo=BARNAUL)

        result = process_call_commitment_controls(now=now)

        self.assertEqual(result["without_deadline"], 1)
        self.assertFalse(
            Notification.objects.filter(
                dedupe_key__startswith=f"call_control:{task.id}:",
            ).exists()
        )

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_rescheduling_starts_new_control_cycle(self, _send_push):
        task = self._task()
        first_now = datetime(2026, 10, 6, 10, 15, tzinfo=BARNAUL)
        process_call_commitment_controls(now=first_now)

        task.end_date = datetime(2026, 10, 8, 10, 0, tzinfo=BARNAUL).date()
        task.start_date = task.end_date
        task.save(update_fields=["start_date", "end_date", "updated_at"])

        before_new_deadline = datetime(2026, 10, 8, 9, 0, tzinfo=BARNAUL)
        self.assertEqual(
            process_call_commitment_controls(now=before_new_deadline)["due_reminders"],
            0,
        )

        after_new_deadline = datetime(2026, 10, 8, 10, 15, tzinfo=BARNAUL)
        self.assertEqual(
            process_call_commitment_controls(now=after_new_deadline)["due_reminders"],
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                user=self.manager,
                dedupe_key__startswith=f"call_control:{task.id}:due:",
            ).count(),
            2,
        )

    @patch("pool_service.services.call_commitment_control.send_push_to_users")
    def test_control_preserves_operations_payload_markers_and_defers_push(self, send_push):
        task = self._task()
        payload = dict(task.payload_json)
        payload["operations_notification_keys"] = ["42:dot-dedupe"]
        task.payload_json = payload
        task.save(update_fields=["payload_json", "updated_at"])

        now = datetime(2026, 10, 6, 10, 15, tzinfo=BARNAUL)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            result = process_call_commitment_controls(now=now)
            self.assertEqual(result["due_reminders"], 1)
            self.assertFalse(send_push.called)

        self.assertEqual(len(callbacks), 1)
        task.refresh_from_db()
        self.assertEqual(
            task.payload_json["operations_notification_keys"],
            ["42:dot-dedupe"],
        )
        self.assertTrue(task.payload_json["control_state"]["due_reminder_sent_at"])

    @patch("pool_service.services.notifications.send_push_to_users")
    def test_completed_commitment_is_ignored(self, _send_push):
        task = self._task()
        task.completed_at = datetime(2026, 10, 6, 9, 0, tzinfo=BARNAUL)
        task.save(update_fields=["completed_at", "updated_at"])

        result = process_call_commitment_controls(
            now=datetime(2026, 10, 8, 12, 0, tzinfo=BARNAUL)
        )

        self.assertEqual(result["checked"], 0)
        self.assertFalse(
            Notification.objects.filter(
                dedupe_key__startswith=f"call_control:{task.id}:",
            ).exists()
        )


    @patch("pool_service.services.call_commitment_control.send_push_to_users")
    def test_control_delivery_rechecks_employee_for_all_recipient_branches(self, send_push):
        for branch in ("primary", "participant", "owner", "admin"):
            task = self._task()
            target = self.manager if branch in {"primary", "participant"} else self.owner
            if branch == "participant":
                task.primary_responsible = None
                task.save(update_fields=["primary_responsible"])
            OrganizationAccess.objects.filter(user=self.owner, organization=self.organization).update(
                role="admin" if branch == "admin" else "owner")
            employee, _ = Employee.objects.get_or_create(organization=self.organization, user=target,
                defaults={"display_name": "Synthetic control recipient"})
            key = _control_key(task, _effective_deadline(task, task.payload_json))
            escalation = branch in {"owner", "admin"}
            for changes in ({"is_active": False}, {"is_active": True, "employment_status": Employee.STATUS_DISMISSED}):
                with self.subTest(branch=branch, changes=changes):
                    Employee.objects.filter(pk=employee.pk).update(is_active=True, employment_status=Employee.STATUS_EMPLOYED)
                    send_push.reset_mock()
                    _deliver_control_push(task.pk, key, escalation=escalation)
                    send_push.assert_called_once()
                    Employee.objects.filter(pk=employee.pk).update(**changes)
                    send_push.reset_mock()
                    _deliver_control_push(task.pk, key, escalation=escalation)
                    send_push.assert_not_called()
            Employee.objects.filter(pk=employee.pk).delete()



class CallRecordingSyncControlIntegrationTests(TestCase):
    @patch(
        "pool_service.management.commands.sync_call_recordings.process_pending_operations_pushes",
        return_value={
            "checked": 0,
            "assignment_attempts": 0,
            "notification_attempts": 0,
            "delivered": 0,
        },
    )
    @patch(
        "pool_service.management.commands.sync_call_recordings.process_call_commitment_controls",
        return_value={
            "checked": 0,
            "due_reminders": 0,
            "escalations": 0,
            "without_deadline": 0,
        },
    )
    def test_recording_sync_runs_controls_even_without_recordings(
        self,
        commitment_control,
        operations_push,
    ):
        call_command("sync_call_recordings", "--limit", "1")
        commitment_control.assert_called_once_with()
        operations_push.assert_called_once_with(limit=100)
