from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pool_service.models import DataAuditLog, Employee, Organization


class RepairTwoPartUserNameOrderCommandTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Name Repair Test")

    def _create_candidate(
        self,
        username,
        *,
        organization=None,
        surname="Surname",
        given_name="Given",
    ):
        organization = organization or self.organization
        user = User.objects.create_user(
            username=username,
            first_name=surname,
            last_name=given_name,
            password="password",
        )
        employee = Employee.objects.create(
            organization=organization,
            user=user,
            first_name="",
            last_name="",
            middle_name="",
            display_name=f"{surname} {given_name}",
            is_active=True,
        )
        return user, employee

    def _call(self, user, employee, *extra, stdout=None, organization=None):
        organization = organization or employee.organization
        return call_command(
            "repair_two_part_user_name_order",
            "--organization-id",
            str(organization.pk),
            "--user-id",
            str(user.pk),
            "--employee-id",
            str(employee.pk),
            *extra,
            stdout=stdout,
        )

    def test_apply_repairs_exact_stable_target_and_records_completion_marker(self):
        user, employee = self._create_candidate("candidate")

        out = StringIO()
        self._call(
            user,
            employee,
            "--apply",
            "--repair-key",
            "repair-test-1",
            stdout=out,
        )

        user.refresh_from_db()
        self.assertEqual(user.first_name, "Given")
        self.assertEqual(user.last_name, "Surname")
        self.assertIn("NAME_ORDER_REPAIR_APPLIED", out.getvalue())

        audit = DataAuditLog.objects.get(entity_type="User", entity_id=str(user.pk))
        self.assertEqual(audit.organization_id, self.organization.pk)
        self.assertEqual(audit.before["first_name"], "Surname")
        self.assertEqual(audit.before["last_name"], "Given")
        self.assertEqual(audit.after["first_name"], "Given")
        self.assertEqual(audit.after["last_name"], "Surname")
        self.assertEqual(audit.changed_fields, ["first_name", "last_name"])
        self.assertIsNone(audit.actor_id)

        marker = DataAuditLog.objects.get(
            entity_type="MaintenanceRepair",
            entity_id="repair-test-1",
        )
        self.assertEqual(marker.after["status"], "completed")
        self.assertEqual(marker.after["organization_id"], self.organization.pk)
        self.assertEqual(marker.after["user_id"], user.pk)
        self.assertEqual(marker.after["employee_id"], employee.pk)

    def test_without_apply_is_read_only(self):
        user, employee = self._create_candidate("dry-run")

        out = StringIO()
        self._call(user, employee, stdout=out)

        user.refresh_from_db()
        self.assertEqual(user.first_name, "Surname")
        self.assertEqual(user.last_name, "Given")
        self.assertIn("NAME_ORDER_REPAIR_DRY_RUN", out.getvalue())
        self.assertFalse(DataAuditLog.objects.exists())

    def test_unrelated_same_name_user_is_ignored_by_stable_ids(self):
        target, target_employee = self._create_candidate("target")
        unrelated, unrelated_employee = self._create_candidate("unrelated")

        self._call(
            target,
            target_employee,
            "--apply",
            "--repair-key",
            "repair-test-2",
        )

        target.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual((target.first_name, target.last_name), ("Given", "Surname"))
        self.assertEqual(
            (unrelated.first_name, unrelated.last_name),
            ("Surname", "Given"),
        )
        self.assertNotEqual(target_employee.pk, unrelated_employee.pk)

    def test_wrong_employee_user_pair_refuses_all_writes(self):
        first_user, first_employee = self._create_candidate("first")
        second_user, _ = self._create_candidate("second")

        with self.assertRaises(CommandError):
            self._call(
                second_user,
                first_employee,
                "--apply",
                "--repair-key",
                "repair-test-3",
            )

        first_user.refresh_from_db()
        second_user.refresh_from_db()
        self.assertEqual((first_user.first_name, first_user.last_name), ("Surname", "Given"))
        self.assertEqual((second_user.first_name, second_user.last_name), ("Surname", "Given"))
        self.assertFalse(DataAuditLog.objects.exists())

    def test_wrong_organization_refuses_all_writes(self):
        user, employee = self._create_candidate("candidate")
        other_organization = Organization.objects.create(name="Other Organization")

        with self.assertRaises(CommandError):
            self._call(
                user,
                employee,
                "--apply",
                "--repair-key",
                "repair-test-4",
                organization=other_organization,
            )

        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Surname", "Given"))
        self.assertFalse(DataAuditLog.objects.exists())

    def test_completion_marker_same_target_is_idempotent(self):
        user, employee = self._create_candidate("candidate")
        self._call(
            user,
            employee,
            "--apply",
            "--repair-key",
            "repair-test-5",
        )
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Given", "Surname"))

        out = StringIO()
        self._call(
            user,
            employee,
            "--apply",
            "--repair-key",
            "repair-test-5",
            stdout=out,
        )
        self.assertIn("NAME_ORDER_REPAIR_ALREADY_COMPLETED", out.getvalue())
        self.assertEqual(
            DataAuditLog.objects.filter(
                entity_type="MaintenanceRepair",
                entity_id="repair-test-5",
            ).count(),
            1,
        )

    def test_completion_marker_different_target_fails_closed(self):
        first_user, first_employee = self._create_candidate("first")
        second_user, second_employee = self._create_candidate(
            "second",
            surname="OtherSurname",
            given_name="OtherGiven",
        )
        self._call(
            first_user,
            first_employee,
            "--apply",
            "--repair-key",
            "repair-test-6",
        )

        with self.assertRaises(CommandError):
            self._call(
                second_user,
                second_employee,
                "--apply",
                "--repair-key",
                "repair-test-6",
            )

        second_user.refresh_from_db()
        self.assertEqual(
            (second_user.first_name, second_user.last_name),
            ("OtherSurname", "OtherGiven"),
        )
        self.assertEqual(
            DataAuditLog.objects.filter(
                entity_type="MaintenanceRepair",
                entity_id="repair-test-6",
            ).count(),
            1,
        )

    def test_invalid_existing_marker_fails_closed(self):
        user, employee = self._create_candidate("candidate")
        DataAuditLog.objects.create(
            entity_type="MaintenanceRepair",
            entity_id="repair-test-7",
            action=DataAuditLog.ACTION_UPDATE,
            organization=self.organization,
            actor=None,
            before={"status": "pending"},
            after={"status": "completed"},
            changed_fields=["status"],
        )

        with self.assertRaises(CommandError):
            self._call(
                user,
                employee,
                "--apply",
                "--repair-key",
                "repair-test-7",
            )

        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Surname", "Given"))


    def test_pending_marker_with_valid_target_ids_fails_closed(self):
        user, employee = self._create_candidate("candidate")
        DataAuditLog.objects.create(
            entity_type="MaintenanceRepair",
            entity_id="repair-test-pending",
            action=DataAuditLog.ACTION_UPDATE,
            organization=self.organization,
            actor=None,
            before={"status": "pending"},
            after={
                "status": "pending",
                "organization_id": self.organization.pk,
                "user_id": user.pk,
                "employee_id": employee.pk,
            },
            changed_fields=["status"],
        )

        with self.assertRaises(CommandError):
            self._call(
                user,
                employee,
                "--apply",
                "--repair-key",
                "repair-test-pending",
            )

        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Surname", "Given"))

    def test_three_part_employee_name_uses_surname_and_given_name(self):
        user = User.objects.create_user(
            username="patronymic",
            first_name="Surname",
            last_name="Given",
            password="password",
        )
        employee = Employee.objects.create(
            organization=self.organization,
            user=user,
            first_name="Given",
            last_name="Surname",
            middle_name="Patronymic",
            display_name="Surname Given Patronymic",
            is_active=True,
        )

        self._call(
            user,
            employee,
            "--apply",
            "--repair-key",
            "repair-test-8",
        )

        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Given", "Surname"))
