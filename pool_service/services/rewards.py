from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone

from pool_service.models import Employee, OneCMonthlyProfit
from pool_service.reward_models import (
    OneCAuthorIdentity,
    RewardAdjustment,
    RewardMonthClose,
    RewardParticipation,
    RewardParticipationChange,
    RewardSchemeVersion,
)
from pool_service.services.finance import can_access_management_finance

MONEY = Decimal("0.01")
ONE = Decimal("1.000000")
RETAIL_CHECK = "Document_ЧекККМ"
RETAIL_REPORT = "Document_ОтчетОРозничныхПродажах"
REALIZATION = "Document_РасходнаяНакладная"


def money(value):
    return Decimal(value or 0).quantize(MONEY, rounding=ROUND_HALF_UP)


def month_start(value):
    if isinstance(value, date):
        return value.replace(day=1)
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError) as exc:
        raise ValidationError("Период должен быть в формате YYYY-MM.") from exc
    return parsed


def _management_role(user, organization):
    return can_access_management_finance(user, organization)


def can_view_rewards(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.view_employee_rewards")
        )
    )


def can_manage_participation(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.manage_employee_reward_participation")
        )
    )


def can_manage_rules(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.manage_employee_reward_rules")
        )
    )


def can_close_period(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.close_employee_reward_period")
        )
    )


def scheme_for_month(organization, period_month):
    return (
        RewardSchemeVersion.objects.filter(
            organization=organization,
            effective_from__lte=period_month,
        )
        .filter(models_q_effective(period_month))
        .order_by("-effective_from", "-version")
        .first()
    )


def models_q_effective(period_month):
    from django.db.models import Q
    return Q(effective_to__isnull=True) | Q(effective_to__gte=period_month)


def ensure_test_scheme(organization, user, period_month):
    scheme = scheme_for_month(organization, period_month)
    if scheme:
        return scheme
    if not can_manage_rules(user, organization):
        raise PermissionDenied
    return RewardSchemeVersion.objects.create(
        organization=organization,
        name="Тестовая схема №1",
        version=1,
        effective_from=period_month,
        created_by=user,
    )


def create_scheme_version(organization, user, *, effective_from, values):
    if not can_manage_rules(user, organization):
        raise PermissionDenied
    effective_from = month_start(effective_from)
    latest = (
        RewardSchemeVersion.objects.filter(organization=organization, name="Тестовая схема №1")
        .order_by("-version")
        .first()
    )
    version = (latest.version if latest else 0) + 1
    if latest and (latest.effective_to is None or latest.effective_to >= effective_from):
        previous_day = effective_from - timedelta(days=1)
        latest.effective_to = previous_day
        latest.save(update_fields=["effective_to"])
    allowed = {
        "documentation_retail_fixed", "documentation_document_fixed",
        "sale_rate", "project_rate", "work_rate", "client_manager_rate",
    }
    fields = {key: Decimal(str(value)) for key, value in values.items() if key in allowed}
    return RewardSchemeVersion.objects.create(
        organization=organization,
        name="Тестовая схема №1",
        version=version,
        effective_from=effective_from,
        created_by=user,
        **fields,
    )


def _row_document_key(row):
    data = row.source_data or {}
    recorder_type = data.get("recorder_type") or ""
    recorder = str(data.get("recorder") or row.source_recorder or "")
    return f"odata-source:{row.organization_id}:{recorder_type}:{recorder}"


def _row_gp(row):
    if row.cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED:
        return None
    value = row.displayed_gross_profit
    return None if value is None else money(value)


def active_profit_rows(organization, period_month):
    return list(
        OneCMonthlyProfit.objects.active_for(organization)
        .filter(period_month=period_month)
        .select_related("import_batch")
        .order_by("source_row_number", "id")
    )


