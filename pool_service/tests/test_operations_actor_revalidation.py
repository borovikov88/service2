"""Operations writes must not use authority cached by bearer authentication."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase, override_settings

from pool_service import operations_mcp_views as operations
from pool_service.models import (
    Notification, Organization, OrganizationAccess, Profile,
    ServiceTask, ServiceTaskChange,
)
from pool_service.operations_mcp_policy import locked_operations_actor
from pool_service.operations_models import OperationsPushQueue


@override_settings(ADVISOR_OPERATIONS_MCP_ALLOW_SUPERUSER=False)
class OperationsActorRevalidationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Actor test organization")
        self.other_organization = Organization.objects.create(name="Other test organization")
        self.owner = User.objects.create_user("operations-actor")
        self.employee = User.objects.create_user("operations-recipient")
        OrganizationAccess.objects.create(
            user=self.owner, organization=self.organization, role="owner",
        )
        OrganizationAccess.objects.create(
            user=self.employee, organization=self.organization, role="manager",
        )
        profile, _ = Profile.objects.get_or_create(user=self.employee)
        profile.push_notifications_enabled = True
        profile.in_app_notifications_enabled = True
        profile.save(update_fields=["push_notifications_enabled", "in_app_notifications_enabled"])
        # Deliberately retain the same cached actor object across role changes.
        self.authenticated = SimpleNamespace(
            grant=SimpleNamespace(authorized_by=self.owner, id=101),
        )
        self.task = ServiceTask.objects.create(
            organization=self.organization, title="Original appointment",
            start_date=date(2026, 10, 7), end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER, status=ServiceTask.STATUS_NEW,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            primary_responsible=self.employee, created_by=self.owner,
        )
        self.task.responsibles.add(self.employee)

    def _create_arguments(self):
        return {
            "idempotency_key": "actor-regression-create", "title": "Follow up",
            "responsible_user_id": self.employee.pk, "due_date": "2026-10-08",
        }

    def _notify_arguments(self):
        return {
            "task_id": self.task.pk, "employee_user_id": self.employee.pk,
            "title": "Follow up", "message": "Test message", "dedupe_key": "actor-regression",
        }

    def _writes(self):
        return [
            ("create", operations._create_task, self._create_arguments()),
            ("reschedule", operations._reschedule_task, {
                "task_id": self.task.pk, "due_date": "2026-10-09", "reason": "Test reschedule",
            }),
            ("notify", operations._send_employee_notification, self._notify_arguments()),
            ("complete", operations._complete_task, {"task_id": self.task.pk, "comment": "Done"}),
        ]

    def _snapshot(self):
        return {
            "tasks": list(ServiceTask.objects.order_by("pk").values()),
            "history": list(ServiceTaskChange.objects.order_by("pk").values()),
            "notifications": list(Notification.objects.order_by("pk").values()),
            "queue": list(OperationsPushQueue.objects.order_by("pk").values()),
        }

    def _deny_all(self):
        for name, handler, arguments in self._writes():
            with self.subTest(operation=name):
                before = self._snapshot()
                with self.captureOnCommitCallbacks(execute=False) as callbacks:
                    with self.assertRaises(PermissionError):
                        handler(self.authenticated, self.organization, arguments)
                self.assertEqual(callbacks, [])
                self.assertEqual(self._snapshot(), before)

    def _demote(self):
        OrganizationAccess.objects.filter(
            user=self.owner, organization=self.organization,
        ).update(role="manager")

    def test_demoted_actor_cannot_perform_any_write(self):
        self._demote()
        self._deny_all()

    def test_revoked_membership_blocks_all_writes(self):
        OrganizationAccess.objects.filter(
            user=self.owner, organization=self.organization,
        ).delete()
        self._deny_all()

    def test_inactive_actor_is_not_allowed_from_cached_active_object(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertTrue(self.owner.is_active)
        self._deny_all()

    def test_deleted_actor_cannot_perform_any_write(self):
        User.objects.filter(pk=self.owner.pk).delete()
        self.assertIsNotNone(self.owner.pk)
        self._deny_all()

    def test_privileged_role_in_another_organization_does_not_help(self):
        self._demote()
        OrganizationAccess.objects.create(
            user=self.owner, organization=self.other_organization, role="owner",
        )
        self._deny_all()

    def test_revoked_create_replay_does_not_requeue_assignment(self):
        arguments = self._create_arguments()
        operations._create_task(self.authenticated, self.organization, arguments)
        self._demote()
        before = self._snapshot()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            with self.assertRaises(PermissionError):
                operations._create_task(self.authenticated, self.organization, arguments)
        self.assertEqual(callbacks, [])
        self.assertEqual(self._snapshot(), before)

    def test_noop_reschedule_still_requires_current_authority(self):
        self._demote()
        before = self._snapshot()
        with self.assertRaises(PermissionError):
            operations._reschedule_task(self.authenticated, self.organization, {
                "task_id": self.task.pk, "due_date": "2026-10-07", "reason": "Same date",
            })
        self.assertEqual(self._snapshot(), before)

    def test_noop_completion_still_requires_current_authority(self):
        ServiceTask.objects.filter(pk=self.task.pk).update(status=ServiceTask.STATUS_DONE)
        self._demote()
        before = self._snapshot()
        with self.assertRaises(PermissionError):
            operations._complete_task(self.authenticated, self.organization, {"task_id": self.task.pk})
        self.assertEqual(self._snapshot(), before)

    def test_revoked_notification_replay_does_not_requeue_push(self):
        arguments = self._notify_arguments()
        operations._send_employee_notification(self.authenticated, self.organization, arguments)
        self._demote()
        before = self._snapshot()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            with self.assertRaises(PermissionError):
                operations._send_employee_notification(self.authenticated, self.organization, arguments)
        self.assertEqual(callbacks, [])
        self.assertEqual(self._snapshot(), before)

    def _allow_all(self):
        for name, handler, arguments in self._writes():
            with self.subTest(operation=name):
                result = handler(self.authenticated, self.organization, arguments)
                self.assertIsInstance(result, dict)
        self.assertTrue(ServiceTask.objects.filter(
            payload_json__operations_mcp_idempotency_key="actor-regression-create",
        ).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_DONE)
        self.assertTrue(Notification.objects.filter(
            user=self.employee, dedupe_key=f"operations_mcp:{self.task.pk}:actor-regression",
        ).exists())

    def test_current_owner_keeps_all_supported_writes(self):
        self._allow_all()

    def test_current_admin_keeps_all_supported_writes(self):
        OrganizationAccess.objects.filter(
            user=self.owner, organization=self.organization,
        ).update(role="admin")
        self._allow_all()

    @override_settings(ADVISOR_OPERATIONS_MCP_ALLOW_SUPERUSER=True)
    def test_stale_superuser_flag_is_not_trusted(self):
        self.owner.is_superuser = True
        self._demote()
        self._deny_all()

    def test_current_superuser_cannot_bypass_disabled_policy(self):
        User.objects.filter(pk=self.owner.pk).update(is_superuser=True)
        self._deny_all()

    @override_settings(ADVISOR_OPERATIONS_MCP_ALLOW_SUPERUSER=True)
    def test_explicit_current_superuser_policy_is_preserved(self):
        User.objects.filter(pk=self.owner.pk).update(is_superuser=True)
        self._demote()
        self._allow_all()

    def test_revocation_just_before_locked_check_blocks_all_writes(self):
        original = operations._authorized_actor
        for name, handler, arguments in self._writes():
            with self.subTest(operation=name):
                before = self._snapshot()

                def revoke_then_check(authenticated, organization):
                    self._demote()
                    return original(authenticated, organization)

                with patch.object(operations, "_authorized_actor", side_effect=revoke_then_check) as check:
                    with self.assertRaises(PermissionError):
                        handler(self.authenticated, self.organization, arguments)
                check.assert_called_once()
                self.assertEqual(self._snapshot(), before)
                # The simulated revocation is inside the rejected transaction.
                self.assertTrue(OrganizationAccess.objects.filter(
                    user=self.owner, organization=self.organization, role="owner",
                ).exists())


class OperationsActorTransactionTests(SimpleTestCase):
    def test_locked_authorization_cannot_run_without_transaction(self):
        with patch("pool_service.operations_mcp_policy.transaction.get_connection") as connection:
            connection.return_value.in_atomic_block = False
            with self.assertRaises(RuntimeError):
                locked_operations_actor(1, SimpleNamespace(pk=1))
