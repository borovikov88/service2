"""Regression coverage for bounded Operations delivery and closed-task retries."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pool_service import operations_mcp_views as operations
from pool_service.models import Notification, Organization, OrganizationAccess, Profile, ServiceTask


class OperationsPushRetryTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Retry test organization")
        self.owner = User.objects.create_user("retry-owner")
        self.employee = User.objects.create_user("retry-employee")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        OrganizationAccess.objects.create(user=self.employee, organization=self.organization, role="manager")
        profile, _ = Profile.objects.get_or_create(user=self.employee)
        profile.push_notifications_enabled = True
        profile.in_app_notifications_enabled = True
        profile.save(update_fields=["push_notifications_enabled", "in_app_notifications_enabled"])
        self.authenticated = SimpleNamespace(grant=SimpleNamespace(authorized_by=self.owner))

    def _task_values(self, payload):
        return {
            "organization": self.organization,
            "title": "Follow up with test client",
            "start_date": date(2026, 10, 7),
            "end_date": date(2026, 10, 7),
            "task_type": ServiceTask.TYPE_CRM_FOLLOWUP,
            "source_type": ServiceTask.SOURCE_MANAGER,
            "status": ServiceTask.STATUS_NEW,
            "visibility": ServiceTask.VISIBILITY_PRIVATE,
            "primary_responsible": self.employee,
            "created_by": self.owner,
            "payload_json": payload,
        }

    def _assignment(self, result="pending_retry"):
        return {
            "responsible_user_id": self.employee.id,
            "added_by_user_id": self.owner.id,
            "push_delivery_result": result,
        }

    def _notification(self, result="pending_retry"):
        return {
            "employee_user_id": self.employee.id,
            "title": "Task reminder",
            "message": "Please follow up",
            "push_delivery_result": result,
        }

    def _task(self, *, assignment=True, notifications=False):
        payload = {"source": "operations_mcp"}
        if assignment:
            payload[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = self._assignment()
        if notifications:
            payload[operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY] = {
                "reminder": self._notification(),
            }
        task = ServiceTask.objects.create(**self._task_values(payload))
        task.responsibles.add(self.employee)
        return task

    def _assert_pending(self, task, expected):
        task.refresh_from_db()
        self.assertIs(task.payload_json[operations.PUSH_PENDING_PAYLOAD_KEY], expected)

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=1)
    def test_legacy_history_is_bounded_and_does_not_starve_later_pending(self, push):
        cap = operations.PUSH_RETRY_CANDIDATE_LIMIT
        old_tasks = [
            ServiceTask(**self._task_values({
                "source": "operations_mcp",
                operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY: self._assignment("sent"),
                operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY: {
                    "old": self._notification("blocked_push_disabled"),
                },
            }))
            for _ in range(cap + 3)
        ]
        ServiceTask.objects.bulk_create(old_tasks, batch_size=500)
        old_time = timezone.now() - timedelta(days=1)
        ServiceTask.objects.filter(organization=self.organization).update(updated_at=old_time)
        pending = self._task()

        with CaptureQueriesContext(connection) as queries:
            first = operations.process_pending_operations_pushes(limit=1)
        self.assertEqual(first["checked"], cap)
        self.assertEqual(first["assignment_attempts"], 0)
        self.assertTrue(any(f"LIMIT {cap}" in entry["sql"].upper() for entry in queries))
        push.assert_not_called()

        second = operations.process_pending_operations_pushes(limit=1)
        self.assertEqual(second["checked"], 4)
        self.assertEqual(second["delivered"], 1)
        self._assert_pending(pending, False)
        self.assertEqual(operations.process_pending_operations_pushes(limit=1)["checked"], 0)
        self.assertEqual(push.call_count, 1)
        old = ServiceTask.objects.filter(organization=self.organization).order_by("id").first()
        self.assertEqual(old.updated_at, old_time)
        self.assertEqual(old.payload_json[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY]["push_delivery_result"], "sent")

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=0)
    def test_failed_deliveries_rotate_between_tasks_and_respect_attempt_budget(self, push):
        first = self._task()
        second = self._task()
        with patch.object(operations, "_retry_assignment_push", wraps=operations._retry_assignment_push) as retry:
            one = operations.process_pending_operations_pushes(limit=1)
            two = operations.process_pending_operations_pushes(limit=1)
        self.assertEqual(one["assignment_attempts"], 1)
        self.assertEqual(two["assignment_attempts"], 1)
        self.assertEqual([call.args[0] for call in retry.call_args_list], [first.id, second.id])
        self.assertEqual(push.call_count, 2)
        self._assert_pending(first, True)
        self._assert_pending(second, True)

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=1)
    def test_pending_flag_covers_both_delivery_kinds(self, push):
        task = self._task(notifications=True)
        self.assertEqual(operations._retry_assignment_push(task.id, self.employee.id), 1)
        self._assert_pending(task, True)
        self.assertEqual(operations._retry_employee_notification_push(task.id, "reminder"), 1)
        self._assert_pending(task, False)
        self.assertEqual(operations.process_pending_operations_pushes()["checked"], 0)
        self.assertEqual(push.call_count, 2)

    @patch("pool_service.operations_mcp_views.send_push_to_users", side_effect=[0, 1])
    def test_transient_failure_stays_pending_until_sent(self, push):
        task = self._task()
        self.assertEqual(operations._retry_assignment_push(task.id, self.employee.id), 0)
        self._assert_pending(task, True)
        self.assertEqual(operations.process_pending_operations_pushes()["delivered"], 1)
        self._assert_pending(task, False)
        self.assertEqual(operations.process_pending_operations_pushes()["checked"], 0)
        self.assertEqual(push.call_count, 2)

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=1)
    def test_retry_reloads_closed_state_and_remains_terminal_after_reopening(self, push):
        states = (
            {"status": ServiceTask.STATUS_DONE},
            {"status": ServiceTask.STATUS_CANCELLED},
            {"is_archived": True},
            {"completed_at": timezone.now()},
        )
        for state in states:
            with self.subTest(state=state):
                task = self._task(notifications=True)
                ServiceTask.objects.filter(pk=task.id).update(**state)
                self.assertEqual(operations._retry_assignment_push(task.id, self.employee.id), 0)
                self.assertEqual(operations._retry_employee_notification_push(task.id, "reminder"), 0)
                self._assert_pending(task, False)
                assignment = task.payload_json[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY]
                notification = task.payload_json[operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY]["reminder"]
                self.assertEqual(assignment["push_delivery_result"], "blocked_task_closed")
                self.assertEqual(notification["push_delivery_result"], "blocked_task_closed")
                self.assertNotIn("push_delivered_at", assignment)
                ServiceTask.objects.filter(pk=task.id).update(
                    status=ServiceTask.STATUS_NEW, is_archived=False, completed_at=None,
                )
                self.assertEqual(operations._retry_assignment_push(task.id, self.employee.id), 0)
                self.assertEqual(operations._retry_employee_notification_push(task.id, "reminder"), 0)
        push.assert_not_called()
        self.assertEqual(operations.process_pending_operations_pushes()["checked"], 0)

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=0)
    def test_assignment_failure_followed_by_completion_is_not_sent_later(self, push):
        task = self._task()
        operations._retry_assignment_push(task.id, self.employee.id)
        self.assertEqual(push.call_count, 1)
        ServiceTask.objects.filter(pk=task.id).update(status=ServiceTask.STATUS_DONE)
        result = operations.process_pending_operations_pushes()
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(push.call_count, 1)
        self._assert_pending(task, False)

    @patch("pool_service.operations_mcp_views._schedule_assignment_push")
    def test_closed_idempotent_assignment_does_not_create_notification(self, schedule):
        task = self._task()
        task.status = ServiceTask.STATUS_DONE
        task.save(update_fields=["status"])
        with transaction.atomic():
            task = ServiceTask.objects.select_for_update().get(pk=task.id)
            self.assertFalse(operations._ensure_assignment_delivery(task, self.employee, self.owner))
        self.assertFalse(Notification.objects.filter(organization=self.organization).exists())
        schedule.assert_not_called()
        self._assert_pending(task, False)

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=1)
    def test_new_notification_reopens_a_drained_task_queue(self, push):
        task = self._task()
        operations._retry_assignment_push(task.id, self.employee.id)
        self._assert_pending(task, False)
        with self.captureOnCommitCallbacks(execute=False):
            operations._send_employee_notification(self.authenticated, self.organization, {
                "employee_user_id": self.employee.id,
                "task_id": task.id,
                "title": "Follow up",
                "message": "New reminder after assignment delivery",
                "dedupe_key": "later-reminder",
            })
        self._assert_pending(task, True)
        result = operations.process_pending_operations_pushes()
        self.assertEqual(result["notification_attempts"], 1)
        self.assertEqual(result["delivered"], 1)
        self._assert_pending(task, False)

    def test_legacy_retirement_preserves_a_new_pending_notification(self):
        task = self._task()
        payload = dict(task.payload_json)
        payload[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = self._assignment("sent")
        payload[operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY] = {
            "new": self._notification(),
        }
        ServiceTask.objects.filter(pk=task.id).update(payload_json=payload)
        operations._refresh_push_pending_flag(task.id)
        self._assert_pending(task, True)
        self.assertIn("new", task.payload_json[operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY])

    @patch("pool_service.operations_mcp_views.send_push_to_users")
    def test_disabled_push_is_removed_from_retry_selection(self, push):
        task = self._task(notifications=True)
        Profile.objects.filter(user=self.employee).update(push_notifications_enabled=False)
        operations.process_pending_operations_pushes()
        self._assert_pending(task, False)
        self.assertEqual(operations.process_pending_operations_pushes()["checked"], 0)
        push.assert_not_called()