def _ensure_required_documentation(organization, user, period_month, key, row, *, basis, author_identity=None):
    data = row.source_data or {}
    existing = RewardParticipation.objects.filter(
        organization=organization,
        period_month=period_month,
        role=RewardParticipation.ROLE_DOCUMENTATION,
        scope_key=key,
        source_document_key=key,
        employee__isnull=True,
        author_identity=author_identity,
        status=RewardParticipation.STATUS_REQUIRED,
    ).first()
    if existing:
        return existing, False
    item = RewardParticipation.objects.create(
        organization=organization,
        employee=None,
        author_identity=author_identity,
        role=RewardParticipation.ROLE_DOCUMENTATION,
        status=RewardParticipation.STATUS_REQUIRED,
        share=ONE,
        period_month=period_month,
        scope_key=key,
        source_document_key=key,
        source_document_type=data.get("recorder_type") or "",
        source_document_guid=str(data.get("recorder") or row.source_recorder or ""),
        source_document_number=data.get("document_number") or "",
        source_document_date=_safe_date(data.get("document_date") or data.get("source_date")),
        scope_line_identities=[],
        customer_name=row.customer_name,
        basis=basis,
        assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
        created_by=user,
    )
    RewardParticipationChange.objects.create(
        participation=item,
        actor=user,
        before={},
        after=participation_snapshot(item),
        reason=basis,
    )
    return item, True


@transaction.atomic
def sync_author_proposals(organization, user, period_month):
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    if RewardMonthClose.objects.filter(organization=organization, period_month=period_month).exists():
        raise ValidationError("Месяц закрыт. Новые назначения оформляются корректировкой.")
    rows = active_profit_rows(organization, period_month)
    by_doc = {}
    for row in rows:
        data = row.source_data or {}
        recorder_type = data.get("recorder_type")
        key = _row_document_key(row)
        by_doc.setdefault(key, row)
    created = 0
    issues = 0
    for key, row in by_doc.items():
        data = row.source_data or {}
        author_guid = (data.get("author_guid") or "").strip()
        author_name = (data.get("author_name") or "").strip()
        if data.get("recorder_type") == RETAIL_REPORT:
            _, was_created = _ensure_required_documentation(
                organization, user, period_month, key, row,
                basis="Агрегирующий отчёт о розничных продажах: автор отчёта не назначается оформителем чеков.",
            )
            created += int(was_created)
            issues += 1
            continue
        if not author_guid:
            _, was_created = _ensure_required_documentation(
                organization, user, period_month, key, row,
                basis="Автор исходного документа 1С отсутствует — требуется сопоставление вручную.",
            )
            created += int(was_created)
            issues += 1
            continue
        identity, _ = OneCAuthorIdentity.objects.get_or_create(
            organization=organization,
            onec_user_id=author_guid,
            defaults={"raw_name": author_name, "status": OneCAuthorIdentity.STATUS_NEEDS_MAPPING},
        )
        if author_name and identity.raw_name != author_name:
            identity.raw_name = author_name
            identity.save(update_fields=["raw_name", "updated_at"])
        if identity.status == OneCAuthorIdentity.STATUS_TECHNICAL:
            _, was_created = _ensure_required_documentation(
                organization, user, period_month, key, row,
                basis="Автор — техническая учётная запись 1С; сотрудник не назначен автоматически.",
                author_identity=identity,
            )
            created += int(was_created)
            issues += 1
            continue
        if identity.status == OneCAuthorIdentity.STATUS_EXCLUDED:
            continue
        employee = identity.employee if identity.status == OneCAuthorIdentity.STATUS_MAPPED else None
        status = RewardParticipation.STATUS_PENDING if employee else RewardParticipation.STATUS_REQUIRED
        defaults = {
            "employee": employee,
            "author_identity": identity,
            "status": status,
            "share": ONE,
            "period_month": period_month,
            "source_document_type": data.get("recorder_type") or "",
            "source_document_guid": str(data.get("recorder") or row.source_recorder or ""),
            "source_document_number": data.get("document_number") or "",
            "source_document_date": _safe_date(data.get("document_date") or data.get("source_date")),
            "scope_line_identities": [],
            "customer_name": row.customer_name,
            "basis": "Автор исходного документа 1С",
            "assignment_source": RewardParticipation.SOURCE_ONEC_AUTHOR,
            "created_by": user,
        }
        _, was_created = RewardParticipation.objects.get_or_create(
            organization=organization,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            scope_key=key,
            source_document_key=key,
            author_identity=identity,
            defaults=defaults,
        )
        created += int(was_created)
    return {"created": created, "issues": issues}


