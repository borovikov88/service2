"""Regression coverage for indexed Operations delivery and closed-task retries."""

from datetime import date, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pool_service import operations_mcp_views as operations
from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import PhoneCall, TelephonyConnection
from pool_service.models import Notification, Organization, OrganizationAccess, Profile, ServiceTask
from pool_service.operations_models import OperationsPushQueue
from pool_service.services.operations_push_queue import due_candidates, sync_queue


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
        # Use the production producer: queue membership and payload are atomic.
        operations._save_push_payload(task, payload)
        return task

    def _assert_pending(self, task, expected):
        task.refresh_from_db()
        self.assertIs(task.payload_json[operations.PUSH_PENDING_PAYLOAD_KEY], expected)
        self.assertEqual(OperationsPushQueue.objects.filter(task_id=task.pk).exists(), expected)

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
        # Historical task JSON does not participate in candidate selection at all.
        self.assertEqual(first["checked"], 1)
        self.assertEqual(first["assignment_attempts"], 1)
        self.assertEqual(first["delivered"], 1)
        self.assertTrue(any(f"LIMIT {cap}" in entry["sql"].upper() for entry in queries))
        self.assertFalse(any("JSON_EXTRACT" in entry["sql"].upper() for entry in queries))
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

    @patch("pool_service.operations_mcp_views.send_push_to_users")
    def test_retries_recheck_call_privacy_under_organization_lock(self, push):
        connection = TelephonyConnection.objects.create(
            organization=self.organization,
            external_id="private-retry-test",
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            source_kind=PhoneCall.SOURCE_TELEPHONY,
            connection=connection,
            external_id="private-retry-call",
            employee=self.employee,
            provider_user=self.employee.username,
            phone_number="+7 999 000-00-01",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            duration_seconds=90,
            result=PhoneCall.RESULT_ANSWERED,
        )
        CallPrivateNumber.objects.create(
            organization=self.organization,
            owner=self.employee,
            label="Private",
            phone_key="9990000001",
        )
        task = self._task(notifications=True)
        task.payload_json["source_call_id"] = call.pk
        task.save(update_fields=["payload_json"])

        self.assertEqual(operations._retry_assignment_push(task.pk, self.employee.pk), 0)
        self.assertEqual(operations._retry_employee_notification_push(task.pk, "reminder"), 0)

        task.refresh_from_db()
        self.assertEqual(
            task.payload_json[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY]["push_delivery_result"],
            "blocked_private_source",
        )
        self.assertEqual(
            task.payload_json[operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY]["reminder"]["push_delivery_result"],
            "blocked_private_source",
        )
        self.assertFalse(OperationsPushQueue.objects.filter(task_id=task.pk).exists())
        push.assert_not_called()

    def test_pending_selection_has_composite_index_and_no_task_json_scan(self):
        query = due_candidates(timezone.now(), 500)
        sql, _params = query.query.sql_with_params()
        self.assertNotIn("payload_json", sql)
        self.assertNotIn(ServiceTask._meta.db_table, sql)
        self.assertIn("LIMIT 500", sql)
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, OperationsPushQueue._meta.db_table)
        self.assertEqual(constraints["ops_push_due_task_idx"]["columns"], ["next_attempt_at", "task_id"])
        if connection.vendor == "sqlite":
            plan = query.explain()
            self.assertIn("ops_push_due_task_idx", plan)
            self.assertNotIn("TEMP B-TREE", plan.upper())

    @patch("pool_service.operations_mcp_views.send_push_to_users", side_effect=[0, 1])
    def test_failing_assignment_does_not_starve_same_task_reminder(self, push):
        task = self._task(notifications=True)
        first = operations.process_pending_operations_pushes(limit=1)
        self.assertEqual(first["assignment_attempts"], 1)
        OperationsPushQueue.objects.filter(task_id=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        second = operations.process_pending_operations_pushes(limit=1)
        self.assertEqual(second["notification_attempts"], 1)
        self.assertEqual(second["delivered"], 1)
        self.assertEqual(push.call_count, 2)
        self._assert_pending(task, True)

    @patch("pool_service.operations_mcp_views.send_push_to_users", side_effect=RuntimeError("ambiguous transport failure"))
    def test_transport_exception_is_terminal_unknown_not_automatic_duplicate(self, push):
        task = self._task()
        with self.assertLogs("pool_service.services.operations_push_queue", level="WARNING"):
            result = operations.process_pending_operations_pushes()
        self.assertEqual(result["assignment_attempts"], 1)
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(operations.process_pending_operations_pushes()["checked"], 0)
        self.assertEqual(push.call_count, 1)
        task.refresh_from_db()
        delivery = task.payload_json[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY]
        self.assertEqual(delivery["push_delivery_result"], "attempt_committed")
        self.assertFalse(OperationsPushQueue.objects.filter(task_id=task.pk).exists())

    @patch("pool_service.operations_mcp_views.send_push_to_users", return_value=1)
    def test_post_send_database_failure_cannot_reopen_duplicate_delivery(self, push):
        task = self._task()
        original = operations._save_push_payload
        saves = {"count": 0}

        def fail_after_external_send(locked_task, payload):
            saves["count"] += 1
            if saves["count"] == 2:
                raise RuntimeError("database write failed after provider accepted")
            return original(locked_task, payload)

        with patch.object(operations, "_save_push_payload", side_effect=fail_after_external_send):
            with self.assertRaises(RuntimeError):
                operations._retry_assignment_push(task.id, self.employee.id)

        task.refresh_from_db()
        delivery = task.payload_json[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY]
        self.assertEqual(delivery["push_delivery_result"], "attempt_committed")
        self.assertFalse(OperationsPushQueue.objects.filter(task_id=task.pk).exists())
        self.assertEqual(operations._retry_assignment_push(task.id, self.employee.id), 0)
        self.assertEqual(push.call_count, 1)

    def test_queue_membership_rolls_back_with_payload(self):
        task = ServiceTask.objects.create(**self._task_values({}))
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                task = ServiceTask.objects.select_for_update().get(pk=task.pk)
                operations._save_push_payload(task, {
                    operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY: self._assignment(),
                })
                raise RuntimeError("rollback")
        task.refresh_from_db()
        self.assertEqual(task.payload_json, {})
        self.assertFalse(OperationsPushQueue.objects.filter(task_id=task.pk).exists())

    def test_migration_backfills_pending_without_trusting_cached_flag(self):
        tasks = []
        for flag in (None, False):
            payload = {operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY: self._assignment()}
            if flag is not None:
                payload[operations.PUSH_PENDING_PAYLOAD_KEY] = flag
            tasks.append(ServiceTask.objects.create(**self._task_values(payload)))
        terminal = ServiceTask.objects.create(**self._task_values({
            operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY: self._assignment("sent"),
        }))
        migration = import_module("pool_service.migrations.0133_operations_push_queue")
        migration.seed_existing_pending(apps, SimpleNamespace(connection=connection))
        migration.seed_existing_pending(apps, SimpleNamespace(connection=connection))
        self.assertEqual(set(OperationsPushQueue.objects.values_list("task_id", flat=True)), {task.pk for task in tasks})
        self.assertFalse(OperationsPushQueue.objects.filter(task_id=terminal.pk).exists())

    def test_replayed_producer_preserves_backoff_and_delivery_cursor(self):
        task = self._task()
        future = timezone.now() + timedelta(hours=1)
        OperationsPushQueue.objects.filter(task_id=task.pk).update(next_attempt_at=future, last_marker="n:reminder")
        with transaction.atomic():
            task = ServiceTask.objects.select_for_update().get(pk=task.pk)
            sync_queue(task, pending=True)
        entry = OperationsPushQueue.objects.get(task_id=task.pk)
        self.assertEqual(entry.next_attempt_at, future)
        self.assertEqual(entry.last_marker, "n:reminder")

    def test_stale_terminal_entry_is_removed_without_rewriting_history(self):
        task = self._task()
        payload = dict(task.payload_json)
        payload[operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = self._assignment("sent")
        ServiceTask.objects.filter(pk=task.pk).update(payload_json=payload)
        result = operations.process_pending_operations_pushes()
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["assignment_attempts"], 0)
        self.assertFalse(OperationsPushQueue.objects.filter(task_id=task.pk).exists())
        task.refresh_from_db()
        self.assertEqual(task.payload_json, payload)

    def test_candidate_limit_bounds_stale_queue_entries_too(self):
        stale = []
        for _ in range(5):
            task = ServiceTask.objects.create(**self._task_values({}))
            stale.append(OperationsPushQueue(task=task))
        OperationsPushQueue.objects.bulk_create(stale)
        with patch.object(operations, "PUSH_RETRY_CANDIDATE_LIMIT", 2):
            result = operations.process_pending_operations_pushes()
        self.assertEqual(result["checked"], 2)
        self.assertEqual(OperationsPushQueue.objects.count(), 3)
