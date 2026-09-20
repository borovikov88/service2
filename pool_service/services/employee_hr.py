from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from pool_service.finance_imports.employee_matching import normalize_onec_name
from pool_service.models import (
    DataAuditLog,
    Employee,
    EmployeeOneCIdentity,
    OrganizationAccess,
    PayrollPlanItem,
    PayrollRow,
)


ZERO = Decimal("0.00")


def _name_tokens(value):
    return {
        token
        for token in normalize_onec_name(value).replace("(", " ").replace(")", " ").split()
        if token
    }


def _employee_name_parts(raw_name):
    parts = [part for part in (raw_name or "").split() if part]
    if len(parts) >= 3:
        return parts[1], parts[0], " ".join(parts[2:])
    return "", "", ""


def _active_access_users(organization):
    user_ids = (
        OrganizationAccess.objects.filter(
            organization=organization,
            user__is_active=True,
        )
        .values_list("user_id", flat=True)
        .distinct()
    )
    return list(User.objects.filter(id__in=user_ids, is_active=True).order_by("id"))


def _candidate_user(employee_name, users):
    employee_tokens = _name_tokens(employee_name)
    candidates = []
    for user in users:
        user_tokens = _name_tokens(" ".join(
            value for value in (user.first_name, user.last_name) if value
        ))
        if len(user_tokens) >= 2 and user_tokens.issubset(employee_tokens):
            candidates.append(user)
    return candidates[0] if len(candidates) == 1 else None


def consolidate_duplicate_employee_identities(organization, actor=None):
    """Merge only deterministic legacy/stable pairs for the exact same normalized name."""
    identities = list(
        EmployeeOneCIdentity.objects.filter(organization=organization)
        .select_related("employee")
        .order_by("normalized_name", "id")
    )
    groups = defaultdict(list)
    for identity in identities:
        groups[identity.normalized_name].append(identity)

    merged = []
    skipped = []
    for normalized_name, rows in groups.items():
        if len(rows) < 2:
            continue
        stable = [
            row for row in rows
            if row.onec_employee_id or row.personnel_number
        ]
        fallback = [
            row for row in rows
            if not row.onec_employee_id and not row.personnel_number
        ]
        if len(stable) != 1 or len(fallback) != 1 or len(rows) != 2:
            skipped.append(normalized_name)
            continue

        stable_row = stable[0]
        canonical = fallback[0]
        if (
            canonical.employee_id
            and stable_row.employee_id
            and canonical.employee_id != stable_row.employee_id
        ):
            skipped.append(normalized_name)
            continue

        with transaction.atomic():
            canonical = EmployeeOneCIdentity.objects.select_for_update().get(pk=canonical.pk)
            stable_row = EmployeeOneCIdentity.objects.select_for_update().get(pk=stable_row.pk)

            before = {
                "id": canonical.pk,
                "employee_id": canonical.employee_id,
                "onec_employee_id": canonical.onec_employee_id,
                "personnel_number": canonical.personnel_number,
                "department_name": canonical.department_name,
                "source_identity_key": canonical.source_identity_key,
                "status": canonical.status,
                "match_method": canonical.match_method,
            }
            PayrollRow.objects.filter(employee_identity=stable_row).update(
                employee_identity=canonical
            )
            PayrollPlanItem.objects.filter(employee_identity=stable_row).update(
                employee_identity=canonical
            )

            incoming_onec = stable_row.onec_employee_id
            incoming_personnel = stable_row.personnel_number
            incoming_employee_id = stable_row.employee_id
            incoming_confirmed_by_id = stable_row.confirmed_by_id
            incoming_confirmed_at = stable_row.confirmed_at
            incoming_comment = stable_row.comment

            stable_id = stable_row.pk
            stable_before = {
                "raw_name": stable_row.raw_name,
                "onec_employee_id": incoming_onec,
                "personnel_number": incoming_personnel,
                "employee_id": incoming_employee_id,
            }
            stable_row.delete()

            canonical.onec_employee_id = incoming_onec or canonical.onec_employee_id
            canonical.personnel_number = incoming_personnel or canonical.personnel_number
            canonical.employee_id = canonical.employee_id or incoming_employee_id
            canonical.confirmed_by_id = canonical.confirmed_by_id or incoming_confirmed_by_id
            canonical.confirmed_at = canonical.confirmed_at or incoming_confirmed_at
            canonical.comment = canonical.comment or incoming_comment
            if canonical.employee_id:
                if canonical.status != EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED:
                    canonical.status = EmployeeOneCIdentity.STATUS_AUTO_MATCHED
                    canonical.match_method = EmployeeOneCIdentity.MATCH_EXTERNAL_ID
            canonical.full_clean()
            canonical.save()

            DataAuditLog.objects.create(
                entity_type="EmployeeOneCIdentity",
                entity_id=str(canonical.pk),
                action=DataAuditLog.ACTION_UPDATE,
                organization=organization,
                actor=actor,
                before=before,
                after={
                    "id": canonical.pk,
                    "employee_id": canonical.employee_id,
                    "onec_employee_id": canonical.onec_employee_id,
                    "personnel_number": canonical.personnel_number,
                    "department_name": canonical.department_name,
                    "source_identity_key": canonical.source_identity_key,
                    "status": canonical.status,
                    "match_method": canonical.match_method,
                    "merged_identity_id": stable_id,
                },
                changed_fields=[
                    "onec_employee_id",
                    "personnel_number",
                    "employee_id",
                    "status",
                    "match_method",
                    "merged_identity_id",
                ],
            )
            DataAuditLog.objects.create(
                entity_type="EmployeeOneCIdentity",
                entity_id=str(stable_id),
                action=DataAuditLog.ACTION_DELETE,
                organization=organization,
                actor=actor,
                before=stable_before,
                after={"merged_into_identity_id": canonical.pk},
                changed_fields=["merged_into_identity_id"],
            )
            merged.append((stable_id, canonical.pk))
    return {"merged": merged, "skipped": skipped}


