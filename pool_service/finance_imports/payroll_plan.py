import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from pool_service.finance_imports.employee_matching import resolve_employee_identity
from pool_service.finance_imports.odata_payroll import (
    MAX_OUTPUT_BYTES,
    PayrollError,
    amount,
    guid,
    no_duplicate_keys,
)
from pool_service.finance_imports.odata_payroll_drafts import (
    auto_coverage_config,
    config_from_settings,
)
from pool_service.models import (
    DataAuditLog,
    PayrollPlanItem,
    PayrollPlanSnapshot,
)
from pool_service.services.finance import can_import_payroll
from pool_service.services.permissions import company_has_access


ZERO = Decimal("0.00")


class PayrollPlanSyncError(ValidationError):
    pass


def _month_start(value):
    return value.replace(day=1)


def _require_access(organization, user):
    if (
        not getattr(user, "is_active", False)
        or not can_import_payroll(user, organization)
        or not company_has_access(organization)
    ):
        raise PermissionDenied("Недостаточно прав для обновления окладов.")


def _configured_scope(config):
    try:
        organizations = sorted(
            {
                guid(value.strip())
                for value in config["ONEC_ODATA_ORGANIZATION_GUIDS"].split(",")
                if value.strip()
            }
        )
        currency = guid(config["ONEC_ODATA_PAYROLL_CURRENCY_GUID"])
    except (KeyError, PayrollError, AttributeError):
        raise PayrollPlanSyncError(
            "Настройка организаций или валюты ФОТ в 1С некорректна."
        ) from None
    if not organizations or len(organizations) > 40:
        raise PayrollPlanSyncError("Не настроен охват организаций ФОТ.")
    return organizations, currency


def _read_plan_payload(config, as_of):
    environment = os.environ.copy()
    environment.update(config)
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).with_name("odata_payroll_plan.py")),
        "--app-dir",
        str(settings.BASE_DIR),
        "--as-of",
        as_of.isoformat(),
    ]
    try:
        result = subprocess.run(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=65,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PayrollPlanSyncError(
            "1С не ответила вовремя. Предыдущие оклады сохранены."
        ) from None
    if result.returncode or len(result.stdout) > MAX_OUTPUT_BYTES:
        raise PayrollPlanSyncError(
            "Не удалось получить оклады из 1С. Предыдущие данные сохранены."
        )
    try:
        return json.loads(
            result.stdout.decode("utf-8"),
            object_pairs_hook=no_duplicate_keys,
        )
    except (ValueError, UnicodeError):
        raise PayrollPlanSyncError(
            "1С вернула некорректные данные окладов. Предыдущие данные сохранены."
        ) from None


def _validated_items(payload, as_of, organizations, currency):
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "payroll_plan_snapshot_v1"
        or payload.get("period_month") != _month_start(as_of).isoformat()
        or payload.get("as_of") != as_of.isoformat()
        or payload.get("selected_organizations") != organizations
        or payload.get("currency_guid") != currency
        or type(payload.get("source_rows")) is not int
        or payload["source_rows"] < 0
        or not isinstance(payload.get("items"), list)
    ):
        raise PayrollPlanSyncError("Структура данных окладов из 1С изменилась.")

    items = []
    seen = set()
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise PayrollPlanSyncError("Некорректная строка оклада из 1С.")
        try:
            org = guid(item.get("organization_guid"))
            employee = guid(item.get("employee_guid"))
            accrual_type = guid(item.get("accrual_type_guid"))
            source_period = date.fromisoformat(item.get("source_period"))
            value = amount(item.get("amount"))
        except (PayrollError, TypeError, ValueError):
            raise PayrollPlanSyncError("Некорректная строка оклада из 1С.") from None
        if org not in organizations or source_period > as_of:
            raise PayrollPlanSyncError("Оклад относится к другому периоду или организации.")
        employee_name = item.get("employee_name")
        type_name = item.get("accrual_type_name")
        if (
            not isinstance(employee_name, str)
            or not employee_name.strip()
            or len(employee_name) > 500
            or not isinstance(type_name, str)
            or not type_name.strip()
            or len(type_name) > 300
            or type(item.get("is_base_salary")) is not bool
            or type(item.get("source_rows")) is not int
            or item["source_rows"] <= 0
            or value != value.quantize(Decimal(".01"))
        ):
            raise PayrollPlanSyncError("Некорректная строка оклада из 1С.")
        key = (org, employee, accrual_type)
        if key in seen:
            raise PayrollPlanSyncError("1С вернула повторную строку планового начисления.")
        seen.add(key)
        items.append(
            {
                "organization_guid": org,
                "employee_guid": employee,
                "employee_name": employee_name.strip(),
                "accrual_type_guid": accrual_type,
                "accrual_type_name": type_name.strip(),
                "amount": value,
                "source_period": source_period,
                "is_base_salary": item["is_base_salary"],
            }
        )
    return items


