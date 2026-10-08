"""Regression coverage for role changes after early task-feedback checks."""

from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from pool_service.models import Organization, OrganizationAccess, ServiceTask
from pool_service.services.task_feedback import apply_feedback, version_for


class TaskFeedbackLockedPermissionsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Feedback permission test")
        self.actor = User.objects.create_user("feedback-current-owner")
        self.creator = User.objects.create_user("feedback-task-creator")
        self.employee = User.objects.create_user("feedback-task-participant")
        self.access = OrganizationAccess.objects.create(
            organization=self.org, user=self.actor, role="owner",
        )
        for user in (self.creator, self.employee):
            OrganizationAccess.objects.create(
                organization=self.org, user=user, role="manager",
            )
        self.task = ServiceTask.objects.create(
            organization=self.org, title="Permission test task",
            start_date=date(2026, 10, 7), end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER,
            status=ServiceTask.STATUS_NEW,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            created_by=self.creator, primary_responsible=self.employee,
        )
        self.task.responsibles.add(self.employee)

    def submit(self, user=None, **overrides):
        self.task.refresh_from_db()
        command = {
            "task_id": self.task.pk, "user": user or self.actor,
            "action": "cancel", "comment": "Explicit cancellation",
            "expected_version": version_for(self.task), "request_id": str(uuid4()),
        }
        command.update(overrides)
        return apply_feedback(**command)

    def assert_unchanged(self):
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertFalse(self.task.changes.filter(field_name__startswith="feedback:").exists())

    def assert_rejected_after_early_checks(self, change):
        checks = 0

        def stale_access(*args, **kwargs):
            nonlocal checks
            checks += 1
            if checks == 2:
                change()
            return True

        with patch("pool_service.services.task_feedback.has_access", side_effect=stale_access):
            with self.assertRaises(PermissionDenied):
                self.submit()
        self.assertEqual(checks, 2)
        self.assert_unchanged()

    def test_demotion_after_second_check_cannot_cancel_unrelated_task(self):
        self.assert_rejected_after_early_checks(
            lambda: OrganizationAccess.objects.filter(pk=self.access.pk).update(role="manager"),
        )

    def test_membership_revoked_after_second_check_cannot_write(self):
        self.assert_rejected_after_early_checks(
            lambda: OrganizationAccess.objects.filter(pk=self.access.pk).delete(),
        )

    def test_deactivated_account_after_second_check_cannot_write(self):
        self.assert_rejected_after_early_checks(
            lambda: User.objects.filter(pk=self.actor.pk).update(is_active=False),
        )

    def test_cached_superuser_flag_cannot_bypass_current_database_role(self):
        OrganizationAccess.objects.filter(pk=self.access.pk).update(role="manager")
        self.actor.is_superuser = True
        with self.assertRaises(PermissionDenied):
            self.submit()
        self.assert_unchanged()

    def test_owner_role_in_another_organization_does_not_grant_access(self):
        OrganizationAccess.objects.filter(pk=self.access.pk).update(role="manager")
        other = Organization.objects.create(name="Unrelated organization")
        OrganizationAccess.objects.create(organization=other, user=self.actor, role="owner")
        with patch("pool_service.services.task_feedback.has_access", return_value=True):
            with self.assertRaises(PermissionDenied):
                self.submit()
        self.assert_unchanged()

    def test_current_owner_can_still_cancel(self):
        task, event, changed = self.submit()
        self.assertTrue(changed)
        self.assertEqual(task.status, ServiceTask.STATUS_CANCELLED)
        self.assertEqual(event.changed_by_id, self.actor.pk)

    def test_creator_with_ordinary_membership_keeps_existing_edit_right(self):
        task, event, changed = self.submit(user=self.creator)
        self.assertTrue(changed)
        self.assertEqual(event.changed_by_id, self.creator.pk)
        self.assertEqual(task.status, ServiceTask.STATUS_CANCELLED)

    def test_participant_with_ordinary_membership_keeps_existing_edit_right(self):
        task, event, changed = self.submit(user=self.employee)
        self.assertTrue(changed)
        self.assertEqual(event.changed_by_id, self.employee.pk)
        self.assertEqual(task.status, ServiceTask.STATUS_CANCELLED)

    def test_primary_assignee_alone_does_not_gain_new_edit_right(self):
        self.task.responsibles.remove(self.employee)
        with patch("pool_service.services.task_feedback.has_access", return_value=True):
            with self.assertRaises(PermissionDenied):
                self.submit(user=self.employee)
        self.assert_unchanged()

    def test_exact_replay_still_requires_current_permission(self):
        request = str(uuid4())
        task, event, changed = self.submit(action="comment", request_id=request)
        self.assertTrue(changed)
        OrganizationAccess.objects.filter(pk=self.access.pk).update(role="manager")
        with patch("pool_service.services.task_feedback.has_access", return_value=True):
            with self.assertRaises(PermissionDenied):
                self.submit(action="comment", request_id=request)
        self.assertEqual(self.task.changes.filter(field_name__startswith="feedback:").count(), 1)
        self.assertEqual(event.changed_by_id, self.actor.pk)
        self.assertEqual(task.status, ServiceTask.STATUS_NEW)
