import hashlib
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from pool_service.models import DataAuditLog, Employee, EmployeeOneCIdentity


def normalize_onec_name(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold().replace("ё", "е")


def _employee_name(employee):
    return employee.display_name or " ".join(
        value for value in (employee.last_name, employee.first_name, employee.middle_name) if value
    )


def _source_identity_key(normalized_name, normalized_department_name):
    payload = f"{normalized_name}\x1f{normalized_department_name}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _optional_identifier(value):
    return re.sub(r"\s+", " ", str(value or "").strip()) or None


def _stable_identity(organization, onec_employee_id=None, personnel_number=None):
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


def _enrich_stable_identifiers(identity, *, onec_employee_id=None, personnel_number=None):
    try:
        with transaction.atomic():
            locked = EmployeeOneCIdentity.objects.select_for_update().get(pk=identity.pk)
            updates = []
            for field, incoming in (
                ("onec_employee_id", onec_employee_id),
                ("personnel_number", personnel_number),
            ):
                current = getattr(locked, field)
                if current and incoming and current != incoming:
                    raise ValidationError(
                        "Новые стабильные идентификаторы конфликтуют с identity."
                    )
                if incoming and not current:
                    setattr(locked, field, incoming)
                    updates.append(field)
            if updates:
                locked.save(update_fields=[*updates, "updated_at"])
            return locked
    except IntegrityError as exc:
        raise ValidationError(
            "Стабильный идентификатор уже относится к другой identity."
        ) from exc


def resolve_employee_identity(
    organization,
    raw_name,
    *,
    department_name="",
    onec_employee_id=None,
    personnel_number=None,
):
    normalized_name = normalize_onec_name(raw_name)
    normalized_department_name = normalize_onec_name(department_name)
    onec_employee_id = _optional_identifier(onec_employee_id)
    personnel_number = _optional_identifier(personnel_number)
    stable = _stable_identity(organization, onec_employee_id, personnel_number)
    if stable:
        return _enrich_stable_identifiers(
            stable,
            onec_employee_id=onec_employee_id,
            personnel_number=personnel_number,
        )

    has_stable_identifier = bool(onec_employee_id or personnel_number)
    fallback_key = _source_identity_key(
        normalized_name, normalized_department_name
    )
    canonical = EmployeeOneCIdentity.objects.filter(
        organization=organization, source_identity_key=fallback_key
    ).first()
    if canonical:
        return _enrich_stable_identifiers(
            canonical,
            onec_employee_id=onec_employee_id,
            personnel_number=personnel_number,
        )
    source_identity_key = None if has_stable_identifier else fallback_key

    confirmed = [
        identity for identity in
        EmployeeOneCIdentity.objects.filter(
            organization=organization,
            normalized_name=normalized_name,
            employee__isnull=False,
            status__in=[
                EmployeeOneCIdentity.STATUS_AUTO_MATCHED,
                EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
            ],
        ).order_by("id")
        if identity.normalized_department_name in {"", normalized_department_name}
    ]
    confirmed_employee_ids = {identity.employee_id for identity in confirmed}
    if not has_stable_identifier and len(confirmed_employee_ids) == 1:
        canonical = confirmed[0]
        canonical.normalized_department_name = normalized_department_name
        canonical.source_identity_key = source_identity_key
        try:
            with transaction.atomic():
                canonical.save(update_fields=[
                    "normalized_department_name", "source_identity_key", "updated_at",
                ])
            return canonical
        except IntegrityError:
            return EmployeeOneCIdentity.objects.get(
                organization=organization, source_identity_key=source_identity_key
            )

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
    values = {
        "employee": employee,
        "raw_name": raw_name,
        "normalized_name": normalized_name,
        "normalized_department_name": normalized_department_name,
        "source_identity_key": source_identity_key,
        "onec_employee_id": onec_employee_id,
        "personnel_number": personnel_number,
        "department_name": department_name,
        "status": status,
        "match_method": method,
    }
    try:
        with transaction.atomic():
            return EmployeeOneCIdentity.objects.create(organization=organization, **values)
    except IntegrityError:
        if not source_identity_key:
            raise
        return EmployeeOneCIdentity.objects.get(
            organization=organization, source_identity_key=source_identity_key
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