def _safe_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


@transaction.atomic
def map_author(identity, employee, user):
    if not can_manage_participation(user, identity.organization):
        raise PermissionDenied
    if employee.organization_id != identity.organization_id:
        raise ValidationError("Сотрудник относится к другой организации.")
    identity.employee = employee
    identity.status = OneCAuthorIdentity.STATUS_MAPPED
    identity.confirmed_by = user
    identity.confirmed_at = timezone.now()
    identity.save(update_fields=["employee", "status", "confirmed_by", "confirmed_at", "updated_at"])
    identity.reward_participations.filter(status=RewardParticipation.STATUS_REQUIRED).update(
        employee=employee,
        status=RewardParticipation.STATUS_PENDING,
        updated_at=timezone.now(),
    )


@transaction.atomic
def save_participation(participation, user, *, employee, role, share, status, line_identities=None):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    if RewardMonthClose.objects.filter(
        organization=participation.organization, period_month=participation.period_month
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if employee and employee.organization_id != participation.organization_id:
        raise ValidationError("Сотрудник относится к другой организации.")
    share = Decimal(str(share)).quantize(Decimal("0.000001"))
    if share < 0 or share > 1:
        raise ValidationError("Доля должна быть от 0 до 100%.")
    before = participation_snapshot(participation)
    participation.employee = employee
    participation.role = role
    participation.share = share
    participation.status = status
    if line_identities is not None:
        participation.scope_line_identities = list(dict.fromkeys(line_identities))
    if status == RewardParticipation.STATUS_CONFIRMED:
        participation.confirmed_by = user
        participation.confirmed_at = timezone.now()
    participation.full_clean()
    participation.save()
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
    )
    return participation


@transaction.atomic
def confirm_participation(participation, user):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    if RewardMonthClose.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if not participation.employee_id:
        raise ValidationError("Нельзя подтвердить участие без сотрудника.")
    confirmed_share = RewardParticipation.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
        scope_key=participation.scope_key,
        role=participation.role,
        status=RewardParticipation.STATUS_CONFIRMED,
    ).exclude(pk=participation.pk).aggregate(total=models.Sum("share"))["total"] or Decimal("0")
    if confirmed_share + participation.share > ONE:
        raise ValidationError("Подтверждённые доли по роли превышают 100%.")
    before = participation_snapshot(participation)
    participation.status = RewardParticipation.STATUS_CONFIRMED
    participation.confirmed_by = user
    participation.confirmed_at = timezone.now()
    participation.save(update_fields=[
        "status", "confirmed_by", "confirmed_at", "updated_at"
    ])
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
        reason="Подтверждение участия",
    )
    return participation


def participation_snapshot(item):
    return {
        "employee_id": item.employee_id,
        "role": item.role,
        "share": str(item.share),
        "status": item.status,
        "scope_line_identities": item.scope_line_identities,
    }


def _scope_rows(participation, rows_by_identity, rows_by_document):
    if participation.scope_line_identities:
        return [rows_by_identity[key] for key in participation.scope_line_identities if key in rows_by_identity]
    return rows_by_document.get(participation.source_document_key, [])


def _rate_for(role, scheme):
    return {
        RewardParticipation.ROLE_SALE: scheme.sale_rate,
        RewardParticipation.ROLE_PROJECT: scheme.project_rate,
        RewardParticipation.ROLE_WORK: scheme.work_rate,
        RewardParticipation.ROLE_CLIENT_MANAGER: scheme.client_manager_rate,
    }.get(role)


def _fixed_for(document_type, scheme):
    return scheme.documentation_retail_fixed if document_type == RETAIL_CHECK else scheme.documentation_document_fixed


