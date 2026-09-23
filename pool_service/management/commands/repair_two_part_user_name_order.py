from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pool_service.models import DataAuditLog, Employee, Organization


_MARKER_ENTITY_TYPE = "MaintenanceRepair"


def _name_parts(display_name):
    parts = [part for part in (display_name or "").split() if part]
    return parts[:2] if len(parts) >= 2 else None


def _is_reversed_candidate(employee, user):
    parts = _name_parts(employee.display_name)
    if not parts or not employee.is_active or not user.is_active:
        return False
    surname, given_name = parts
    if user.first_name.strip() != surname or user.last_name.strip() != given_name:
        return False
    employee_first = (employee.first_name or "").strip()
    employee_last = (employee.last_name or "").strip()
    if employee_first not in ("", given_name):
        return False
    if employee_last not in ("", surname):
        return False
    return True


def _marker_target(marker):
    after = marker.after or {}
    if after.get("status") != "completed":
        raise CommandError(
            "Refusing repair: existing repair marker is not a completed marker."
        )
    try:
        return (
            int(after["organization_id"]),
            int(after["user_id"]),
            int(after["employee_id"]),
        )
    except (KeyError, TypeError, ValueError):
        raise CommandError(
            "Refusing repair: existing completion marker has no valid stable target IDs."
        )


class Command(BaseCommand):
    help = (
        "Repairs one explicitly targeted Service2 user whose first_name/last_name "
        "are reversed against a linked employee record."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--organization-id",
            type=int,
            required=True,
            help="Stable Service2 organization primary key for the target employee.",
        )
        parser.add_argument(
            "--user-id",
            type=int,
            required=True,
            help="Stable Service2 auth user primary key to repair.",
        )
        parser.add_argument(
            "--employee-id",
            type=int,
            required=True,
            help="Stable Service2 employee primary key linked to the target user.",
        )
        parser.add_argument(
            "--repair-key",
            help=(
                "Opaque one-time repair key recorded in DataAuditLog. "
                "Required together with --apply."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the repair. Without this flag the command is read-only.",
        )

    @staticmethod
    def _load_target(*, organization_id, user_id, employee_id, lock=False):
        User = get_user_model()
        employee_qs = Employee.objects
        user_qs = User.objects
        if lock:
            employee_qs = employee_qs.select_for_update()
            user_qs = user_qs.select_for_update()

        try:
            employee = employee_qs.select_related("organization").get(
                pk=employee_id,
                organization_id=organization_id,
            )
        except Employee.DoesNotExist as exc:
            raise CommandError(
                "Refusing repair: target employee does not exist in the specified organization."
            ) from exc

        try:
            user = user_qs.get(pk=user_id)
        except User.DoesNotExist as exc:
            raise CommandError("Refusing repair: target user does not exist.") from exc

        if employee.user_id != user.pk:
            raise CommandError(
                "Refusing repair: target employee is not linked to the specified user."
            )
        if not _is_reversed_candidate(employee, user):
            raise CommandError(
                "Refusing repair: the explicitly targeted user is not a reversed-name candidate."
            )
        return employee, user

    def handle(self, *args, **options):
        organization_id = options["organization_id"]
        user_id = options["user_id"]
        employee_id = options["employee_id"]
        repair_key = (options.get("repair_key") or "").strip()
        apply = bool(options["apply"])
        requested_target = (organization_id, user_id, employee_id)

        if min(requested_target) <= 0:
            raise CommandError(
                "Refusing repair: organization, user and employee IDs must be positive integers."
            )
        if apply and not repair_key:
            raise CommandError("Refusing repair: --repair-key is required with --apply.")

        if not apply:
            self._load_target(
                organization_id=organization_id,
                user_id=user_id,
                employee_id=employee_id,
                lock=False,
            )
            self.stdout.write(
                "NAME_ORDER_REPAIR_DRY_RUN "
                f"organization_id={organization_id} user_id={user_id} employee_id={employee_id}"
            )
            return

        with transaction.atomic():
            # Serialize all name-order repairs through one shared database row.
            # A per-target lock is not enough because the same repair key could
            # otherwise be raced against a different valid target.
            global_lock = (
                Organization.objects.select_for_update()
                .order_by("pk")
                .first()
            )
            if global_lock is None:
                raise CommandError(
                    "Refusing repair: no organization exists to provide the repair lock."
                )

            marker = (
                DataAuditLog.objects.filter(
                    entity_type=_MARKER_ENTITY_TYPE,
                    entity_id=repair_key,
                )
                .order_by("id")
                .first()
            )
            if marker is not None:
                if _marker_target(marker) != requested_target:
                    raise CommandError(
                        "Refusing repair: repair key is already bound to a different "
                        "organization/user/employee target."
                    )
                self.stdout.write("NAME_ORDER_REPAIR_ALREADY_COMPLETED")
                return

            employee, user = self._load_target(
                organization_id=organization_id,
                user_id=user_id,
                employee_id=employee_id,
                lock=True,
            )

            surname, given_name = _name_parts(employee.display_name)
            before = {
                "first_name": user.first_name,
                "last_name": user.last_name,
            }
            user.first_name = given_name
            user.last_name = surname
            user.save(update_fields=["first_name", "last_name"])

            DataAuditLog.objects.create(
                entity_type="User",
                entity_id=str(user.pk),
                action=DataAuditLog.ACTION_UPDATE,
                organization=employee.organization,
                actor=None,
                before=before,
                after={
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                },
                changed_fields=["first_name", "last_name"],
            )
            DataAuditLog.objects.create(
                entity_type=_MARKER_ENTITY_TYPE,
                entity_id=repair_key,
                action=DataAuditLog.ACTION_UPDATE,
                organization=employee.organization,
                actor=None,
                before={"status": "pending"},
                after={
                    "status": "completed",
                    "organization_id": organization_id,
                    "user_id": user.pk,
                    "employee_id": employee.pk,
                },
                changed_fields=["status"],
            )

        self.stdout.write(
            self.style.SUCCESS(
                "NAME_ORDER_REPAIR_APPLIED "
                f"organization_id={organization_id} user_id={user_id} employee_id={employee_id}"
            )
        )
