import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pool_service.models import DataAuditLog, Employee, EmployeeOneCIdentity


def normalize_onec_name(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold().replace("ё", "е")


def _employee_name(employee):
    return employee.display_name or " ".join(
        value for value in (employee.last_name, employee.first_name, employee.middle_name) if value
    )


def _stable_identity(organization, onec_employee_id="", personnel_number=""):
    matches = []
    if onec_employee_id:
        match = EmployeeOneCIdentity.objects.filter(
            organization=organization, onec_employee_id=onec_employee_id
        ).first()
        if match:
            matches.append(match)
    if personnel_number:
        match = EmployeeOneCIdentity.objects.filter(
            organization=organization, personnel_number=personnel_number
        ).first()
        if match:
            matches.append(match)
    if matches and any(match.pk != matches[0].pk for match in matches[1:]):
        raise ValidationError("Идентификатор 1С и табельный номер относятся к разным identity.")
    return matches[0] if matches else None


def resolve_employee_identity(
    organization,
    raw_name,
    *,
    department_name="",
    onec_employee_id="",
    personnel_number="",
):
    normalized_name = normalize_onec_name(raw_name)
    stable = _stable_identity(organization, onec_employee_id, personnel_number)
    if stable:
        return stable

    confirmed = list(
        EmployeeOneCIdentity.objects.filter(
            organization=organization,
            normalized_name=normalized_name,
            employee__isnull=False,
            status__in=[
                EmployeeOneCIdentity.STATUS_AUTO_MATCHED,
                EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
            ],
        ).order_by("id")
    )
    confirmed_employee_ids = {identity.employee_id for identity in confirmed}
    if len(confirmed_employee_ids) == 1:
        return confirmed[0]

    candidates = [
        employee
        for employee in Employee.objects.filter(organization=organization).order_by("id")
        if normalize_onec_name(_employee_name(employee)) == normalized_name
    ]
    employee = candidates[0] if len(candidates) == 1 else None
    if len(candidates) == 1:
        status = EmployeeOneCIdentity.STATUS_AUTO_MATCHED
        method = EmployeeOneCIdentity.MATCH_EXACT
    elif len(candidates) > 1 or len(confirmed_employee_ids) > 1:
        status = EmployeeOneCIdentity.STATUS_AMBIGUOUS
        method = EmployeeOneCIdentity.MATCH_NONE
    else:
        status = EmployeeOneCIdentity.STATUS_NOT_FOUND
        method = EmployeeOneCIdentity.MATCH_NONE
    return EmployeeOneCIdentity.objects.create(
        organization=organization,
        employee=employee,
        raw_name=raw_name,
        normalized_name=normalized_name,
        onec_employee_id=onec_employee_id,
        personnel_number=personnel_number,
        department_name=department_name,
        status=status,
        match_method=method,
    )


def confirm_employee_identity(identity, employee, user, *, comment=""):
    if identity.organization_id != employee.organization_id:
        raise ValidationError("Employee и identity относятся к разным организациям.")
    with transaction.atomic():
        locked = EmployeeOneCIdentity.objects.select_for_update().get(pk=identity.pk)
        before = {
            "employee_id": locked.employee_id,
            "status": locked.status,
            "match_method": locked.match_method,
        }
        locked.employee = employee
        locked.status = EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED
        locked.match_method = EmployeeOneCIdentity.MATCH_MANUAL
        locked.confirmed_by = user
        locked.confirmed_at = timezone.now()
        locked.comment = comment
        locked.full_clean()
        locked.save()
        after = {
            "employee_id": locked.employee_id,
            "status": locked.status,
            "match_method": locked.match_method,
        }
        DataAuditLog.objects.create(
            entity_type="EmployeeOneCIdentity",
            entity_id=str(locked.pk),
            action=DataAuditLog.ACTION_UPDATE,
            organization=locked.organization,
            actor=user,
            before=before,
            after=after,
            changed_fields=sorted(key for key in before if before[key] != after[key]),
        )
    return locked