def calculate_month(organization, period_month, *, employee_id=None, use_closed=True):
    period_month = month_start(period_month)
    closed = RewardMonthClose.objects.filter(organization=organization, period_month=period_month).first()
    if closed and use_closed:
        snapshot = closed.snapshot
        if employee_id:
            snapshot = dict(snapshot)
            snapshot["employees"] = [x for x in snapshot.get("employees", []) if x.get("employee_id") == employee_id]
            snapshot["details"] = [x for x in snapshot.get("details", []) if x.get("employee_id") == employee_id]
        return snapshot

    scheme = scheme_for_month(organization, period_month)
    rows = active_profit_rows(organization, period_month)
    rows_by_identity = {row.source_identity: row for row in rows}
    rows_by_document = defaultdict(list)
    for row in rows:
        original_key = _row_document_key(row)
        business_key = _row_business_scope_key(row)
        rows_by_document[original_key].append(row)
        if business_key != original_key:
            rows_by_document[business_key].append(row)

    participations = list(
        RewardParticipation.objects.filter(organization=organization, period_month=period_month)
        .select_related("employee", "author_identity")
        .order_by("scope_key", "role", "employee_id", "id")
    )
    groups = defaultdict(list)
    for item in participations:
        groups[(item.scope_key, item.role)].append(item)

    details = []
    issues = []
    employee_totals = defaultdict(lambda: {
        "documentation_count": 0, "seller_revenue": Decimal("0"), "seller_gp": Decimal("0"),
        "documentation_reward": Decimal("0"), "sale_reward": Decimal("0"),
        "project_reward": Decimal("0"), "work_reward": Decimal("0"),
        "adjustments": Decimal("0"), "review_count": 0,
    })

    if scheme is None:
        issues.append({"kind": "missing_scheme", "label": "Нет версии тестовой схемы для месяца."})

    for (scope_key, role), items in groups.items():
        confirmed = [x for x in items if x.status == RewardParticipation.STATUS_CONFIRMED and x.employee_id]
        pending = [x for x in items if x.status not in {RewardParticipation.STATUS_CONFIRMED, RewardParticipation.STATUS_NOT_APPLICABLE}]
        if pending:
            issues.append({"kind": "unconfirmed", "label": scope_key, "count": len(pending)})
            for x in pending:
                if x.employee_id:
                    employee_totals[x.employee_id]["review_count"] += 1
        if not confirmed or scheme is None:
            continue
        scope_rows = _scope_rows(confirmed[0], rows_by_identity, rows_by_document)
        if not scope_rows:
            issues.append({"kind": "missing_base", "label": scope_key, "count": 1})
            continue
        revenue = money(sum((Decimal(row.revenue or 0) for row in scope_rows), Decimal("0")))
        if role == RewardParticipation.ROLE_DOCUMENTATION:
            base = money(sum((_row_gp(row) or Decimal("0") for row in scope_rows), Decimal("0")))
        else:
            gp_values = [_row_gp(row) for row in scope_rows]
            if any(value is None for value in gp_values):
                issues.append({"kind": "missing_cost", "label": scope_key, "count": 1})
                for x in confirmed:
                    employee_totals[x.employee_id]["review_count"] += 1
                continue
            base = money(sum(gp_values, Decimal("0")))
        share_total = sum((x.share for x in confirmed), Decimal("0"))
        if share_total > ONE:
            issues.append({"kind": "share_overflow", "label": scope_key, "count": 1})
            continue
        if share_total < ONE:
            issues.append({"kind": "unallocated", "label": scope_key, "share": str(ONE - share_total)})

        document_type = confirmed[0].source_document_type
        if role == RewardParticipation.ROLE_DOCUMENTATION:
            fund = money(_fixed_for(document_type, scheme))
        else:
            rate = _rate_for(role, scheme)
            fund = money(max(base, Decimal("0")) * Decimal(rate or 0))
        allocations = _allocate(fund, confirmed, full=(share_total == ONE))
        for item, amount in allocations:
            totals = employee_totals[item.employee_id]
            if role == RewardParticipation.ROLE_DOCUMENTATION:
                totals["documentation_count"] += 1
                totals["documentation_reward"] += amount
                rate_label = f"{_fixed_for(document_type, scheme):.2f} ₽"
            else:
                rate = _rate_for(role, scheme) or Decimal("0")
                rate_label = f"{(rate * 100):.2f}%"
                if role == RewardParticipation.ROLE_SALE:
                    totals["seller_revenue"] += money(revenue * item.share)
                    totals["seller_gp"] += money(base * item.share)
                    totals["sale_reward"] += amount
                elif role == RewardParticipation.ROLE_PROJECT:
                    totals["project_reward"] += amount
                elif role == RewardParticipation.ROLE_WORK:
                    totals["work_reward"] += amount
            details.append({
                "employee_id": item.employee_id,
                "employee": item.employee.display_name,
                "customer": item.customer_name,
                "document": _document_label(item),
                "role": item.get_role_display(),
                "base": str(base),
                "rate": rate_label,
                "share": str(item.share),
                "amount": str(amount),
                "status": item.get_status_display(),
                "scope_key": scope_key,
            })

    adjustments = RewardAdjustment.objects.filter(
        organization=organization, period_month=period_month, status=RewardAdjustment.STATUS_CONFIRMED
    ).select_related("employee")
    for adjustment in adjustments:
        employee_totals[adjustment.employee_id]["adjustments"] += money(adjustment.amount)

    employees = []
    employee_map = {e.id: e for e in Employee.objects.filter(organization=organization)}
    for eid, totals in employee_totals.items():
        if employee_id and eid != employee_id:
            continue
        total = money(
            totals["documentation_reward"] + totals["sale_reward"] + totals["project_reward"]
            + totals["work_reward"] + totals["adjustments"]
        )
        employees.append({
            "employee_id": eid,
            "employee": employee_map[eid].display_name if eid in employee_map else f"#{eid}",
            **{k: (str(money(v)) if isinstance(v, Decimal) else v) for k, v in totals.items()},
            "total": str(total),
        })
    employees.sort(key=lambda x: x["employee"].casefold())

    unmapped_authors = list(
        OneCAuthorIdentity.objects.filter(
            organization=organization, status=OneCAuthorIdentity.STATUS_NEEDS_MAPPING,
            reward_participations__period_month=period_month,
        ).distinct().values("id", "onec_user_id", "raw_name")
    )
    if unmapped_authors:
        issues.append({"kind": "unmapped_author", "label": "Несопоставленные авторы 1С", "count": len(unmapped_authors)})

    return {
        "period_month": period_month.isoformat(),
        "is_test": True,
        "closed": False,
        "scheme": _scheme_payload(scheme),
        "employees": employees,
        "details": [x for x in details if not employee_id or x["employee_id"] == employee_id],
        "issues": issues,
        "unmapped_authors": unmapped_authors,
    }