def bootstrap_employee_profiles(organization, actor=None):
    """Create one canonical Employee profile per exact 1C employee name and attach identities."""
    merge_result = consolidate_duplicate_employee_identities(organization, actor=actor)
    identities = list(
        EmployeeOneCIdentity.objects.filter(organization=organization)
        .select_related("employee")
        .order_by("normalized_name", "id")
    )
    groups = defaultdict(list)
    for identity in identities:
        groups[identity.normalized_name].append(identity)

    users = _active_access_users(organization)
    created = []
    linked_users = []
    attached = 0
    skipped = []

    for normalized_name, rows in groups.items():
        employee_ids = {row.employee_id for row in rows if row.employee_id}
        if len(employee_ids) > 1:
            skipped.append(normalized_name)
            continue

        employee = (
            Employee.objects.filter(pk=next(iter(employee_ids))).first()
            if employee_ids
            else None
        )
        raw_name = next((row.raw_name for row in rows if row.raw_name), normalized_name)
        department = next(
            (row.department_name for row in rows if row.department_name),
            "",
        )

        if employee is None:
            first_name, last_name, middle_name = _employee_name_parts(raw_name)
            employee = Employee.objects.create(
                organization=organization,
                first_name=first_name,
                last_name=last_name,
                middle_name=middle_name,
                display_name=raw_name,
                department_name=department,
                employment_status=Employee.STATUS_EMPLOYED,
                is_active=True,
            )
            created.append(employee.pk)
            DataAuditLog.objects.create(
                entity_type="Employee",
                entity_id=str(employee.pk),
                action=DataAuditLog.ACTION_CREATE,
                organization=organization,
                actor=actor,
                before={},
                after={
                    "display_name": employee.display_name,
                    "department_name": employee.department_name,
                },
                changed_fields=["display_name", "department_name"],
            )
        elif department and not employee.department_name:
            employee.department_name = department
            employee.save(update_fields=["department_name", "updated_at"])

        if employee.user_id is None:
            user = _candidate_user(employee.display_name, users)
            if user and not Employee.objects.filter(
                organization=organization,
                user=user,
            ).exclude(pk=employee.pk).exists():
                employee.user = user
                employee.save(update_fields=["user", "updated_at"])
                linked_users.append((employee.pk, user.pk))
                DataAuditLog.objects.create(
                    entity_type="Employee",
                    entity_id=str(employee.pk),
                    action=DataAuditLog.ACTION_UPDATE,
                    organization=organization,
                    actor=actor,
                    before={"user_id": None},
                    after={"user_id": user.pk},
                    changed_fields=["user_id"],
                )

        for identity in rows:
            if identity.employee_id == employee.pk:
                continue
            before = {
                "employee_id": identity.employee_id,
                "status": identity.status,
                "match_method": identity.match_method,
            }
            identity.employee = employee
            if identity.status != EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED:
                identity.status = EmployeeOneCIdentity.STATUS_AUTO_MATCHED
                identity.match_method = (
                    EmployeeOneCIdentity.MATCH_EXTERNAL_ID
                    if identity.onec_employee_id or identity.personnel_number
                    else EmployeeOneCIdentity.MATCH_EXACT
                )
            identity.save(update_fields=[
                "employee", "status", "match_method", "updated_at",
            ])
            attached += 1
            DataAuditLog.objects.create(
                entity_type="EmployeeOneCIdentity",
                entity_id=str(identity.pk),
                action=DataAuditLog.ACTION_UPDATE,
                organization=organization,
                actor=actor,
                before=before,
                after={
                    "employee_id": identity.employee_id,
                    "status": identity.status,
                    "match_method": identity.match_method,
                },
                changed_fields=["employee_id", "status", "match_method"],
            )

    return {
        "merge": merge_result,
        "created_employee_ids": created,
        "linked_users": linked_users,
        "attached_identities": attached,
        "skipped": skipped,
    }


def employee_current_plan(employee, period_month):
    items = (
        PayrollPlanItem.objects.filter(
            snapshot__organization=employee.organization,
            snapshot__period_month=period_month,
            employee_identity__employee=employee,
        )
        .select_related("snapshot", "employee_identity")
        .order_by("-snapshot__fetched_at", "-snapshot_id", "accrual_type_name")
    )
    latest_snapshot_id = items.values_list("snapshot_id", flat=True).first()
    if not latest_snapshot_id:
        return {
            "snapshot": None,
            "base_salary": ZERO,
            "other_plan_total": ZERO,
            "items": [],
        }
    current = list(items.filter(snapshot_id=latest_snapshot_id))
    return {
        "snapshot": current[0].snapshot if current else None,
        "base_salary": sum(
            (row.amount for row in current if row.is_base_salary),
            ZERO,
        ),
        "other_plan_total": sum(
            (row.amount for row in current if not row.is_base_salary),
            ZERO,
        ),
        "items": current,
    }