def refresh_payroll_plan_snapshot(organization, user, *, as_of=None):
    _require_access(organization, user)
    auto_coverage_config()
    config = config_from_settings()
    organizations, currency = _configured_scope(config)
    as_of = as_of or timezone.localdate()
    payload = _read_plan_payload(config, as_of)
    items = _validated_items(payload, as_of, organizations, currency)

    fingerprint_payload = [
        {
            "organization_guid": item["organization_guid"],
            "employee_guid": item["employee_guid"],
            "employee_name": item["employee_name"],
            "accrual_type_guid": item["accrual_type_guid"],
            "accrual_type_name": item["accrual_type_name"],
            "amount": format(item["amount"], "f"),
            "source_period": item["source_period"].isoformat(),
            "is_base_salary": item["is_base_salary"],
        }
        for item in items
    ]
    source_hash = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with transaction.atomic():
        existing = PayrollPlanSnapshot.objects.filter(
            organization=organization,
            period_month=_month_start(as_of),
            source_hash=source_hash,
        ).first()
        if existing:
            return existing, False

        snapshot = PayrollPlanSnapshot.objects.create(
            organization=organization,
            period_month=_month_start(as_of),
            source_hash=source_hash,
            source_rows=payload["source_rows"],
            source_organization_guids=organizations,
            currency_guid=currency,
            fetched_by=user,
        )
        identity_cache = {}
        rows = []
        for item in items:
            identity = identity_cache.get(item["employee_guid"])
            if identity is None:
                identity = resolve_employee_identity(
                    organization,
                    item["employee_name"],
                    onec_employee_id=item["employee_guid"],
                )
                identity_cache[item["employee_guid"]] = identity
            rows.append(
                PayrollPlanItem(
                    snapshot=snapshot,
                    employee_identity=identity,
                    onec_employee_id=item["employee_guid"],
                    employee_raw_name=item["employee_name"],
                    accrual_type_id=item["accrual_type_guid"],
                    accrual_type_name=item["accrual_type_name"],
                    amount=item["amount"],
                    is_base_salary=item["is_base_salary"],
                    source_period=item["source_period"],
                    source_organization_guid=item["organization_guid"],
                )
            )
        PayrollPlanItem.objects.bulk_create(rows)
        DataAuditLog.objects.create(
            entity_type="PayrollPlanSnapshot",
            entity_id=str(snapshot.pk),
            action=DataAuditLog.ACTION_CREATE,
            organization=organization,
            actor=user,
            before={},
            after={
                "period_month": snapshot.period_month.isoformat(),
                "source_hash": source_hash,
                "source_rows": snapshot.source_rows,
                "items": len(rows),
            },
            changed_fields=["period_month", "source_hash", "source_rows", "items"],
        )
    return snapshot, True


def payroll_compensation_dashboard_data(organization, period_month):
    period_month = _month_start(period_month)
    snapshot = (
        PayrollPlanSnapshot.objects.filter(
            organization=organization,
            period_month=period_month,
        )
        .order_by("-fetched_at", "-id")
        .first()
    )
    if snapshot is None:
        return {
            "period_month": period_month,
            "has_data": False,
            "snapshot": None,
            "employees": [],
            "base_salary_total": ZERO,
            "percent_total": ZERO,
            "bonus_total": ZERO,
            "total": ZERO,
            "other_plan_total": ZERO,
            "other_plan_items": 0,
        }

    grouped = {}
    other_total = ZERO
    other_items = 0
    for item in snapshot.items.select_related(
        "employee_identity__employee"
    ).order_by("employee_raw_name", "id"):
        identity = item.employee_identity
        key = identity.pk
        row = grouped.setdefault(
            key,
            {
                "employee_name": (
                    identity.employee.display_name
                    if identity.employee_id and identity.employee.display_name
                    else item.employee_raw_name
                ),
                "department_name": identity.department_name,
                "base_salary": ZERO,
                "percent_amount": ZERO,
                "bonus_amount": ZERO,
                "total": ZERO,
            },
        )
        if item.is_base_salary:
            row["base_salary"] += item.amount
        else:
            other_total += item.amount
            other_items += 1

    employees = []
    for row in grouped.values():
        row["total"] = row["base_salary"] + row["percent_amount"] + row["bonus_amount"]
        employees.append(row)
    employees.sort(key=lambda row: (-row["total"], row["employee_name"].casefold()))

    base_total = sum((row["base_salary"] for row in employees), ZERO)
    percent_total = sum((row["percent_amount"] for row in employees), ZERO)
    bonus_total = sum((row["bonus_amount"] for row in employees), ZERO)
    return {
        "period_month": period_month,
        "has_data": True,
        "snapshot": snapshot,
        "employees": employees,
        "base_salary_total": base_total,
        "percent_total": percent_total,
        "bonus_total": bonus_total,
        "total": base_total + percent_total + bonus_total,
        "other_plan_total": other_total,
        "other_plan_items": other_items,
    }