def _allocate(fund, participations, *, full):
    result = []
    running = Decimal("0")
    ordered = sorted(participations, key=lambda x: (x.employee_id or 0, x.id or 0))
    for index, item in enumerate(ordered):
        if full and index == len(ordered) - 1:
            amount = money(fund - running)
        else:
            amount = money(fund * item.share)
        running += amount
        result.append((item, amount))
    return result


def _document_label(item):
    parts = []
    if item.source_document_number:
        parts.append(f"№{item.source_document_number}")
    if item.source_document_date:
        parts.append(f"от {item.source_document_date:%d.%m.%Y}")
    return " ".join(parts) or item.source_document_key


def _scheme_payload(scheme):
    if scheme is None:
        return None
    return {
        "id": scheme.id, "name": scheme.name, "version": scheme.version,
        "effective_from": scheme.effective_from.isoformat(),
        "documentation_retail_fixed": str(scheme.documentation_retail_fixed),
        "documentation_document_fixed": str(scheme.documentation_document_fixed),
        "sale_rate": str(scheme.sale_rate), "project_rate": str(scheme.project_rate),
        "work_rate": str(scheme.work_rate), "client_manager_rate": str(scheme.client_manager_rate),
    }


@transaction.atomic
def close_month(organization, user, period_month):
    if not can_close_period(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    if RewardMonthClose.objects.filter(organization=organization, period_month=period_month).exists():
        raise ValidationError("Месяц уже закрыт.")
    scheme = scheme_for_month(organization, period_month)
    if scheme is None:
        raise ValidationError("Нельзя закрыть месяц без версии тестовой схемы.")
    snapshot = calculate_month(organization, period_month, use_closed=False)
    blocking = {"missing_scheme", "missing_base", "missing_cost", "share_overflow", "unmapped_author", "unconfirmed", "unallocated"}
    if any(issue.get("kind") in blocking for issue in snapshot["issues"]):
        raise ValidationError("Есть неполные или неподтверждённые данные; месяц не закрыт.")
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    obj = RewardMonthClose.objects.create(
        organization=organization,
        period_month=period_month,
        scheme_version=scheme,
        snapshot={**snapshot, "closed": True},
        source_hash=hashlib.sha256(raw).hexdigest(),
        closed_by=user,
    )
    return obj


def _row_business_scope_key(row):
    """Stable order scope when available; otherwise the original sale document."""
    data = row.source_data or {}
    order_guid = data.get("resolved_order_guid") or data.get("direct_expense_order_guid")
    if order_guid:
        return f"odata-order:{row.organization_id}:{str(order_guid).lower()}"
    return _row_document_key(row)


def reward_document_options(organization, period_month):
    """Read-only assignment scopes built only from active confirmed Service2 profit rows."""
    period_month = month_start(period_month)
    rows = active_profit_rows(organization, period_month)
    groups = defaultdict(list)
    for row in rows:
        groups[_row_business_scope_key(row)].append(row)
    result = []
    for scope_key, scope_rows in groups.items():
        sale_rows = [
            row for row in scope_rows
            if (row.source_data or {}).get("row_kind") != "direct_order_expense"
        ]
        primary = sale_rows[0] if sale_rows else scope_rows[0]
        data = primary.source_data or {}
        label = (
            data.get("resolved_order_display")
            or data.get("document_display")
            or primary.document_name
            or scope_key
        )
        lines = []
        for row in scope_rows:
            row_data = row.source_data or {}
            gp = _row_gp(row)
            lines.append({
                "identity": row.source_identity,
                "name": row.nomenclature,
                "type": row.nomenclature_type,
                "revenue": str(money(row.revenue or 0)),
                "gross_profit": None if gp is None else str(gp),
                "cost_missing": gp is None,
                "is_direct_expense": row_data.get("row_kind") == "direct_order_expense",
            })
        result.append({
            "scope_key": scope_key,
            "label": label,
            "customer": primary.customer_name,
            "source_document_type": data.get("recorder_type") or "",
            "source_document_guid": str(data.get("recorder") or primary.source_recorder or ""),
            "source_document_number": data.get("document_number") or data.get("resolved_order_number") or "",
            "source_document_date": _safe_date(data.get("document_date") or data.get("resolved_order_date") or data.get("source_date")),
            "lines": lines,
        })
    result.sort(key=lambda item: ((item["customer"] or "").casefold(), item["label"].casefold()))
    return result


def _assignment_scope_key(document_key, role, line_identities):
    normalized = sorted(set(line_identities or []))
    suffix = "all"
    if normalized:
        digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()[:20]
        suffix = f"lines:{digest}"
    return f"{document_key}:{role}:{suffix}"


@transaction.atomic
def create_manual_participation(
    organization,
    user,
    period_month,
    *,
    document_key,
    employee,
    role,
    share,
    line_identities=None,
    not_applicable=False,
):
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    if RewardMonthClose.objects.filter(organization=organization, period_month=period_month).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    roles = {value for value, _ in RewardParticipation.ROLE_CHOICES}
    if role not in roles:
        raise ValidationError("Неизвестная роль участия.")
    options = {item["scope_key"]: item for item in reward_document_options(organization, period_month)}
    document = options.get(document_key)
    if document is None:
        raise ValidationError("Документ не относится к активным подтверждённым данным месяца.")
    allowed_lines = {item["identity"] for item in document["lines"] if not item["is_direct_expense"]}
    selected = list(dict.fromkeys(line_identities or []))
    if any(identity not in allowed_lines for identity in selected):
        raise ValidationError("Выбранная строка не относится к документу или является прямой затратой.")
    if role in {RewardParticipation.ROLE_PROJECT, RewardParticipation.ROLE_WORK} and not selected and not not_applicable:
        raise ValidationError("Для проекта или выполнения работ нужно выбрать конкретные позиции/работы.")
    if not_applicable:
        employee = None
        share_decimal = Decimal("0")
        status = RewardParticipation.STATUS_NOT_APPLICABLE
    else:
        if employee is None or employee.organization_id != organization.id:
            raise ValidationError("Нужно выбрать сотрудника этой организации.")
        share_decimal = Decimal(str(share)).quantize(Decimal("0.000001"))
        if share_decimal <= 0 or share_decimal > ONE:
            raise ValidationError("Доля должна быть больше 0 и не больше 100%.")
        status = RewardParticipation.STATUS_PENDING
    scope_key = _assignment_scope_key(document_key, role, selected)
    existing = RewardParticipation.objects.filter(
        organization=organization,
        period_month=period_month,
        scope_key=scope_key,
        role=role,
        employee=employee,
        status__in=[RewardParticipation.STATUS_PENDING, RewardParticipation.STATUS_CONFIRMED, RewardParticipation.STATUS_NOT_APPLICABLE],
    ).first()
    if existing:
        raise ValidationError("Такое участие уже добавлено.")
    item = RewardParticipation(
        organization=organization,
        employee=employee,
        role=role,
        status=status,
        share=share_decimal,
        period_month=period_month,
        scope_key=scope_key,
        source_document_key=document_key,
        source_document_type=document["source_document_type"],
        source_document_guid=document["source_document_guid"],
        source_document_number=document["source_document_number"],
        source_document_date=document["source_document_date"],
        scope_line_identities=selected,
        customer_name=document["customer"],
        basis="Ручное распределение по подтверждённым строкам ВП",
        assignment_source=RewardParticipation.SOURCE_MANUAL,
        created_by=user,
    )
    item.full_clean()
    item.save()
    RewardParticipationChange.objects.create(
        participation=item,
        actor=user,
        before={},
        after=participation_snapshot(item),
        reason="Создание назначения",
    )
    return item


@transaction.atomic
def add_documentation_participant(participation, employee, share, user):
    """Add a co-documenter to the same fixed-fee unit; never creates another fund."""
    if participation.role != RewardParticipation.ROLE_DOCUMENTATION:
        raise ValidationError("Совместный оформитель добавляется только к роли оформления.")
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    if employee.organization_id != participation.organization_id:
        raise ValidationError("Сотрудник относится к другой организации.")
    share = Decimal(str(share)).quantize(Decimal("0.000001"))
    if share <= 0 or share > ONE:
        raise ValidationError("Доля должна быть больше 0 и не больше 100%.")
    item = RewardParticipation.objects.create(
        organization=participation.organization,
        employee=employee,
        role=participation.role,
        status=RewardParticipation.STATUS_PENDING,
        share=share,
        period_month=participation.period_month,
        scope_key=participation.scope_key,
        source_document_key=participation.source_document_key,
        source_document_type=participation.source_document_type,
        source_document_guid=participation.source_document_guid,
        source_document_number=participation.source_document_number,
        source_document_date=participation.source_document_date,
        scope_line_identities=list(participation.scope_line_identities),
        customer_name=participation.customer_name,
        object_label=participation.object_label,
        basis="Совместное оформление комплекта документов",
        assignment_source=RewardParticipation.SOURCE_MANUAL,
        created_by=user,
    )
    RewardParticipationChange.objects.create(
        participation=item,
        actor=user,
        before={},
        after=participation_snapshot(item),
        reason="Добавлен совместный оформитель",
    )
    return item


@transaction.atomic
def confirm_participation_batch(organization, user, period_month, participation_ids):
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    ids = list(dict.fromkeys(int(value) for value in participation_ids))
    items = list(
        RewardParticipation.objects.select_for_update()
        .filter(
            organization=organization,
            period_month=period_month,
            pk__in=ids,
        )
        .select_related("employee")
        .order_by("scope_key", "role", "id")
    )
    if len(items) != len(ids):
        raise ValidationError("Часть выбранных назначений недоступна.")
    if any(item.status == RewardParticipation.STATUS_NOT_APPLICABLE for item in items):
        raise ValidationError("Строка «не применяется» не требует подтверждения.")
    for item in items:
        if item.status != RewardParticipation.STATUS_CONFIRMED:
            confirm_participation(item, user)
    return items
