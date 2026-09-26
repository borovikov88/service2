from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.finance_imports.profit_dashboard import (
    apply_period_analytics,
    classify_nomenclature_type,
)
from pool_service.models import (
    Employee,
    EmployeeOneCUserIdentity,
    EmployeeRewardAdjustment,
    EmployeeRewardAssignment,
    EmployeeRewardAssignmentChange,
    EmployeeRewardAssignmentLine,
    EmployeeRewardMonthClose,
    EmployeeRewardRule,
    EmployeeRewardScheme,
    EmployeeRewardTemplate,
    OneCMonthlyProfit,
)


MONEY = Decimal("0.01")
HUNDRED = Decimal("100")
ZERO = Decimal("0")
RETAIL_CHECK = "Document_ЧекККМ"
RETAIL_REPORT = "Document_ОтчетОРозничныхПродажах"
REALIZATION = "Document_РасходнаяНакладная"
PAPERWORK_TYPES = {RETAIL_CHECK, REALIZATION}

DECIMAL_SNAPSHOT_KEYS = {
    "seller_revenue",
    "seller_gp",
    "paperwork_amount",
    "sale_amount",
    "project_amount",
    "work_amount",
    "adjustments",
    "total",
    "base",
    "revenue",
    "gross_profit",
    "amount",
    "fund",
    "unallocated",
    "share_percent",
}


def quantize_money(value):
    return Decimal(value or 0).quantize(MONEY, rounding=ROUND_HALF_UP)


def _source_data(row):
    return row.source_data if isinstance(row.source_data, dict) else {}


def _guid_text(value):
    value = str(value or "").strip().lower()
    if not value or value == "00000000-0000-0000-0000-000000000000":
        return None
    return value


def reward_document_ref(row):
    """Return the real sale document, never an aggregate retail report author."""
    data = _source_data(row)
    explicit_type = data.get("reward_document_type")
    explicit_guid = _guid_text(data.get("reward_document_guid"))
    if explicit_type in PAPERWORK_TYPES and explicit_guid:
        return explicit_type, explicit_guid

    recorder_type = data.get("recorder_type")
    recorder = _guid_text(data.get("recorder"))
    linked_type = data.get("document_type")
    linked_guid = _guid_text(data.get("document_guid"))

    if recorder_type in PAPERWORK_TYPES and recorder:
        return recorder_type, recorder
    if recorder_type == RETAIL_REPORT and linked_type == RETAIL_CHECK and linked_guid:
        return linked_type, linked_guid
    return None


def _resolved_order_guid(row):
    data = _source_data(row)
    return _guid_text(data.get("resolved_order_guid") or data.get("order_guid"))


def _document_label(row):
    data = _source_data(row)
    value = (
        data.get("reward_document_display")
        or data.get("document_display")
        or row.document_name
        or "Документ 1С"
    )
    return str(value).strip()[:500]


def _document_period(row):
    data = _source_data(row)
    raw = data.get("reward_document_date") or data.get("document_date") or data.get("source_date")
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10]).replace(day=1)
        except ValueError:
            pass
    return row.period_month


def active_profit_rows(organization, period_month):
    rows = list(
        OneCMonthlyProfit.objects.active_for(organization)
        .filter(period_month=period_month)
        .select_related("import_batch")
        .order_by("source_recorder", "source_row_number", "id")
    )
    apply_period_analytics(rows)
    return rows


def document_rows(organization, period_month, document_type, document_guid):
    guid = str(document_guid).lower()
    return [
        row
        for row in active_profit_rows(organization, period_month)
        if reward_document_ref(row) == (document_type, guid)
    ]


def register_author_identities_from_profit_rows(organization, rows):
    """Persist 1C user identities only. Never guess an Employee from a user GUID/name."""
    found = {}
    for row in rows:
        data = _source_data(row)
        user_guid = _guid_text(data.get("author_user_guid"))
        user_name = str(data.get("author_user_name") or "").strip()
        if user_guid and user_name:
            found[user_guid] = user_name[:500]
    for user_guid, user_name in found.items():
        identity, created = EmployeeOneCUserIdentity.objects.get_or_create(
            organization=organization,
            onec_user_id=user_guid,
            defaults={
                "display_name": user_name,
                "status": EmployeeOneCUserIdentity.STATUS_NEEDS_CONFIRMATION,
            },
        )
        if not created and identity.display_name != user_name:
            identity.display_name = user_name
            identity.save(update_fields=["display_name", "updated_at"])
    return len(found)


def _document_author_groups(rows):
    groups = {}
    for row in rows:
        ref = reward_document_ref(row)
        if not ref:
            continue
        data = _source_data(row)
        user_guid = _guid_text(data.get("author_user_guid"))
        key = (row.period_month, *ref)
        item = groups.setdefault(
            key,
            {
                "period_month": row.period_month,
                "document_type": ref[0],
                "document_guid": ref[1],
                "label": _document_label(row),
                "authors": set(),
                "rows": [],
            },
        )
        if user_guid:
            item["authors"].add(user_guid)
        item["rows"].append(row)
    return groups


def is_standalone_retail_document(rows):
    if not rows:
        return False
    refs = {reward_document_ref(row) for row in rows}
    if len(refs) != 1 or next(iter(refs))[0] != RETAIL_CHECK:
        return False
    return not any(_resolved_order_guid(row) for row in rows)


def seed_author_paperwork_proposals(organization, rows, proposed_by=None):
    """Create one proposed paperwork assignment per real sale unit after explicit user mapping."""
    register_author_identities_from_profit_rows(organization, rows)
    identities = {
        str(item.onec_user_id).lower(): item
        for item in EmployeeOneCUserIdentity.objects.filter(organization=organization)
        .select_related("employee")
    }
    created_count = 0
    for item in _document_author_groups(rows).values():
        if len(item["authors"]) != 1:
            continue
        author_guid = next(iter(item["authors"]))
        identity = identities.get(author_guid)
        if (
            not identity
            or identity.status != EmployeeOneCUserIdentity.STATUS_CONFIRMED
            or not identity.employee_id
        ):
            continue
        doc_rows = item["rows"]
        if item["document_type"] == RETAIL_CHECK and not is_standalone_retail_document(doc_rows):
            # A linked/payment check must not create a second fixed reward.
            continue
        scope_key = f'document:{item["document_type"]}:{item["document_guid"]}'
        _, created = EmployeeRewardAssignment.objects.get_or_create(
            organization=organization,
            period_month=item["period_month"],
            source_document_type=item["document_type"],
            source_document_guid=item["document_guid"],
            scope_key=scope_key,
            role=EmployeeRewardRule.ROLE_PAPERWORK,
            employee=identity.employee,
            defaults={
                "source_document_label": item["label"],
                "share_percent": HUNDRED,
                "status": EmployeeRewardAssignment.STATUS_PROPOSED,
                "source_kind": EmployeeRewardAssignment.SOURCE_ONEC_AUTHOR,
                "basis_note": "Автор исходного документа 1С",
                "proposed_by": proposed_by,
            },
        )
        if created:
            created_count += 1
    return created_count


def active_scheme(organization, period_month):
    return (
        EmployeeRewardScheme.objects.filter(
            organization=organization,
            is_active=True,
            effective_from__lte=period_month,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period_month))
        .prefetch_related("rules")
        .order_by("-effective_from", "-version")
        .first()
    )


def _rule_unit(assignment, scope_rows):
    if assignment.role == EmployeeRewardRule.ROLE_CLIENT_MANAGER:
        return EmployeeRewardRule.UNIT_CLIENT_MANAGER
    if assignment.role == EmployeeRewardRule.ROLE_SALE:
        return EmployeeRewardRule.UNIT_SALE
    if assignment.role == EmployeeRewardRule.ROLE_PROJECT:
        return EmployeeRewardRule.UNIT_PROJECT
    if assignment.role == EmployeeRewardRule.ROLE_WORK:
        return EmployeeRewardRule.UNIT_WORK
    if assignment.role == EmployeeRewardRule.ROLE_PAPERWORK:
        if (
            assignment.source_document_type == RETAIL_CHECK
            and is_standalone_retail_document(scope_rows)
        ):
            return EmployeeRewardRule.UNIT_PAPERWORK_RETAIL
        return EmployeeRewardRule.UNIT_PAPERWORK_PACKAGE
    raise ValidationError("Неизвестная роль вознаграждения.")


def _rule_map(scheme):
    if not scheme:
        return {}
    return {(rule.role, rule.unit_kind): rule for rule in scheme.rules.all()}


def _scope_rows(assignment, all_rows):
    explicit = list(assignment.lines.all())
    if explicit:
        identities = {item.source_identity for item in explicit}
        return [row for row in all_rows if row.source_identity in identities]

    if assignment.scope_key.startswith("order:"):
        order_guid = assignment.scope_key.split(":", 1)[1].lower()
        return [row for row in all_rows if _resolved_order_guid(row) == order_guid]

    doc_ref = (
        assignment.source_document_type,
        str(assignment.source_document_guid).lower(),
    )
    return [row for row in all_rows if reward_document_ref(row) == doc_ref]


def _is_direct_expense(row):
    return _source_data(row).get("row_kind") == "direct_order_expense"


def _role_rows(role, rows):
    if role != EmployeeRewardRule.ROLE_WORK:
        return rows
    return [
        row
        for row in rows
        if _is_direct_expense(row)
        or classify_nomenclature_type(row.nomenclature_type) == "service"
    ]


def _scope_financials(role, rows):
    selected = _role_rows(role, rows)
    revenue = sum((row.dashboard_revenue for row in selected), ZERO)
    missing_cost = any(
        row.dashboard_analytical_cost is None and row.dashboard_revenue != 0
        for row in selected
    )
    if missing_cost:
        return {
            "rows": selected,
            "revenue": quantize_money(revenue),
            "gross_profit": None,
            "complete": False,
        }
    gp = sum((row.dashboard_gross_profit or ZERO for row in selected), ZERO)
    return {
        "rows": selected,
        "revenue": quantize_money(revenue),
        "gross_profit": quantize_money(gp),
        "complete": True,
    }


def _rule_label(rule):
    if not rule:
        return "Нет правила"
    if rule.calculation_kind == EmployeeRewardRule.KIND_FIXED:
        return f"{quantize_money(rule.fixed_amount)} ₽ за единицу"
    if rule.calculation_kind == EmployeeRewardRule.KIND_PERCENT:
        return f"{rule.rate_percent.normalize()}% ВП"
    return "Информационная роль"


def _fund(rule, base):
    if not rule:
        return None
    if rule.calculation_kind == EmployeeRewardRule.KIND_INFORMATION:
        return ZERO
    if rule.calculation_kind == EmployeeRewardRule.KIND_FIXED:
        return quantize_money(rule.fixed_amount)
    if base is None:
        return None
    if base <= 0:
        return ZERO
    return quantize_money(base * rule.rate_percent / HUNDRED)


def _allocate_fund(assignments, fund):
    """Round to kopecks and make a 100% group equal its fund exactly."""
    result = {}
    if fund is None:
        return result, None
    ordered = sorted(assignments, key=lambda item: item.id)
    total_share = sum((item.share_percent for item in ordered), ZERO)
    allocated = ZERO
    for index, assignment in enumerate(ordered):
        if total_share == HUNDRED and index == len(ordered) - 1:
            amount = quantize_money(fund - allocated)
        else:
            amount = quantize_money(fund * assignment.share_percent / HUNDRED)
        result[assignment.id] = amount
        allocated += amount
    unallocated = quantize_money(fund - allocated) if total_share < HUNDRED else ZERO
    return result, unallocated


def _empty_employee_row(employee):
    return {
        "employee": employee,
        "employee_id": employee.id,
        "employee_name": employee.display_name,
        "paperwork_units": ZERO,
        "seller_revenue": ZERO,
        "seller_gp": ZERO,
        "paperwork_amount": ZERO,
        "sale_amount": ZERO,
        "project_amount": ZERO,
        "work_amount": ZERO,
        "adjustments": ZERO,
        "total": ZERO,
        "review_count": 0,
        "details": [],
    }


def _detail(assignment, financials, rule, amount):
    rows = financials["rows"]
    names = []
    for row in rows:
        name = (row.nomenclature or "").strip()
        if name and name not in names:
            names.append(name)
    return {
        "assignment_id": assignment.id,
        "document_label": assignment.source_document_label or "Документ 1С",
        "document_type": assignment.source_document_type,
        "document_guid": str(assignment.source_document_guid),
        "role": assignment.get_role_display(),
        "role_code": assignment.role,
        "positions": ", ".join(names[:6]) + ("…" if len(names) > 6 else ""),
        "base": financials["gross_profit"],
        "revenue": financials["revenue"],
        "rate_label": _rule_label(rule),
        "share_percent": assignment.share_percent,
        "amount": amount,
        "status": assignment.get_status_display(),
        "status_code": assignment.status,
        "basis_complete": financials["complete"],
        "source_kind": assignment.get_source_kind_display(),
    }


def _document_inventory(all_rows):
    result = {}
    for row in all_rows:
        ref = reward_document_ref(row)
        if not ref:
            continue
        key = (ref[0], ref[1])
        item = result.setdefault(
            key,
            {
                "document_type": ref[0],
                "document_guid": ref[1],
                "document_label": _document_label(row),
                "period_month": row.period_month,
                "customer_name": row.customer_name,
                "customer_guid": _guid_text(_source_data(row).get("customer_guid")),
                "rows": [],
            },
        )
        item["rows"].append(row)
    return result


def _open_dashboard_data(organization, period_month, employee_id=None):
    all_rows = active_profit_rows(organization, period_month)
    scheme = active_scheme(organization, period_month)
    rules = _rule_map(scheme)
    employees = list(
        Employee.objects.filter(organization=organization, is_active=True)
        .select_related("user")
        .order_by("display_name", "id")
    )
    if employee_id is not None:
        employees = [item for item in employees if item.id == employee_id]
    summary = {item.id: _empty_employee_row(item) for item in employees}

    assignments_qs = (
        EmployeeRewardAssignment.objects.filter(
            organization=organization,
            period_month=period_month,
        )
        .select_related("employee")
        .prefetch_related("lines")
        .order_by("role", "scope_key", "id")
    )
    if employee_id is not None:
        assignments_qs = assignments_qs.filter(
            Q(employee_id=employee_id) | Q(employee__isnull=True)
        )
    assignments = list(assignments_qs)
    groups = defaultdict(list)
    for assignment in assignments:
        groups[(assignment.role, assignment.scope_key)].append(assignment)

    issues = []
    missing_basis = []
    pending_assignments = []
    for (role, scope_key), group in groups.items():
        confirmed = [
            item for item in group
            if item.status == EmployeeRewardAssignment.STATUS_CONFIRMED
            and item.employee_id
        ]
        for item in group:
            if item.status in {
                EmployeeRewardAssignment.STATUS_REQUIRED,
                EmployeeRewardAssignment.STATUS_PROPOSED,
            }:
                pending_assignments.append(item)
                if item.employee_id in summary:
                    summary[item.employee_id]["review_count"] += 1

        if not confirmed:
            continue
        share_total = sum((item.share_percent for item in confirmed), ZERO)
        if share_total > HUNDRED:
            issues.append({
                "kind": "share_overflow",
                "label": confirmed[0].source_document_label,
                "detail": f"Подтверждённые доли роли превышают 100%: {share_total}%.",
            })
            for item in confirmed:
                if item.employee_id in summary:
                    summary[item.employee_id]["review_count"] += 1
            continue

        scope_rows = _scope_rows(confirmed[0], all_rows)
        financials = _scope_financials(role, scope_rows)
        unit = _rule_unit(confirmed[0], scope_rows)
        rule = rules.get((role, unit))
        if not financials["complete"] and role in {
            EmployeeRewardRule.ROLE_SALE,
            EmployeeRewardRule.ROLE_PROJECT,
            EmployeeRewardRule.ROLE_WORK,
        }:
            issue = {
                "kind": "missing_basis",
                "label": confirmed[0].source_document_label,
                "detail": "Себестоимость части объёма не определена; процентная сумма не утверждается.",
            }
            issues.append(issue)
            missing_basis.append(issue)
        if not rule:
            issues.append({
                "kind": "missing_rule",
                "label": confirmed[0].source_document_label,
                "detail": "Для подтверждённой роли нет действующего правила.",
            })

        base = financials["gross_profit"]
        fund = _fund(rule, base)
        amounts, unallocated = _allocate_fund(confirmed, fund)
        if unallocated and unallocated > 0:
            issues.append({
                "kind": "unallocated_fund",
                "label": confirmed[0].source_document_label,
                "detail": f"Нераспределённая часть фонда: {unallocated} ₽.",
                "fund": fund,
                "unallocated": unallocated,
            })

        for item in confirmed:
            if item.employee_id not in summary:
                continue
            row = summary[item.employee_id]
            amount = amounts.get(item.id)
            row["details"].append(_detail(item, financials, rule, amount))
            if amount is None:
                row["review_count"] += 1
                continue
            if role == EmployeeRewardRule.ROLE_PAPERWORK:
                row["paperwork_units"] += item.share_percent / HUNDRED
                row["paperwork_amount"] += amount
            elif role == EmployeeRewardRule.ROLE_SALE:
                row["seller_revenue"] += quantize_money(
                    financials["revenue"] * item.share_percent / HUNDRED
                )
                if base is not None:
                    row["seller_gp"] += quantize_money(
                        base * item.share_percent / HUNDRED
                    )
                row["sale_amount"] += amount
            elif role == EmployeeRewardRule.ROLE_PROJECT:
                row["project_amount"] += amount
            elif role == EmployeeRewardRule.ROLE_WORK:
                row["work_amount"] += amount

    inventory = _document_inventory(all_rows)
    assigned_docs = {
        (item.source_document_type, str(item.source_document_guid).lower())
        for item in assignments
        if item.status != EmployeeRewardAssignment.STATUS_NOT_APPLICABLE
    }
    unassigned_documents = [
        item for key, item in inventory.items() if key not in assigned_docs
    ]

    author_identities = {
        str(item.onec_user_id).lower(): item
        for item in EmployeeOneCUserIdentity.objects.filter(organization=organization)
        .select_related("employee")
    }
    unmapped_authors = []
    seen_unmapped = set()
    for doc in inventory.values():
        author_guids = {
            _guid_text(_source_data(row).get("author_user_guid"))
            for row in doc["rows"]
        } - {None}
        if not author_guids:
            marker = (None, doc["document_guid"])
            if marker not in seen_unmapped:
                seen_unmapped.add(marker)
                unmapped_authors.append({
                    "author_guid": None,
                    "author_name": "Автор отсутствует",
                    "document_label": doc["document_label"],
                })
            continue
        for guid in author_guids:
            identity = author_identities.get(guid)
            if (
                identity is None
                or identity.status != EmployeeOneCUserIdentity.STATUS_CONFIRMED
            ):
                marker = (guid, doc["document_guid"])
                if marker not in seen_unmapped:
                    seen_unmapped.add(marker)
                    unmapped_authors.append({
                        "author_guid": guid,
                        "author_name": (
                            identity.display_name if identity else
                            next(
                                (
                                    str(_source_data(row).get("author_user_name") or "")
                                    for row in doc["rows"]
                                    if _guid_text(_source_data(row).get("author_user_guid")) == guid
                                ),
                                "",
                            )
                        ),
                        "document_label": doc["document_label"],
                    })

    for item in summary.values():
        item["paperwork_amount"] = quantize_money(item["paperwork_amount"])
        item["sale_amount"] = quantize_money(item["sale_amount"])
        item["project_amount"] = quantize_money(item["project_amount"])
        item["work_amount"] = quantize_money(item["work_amount"])
        item["seller_revenue"] = quantize_money(item["seller_revenue"])
        item["seller_gp"] = quantize_money(item["seller_gp"])

    data = {
        "period_month": period_month,
        "scheme": scheme,
        "scheme_name": str(scheme) if scheme else "Нет действующей схемы",
        "rows": list(summary.values()),
        "issues": issues,
        "unassigned_documents": unassigned_documents,
        "unmapped_authors": unmapped_authors,
        "missing_basis": missing_basis,
        "pending_assignments": pending_assignments,
        "is_closed": False,
        "close": None,
    }
    _apply_adjustments(data, organization, period_month)
    period_financials = _scope_financials(EmployeeRewardRule.ROLE_SALE, all_rows)
    total_rewards = quantize_money(
        sum((item["total"] for item in data["rows"]), ZERO)
    )
    period_gp = period_financials["gross_profit"]
    if (
        period_financials["complete"]
        and period_gp is not None
        and total_rewards > period_gp
    ):
        data["issues"].append({
            "kind": "rewards_exceed_gp",
            "label": f"Итог за {period_month:%m.%Y}",
            "detail": (
                f"Тестовые вознаграждения {total_rewards} ₽ превышают "
                f"итоговую ВП {period_gp} ₽."
            ),
        })
    return data


def _snapshot_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_snapshot_value(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "employee":
                continue
            if hasattr(item, "pk"):
                continue
            result[key] = _snapshot_value(item)
        return result
    if hasattr(value, "pk"):
        return None
    return value


def _snapshot_dashboard(data):
    return {
        "period_month": data["period_month"].isoformat(),
        "scheme_name": data["scheme_name"],
        "rows": [_snapshot_value(item) for item in data["rows"]],
        "issues": _snapshot_value(data["issues"]),
        "unassigned_documents": _snapshot_value(data["unassigned_documents"]),
        "unmapped_authors": _snapshot_value(data["unmapped_authors"]),
        "missing_basis": _snapshot_value(data["missing_basis"]),
    }


def _restore_values(value, key=None):
    if isinstance(value, list):
        return [_restore_values(item) for item in value]
    if isinstance(value, dict):
        return {k: _restore_values(v, k) for k, v in value.items()}
    if key in DECIMAL_SNAPSHOT_KEYS and value not in (None, ""):
        return Decimal(str(value))
    return value


def _closed_dashboard_data(organization, close, employee_id=None):
    payload = _restore_values(close.result_data)
    employee_map = {
        item.id: item
        for item in Employee.objects.filter(organization=organization).select_related("user")
    }
    rows = []
    for raw in payload.get("rows", []):
        if employee_id is not None and raw.get("employee_id") != employee_id:
            continue
        employee = employee_map.get(raw.get("employee_id"))
        raw["employee"] = employee
        rows.append(raw)
    data = {
        "period_month": close.period_month,
        "scheme": close.scheme,
        "scheme_name": payload.get("scheme_name", str(close.scheme)),
        "rows": rows,
        "issues": payload.get("issues", []),
        "unassigned_documents": payload.get("unassigned_documents", []),
        "unmapped_authors": payload.get("unmapped_authors", []),
        "missing_basis": payload.get("missing_basis", []),
        "pending_assignments": [],
        "is_closed": True,
        "close": close,
    }
    _apply_adjustments(data, organization, close.period_month)
    return data


def _apply_adjustments(data, organization, period_month):
    by_employee = {item["employee_id"]: item for item in data["rows"]}
    adjustments = list(
        EmployeeRewardAdjustment.objects.filter(
            organization=organization,
            period_month=period_month,
        ).select_related("employee")
    )
    data["adjustment_rows"] = adjustments
    for adjustment in adjustments:
        row = by_employee.get(adjustment.employee_id)
        if not row:
            continue
        if adjustment.status == EmployeeRewardAdjustment.STATUS_CONFIRMED:
            row["adjustments"] = quantize_money(row.get("adjustments", ZERO) + adjustment.amount)
            row["details"].append({
                "assignment_id": None,
                "document_label": "Корректировка",
                "document_type": "",
                "document_guid": "",
                "role": "Корректировка",
                "role_code": "adjustment",
                "positions": adjustment.reason,
                "base": None,
                "revenue": None,
                "rate_label": "Отдельная подтверждённая корректировка",
                "share_percent": HUNDRED,
                "amount": quantize_money(adjustment.amount),
                "status": adjustment.get_status_display(),
                "status_code": adjustment.status,
                "basis_complete": True,
                "source_kind": "Корректировка",
            })
        elif adjustment.status == EmployeeRewardAdjustment.STATUS_PROPOSED:
            row["review_count"] = int(row.get("review_count", 0)) + 1
    for row in data["rows"]:
        row["total"] = quantize_money(
            row.get("paperwork_amount", ZERO)
            + row.get("sale_amount", ZERO)
            + row.get("project_amount", ZERO)
            + row.get("work_amount", ZERO)
            + row.get("adjustments", ZERO)
        )


def reward_dashboard_data(organization, period_month, employee_id=None):
    close = (
        EmployeeRewardMonthClose.objects.filter(
            organization=organization,
            period_month=period_month,
        )
        .select_related("scheme", "closed_by")
        .first()
    )
    if close:
        return _closed_dashboard_data(organization, close, employee_id=employee_id)
    return _open_dashboard_data(organization, period_month, employee_id=employee_id)


def _scope_key(document_type, document_guid, rows, line_identities, role):
    if line_identities:
        digest = hashlib.sha256(
            "\0".join(sorted(line_identities)).encode("utf-8")
        ).hexdigest()[:24]
        return f"lines:{digest}"
    if role != EmployeeRewardRule.ROLE_PAPERWORK:
        order_guids = {_resolved_order_guid(row) for row in rows} - {None}
        if len(order_guids) == 1:
            return f"order:{next(iter(order_guids))}"
    return f"document:{document_type}:{str(document_guid).lower()}"


@transaction.atomic
def create_or_update_assignment(
    *,
    organization,
    period_month,
    document_type,
    document_guid,
    role,
    employee,
    share_percent,
    actor,
    line_identities=None,
    confirm=False,
    source_kind=EmployeeRewardAssignment.SOURCE_MANUAL,
    basis_note="",
):
    if EmployeeRewardMonthClose.objects.filter(
        organization=organization, period_month=period_month
    ).exists():
        raise ValidationError(
            "Месяц закрыт. Изменения участия оформляются отдельной корректировкой."
        )
    if employee.organization_id != organization.id:
        raise ValidationError("Сотрудник относится к другой организации.")
    try:
        share = Decimal(str(share_percent))
    except Exception as exc:
        raise ValidationError("Некорректная доля участия.") from exc
    if share <= 0 or share > HUNDRED:
        raise ValidationError("Доля должна быть больше 0 и не больше 100%.")

    rows = document_rows(organization, period_month, document_type, document_guid)
    if not rows:
        raise ValidationError("Активные подтверждённые строки документа не найдены.")
    available = {row.source_identity: row for row in rows}
    requested = sorted(set(line_identities or []))
    if any(identity not in available for identity in requested):
        raise ValidationError("Выбраны строки, которые не принадлежат активному документу.")
    if any(not identity.startswith("odata:") for identity in requested):
        raise ValidationError("Для распределения по строкам требуется устойчивая OData identity.")
    if role == EmployeeRewardRule.ROLE_PROJECT and not requested:
        raise ValidationError(
            "Проект / расчёт подтверждается только по явно выбранным позициям."
        )

    scope_key = _scope_key(
        document_type, document_guid, rows, requested, role
    )
    status = (
        EmployeeRewardAssignment.STATUS_CONFIRMED
        if confirm else EmployeeRewardAssignment.STATUS_PROPOSED
    )
    assignment = (
        EmployeeRewardAssignment.objects.filter(
            organization=organization,
            period_month=period_month,
            scope_key=scope_key,
            role=role,
            employee=employee,
        ).first()
    )
    before = {}
    if assignment:
        before = {
            "share_percent": format(assignment.share_percent, "f"),
            "status": assignment.status,
            "source_kind": assignment.source_kind,
        }
        assignment.share_percent = share
        assignment.status = status
        assignment.source_kind = source_kind
        assignment.basis_note = basis_note[:500]
        assignment.source_document_label = _document_label(rows[0])
        assignment.proposed_by = actor
    else:
        assignment = EmployeeRewardAssignment(
            organization=organization,
            period_month=period_month,
            source_document_type=document_type,
            source_document_guid=document_guid,
            source_document_label=_document_label(rows[0]),
            scope_key=scope_key,
            role=role,
            employee=employee,
            share_percent=share,
            status=status,
            source_kind=source_kind,
            basis_note=basis_note[:500],
            proposed_by=actor,
        )
    if confirm:
        assignment.confirmed_by = actor
        assignment.confirmed_at = timezone.now()
    assignment.full_clean()
    assignment.save()
    assignment.lines.all().delete()
    EmployeeRewardAssignmentLine.objects.bulk_create([
        EmployeeRewardAssignmentLine(
            assignment=assignment,
            source_identity=identity,
            nomenclature=available[identity].nomenclature,
            nomenclature_type=available[identity].nomenclature_type,
        )
        for identity in requested
    ])
    EmployeeRewardAssignmentChange.objects.create(
        assignment=assignment,
        actor=actor,
        action="confirmed" if confirm else "proposed",
        before=before,
        after={
            "share_percent": format(assignment.share_percent, "f"),
            "status": assignment.status,
            "source_kind": assignment.source_kind,
            "line_identities": requested,
        },
    )
    return assignment


@transaction.atomic
def create_reward_template(
    *,
    organization,
    source_customer_guid,
    client,
    pool,
    role,
    employee,
    share_percent,
    effective_from,
    actor,
):
    """Create a future suggestion template; it never rewrites existing assignments."""
    guid = _guid_text(source_customer_guid)
    if not guid:
        raise ValidationError("Для шаблона нужен устойчивый GUID клиента 1С.")
    if client is None and pool is None:
        raise ValidationError("Шаблон должен быть привязан к клиенту или объекту Service2.")
    if client is not None and client.organization_id != organization.id:
        raise ValidationError("Клиент относится к другой организации.")
    if pool is not None and pool.organization_id != organization.id:
        raise ValidationError("Объект относится к другой организации.")
    if employee.organization_id != organization.id:
        raise ValidationError("Сотрудник относится к другой организации.")
    if role == EmployeeRewardRule.ROLE_PAPERWORK:
        raise ValidationError("Оформление определяется автором исходного документа 1С.")
    if role == EmployeeRewardRule.ROLE_PROJECT:
        raise ValidationError(
            "Проект / расчёт назначается только на конкретный проект и выбранные позиции."
        )
    try:
        share = Decimal(str(share_percent))
    except Exception as exc:
        raise ValidationError("Некорректная доля шаблона.") from exc
    if share <= 0 or share > HUNDRED:
        raise ValidationError("Доля шаблона должна быть больше 0 и не больше 100%.")
    template = EmployeeRewardTemplate(
        organization=organization,
        client=client,
        pool=pool,
        source_customer_guid=guid,
        role=role,
        employee=employee,
        share_percent=share,
        effective_from=effective_from,
        is_active=True,
        created_by=actor,
    )
    template.full_clean()
    template.save()
    return template


def seed_template_participation(organization, rows, proposed_by=None):
    """Apply active client/object templates as proposals to new sale scopes."""
    inventory = {}
    for row in rows:
        ref = reward_document_ref(row)
        if not ref:
            continue
        key = (row.period_month, ref[0], ref[1])
        item = inventory.setdefault(
            key,
            {
                "period_month": row.period_month,
                "document_type": ref[0],
                "document_guid": ref[1],
                "label": _document_label(row),
                "customer_guid": _guid_text(_source_data(row).get("customer_guid")),
                "rows": [],
            },
        )
        item["rows"].append(row)

    created_count = 0
    for item in inventory.values():
        customer_guid = item["customer_guid"]
        if not customer_guid:
            continue
        templates = (
            EmployeeRewardTemplate.objects.filter(
                organization=organization,
                source_customer_guid=customer_guid,
                is_active=True,
                effective_from__lte=item["period_month"],
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=item["period_month"]))
            .select_related("employee", "client", "pool")
        )
        for template in templates:
            # Project participation always requires explicit positions for this project.
            if template.role in {
                EmployeeRewardRule.ROLE_PROJECT,
                EmployeeRewardRule.ROLE_PAPERWORK,
            }:
                continue
            scope_key = _scope_key(
                item["document_type"],
                item["document_guid"],
                item["rows"],
                [],
                template.role,
            )
            existing = EmployeeRewardAssignment.objects.filter(
                organization=organization,
                period_month=item["period_month"],
                scope_key=scope_key,
                role=template.role,
                employee=template.employee,
            ).exists()
            if existing:
                continue
            assignment = EmployeeRewardAssignment(
                organization=organization,
                period_month=item["period_month"],
                source_document_type=item["document_type"],
                source_document_guid=item["document_guid"],
                source_document_label=item["label"],
                scope_key=scope_key,
                role=template.role,
                employee=template.employee,
                share_percent=template.share_percent,
                status=EmployeeRewardAssignment.STATUS_PROPOSED,
                source_kind=(
                    EmployeeRewardAssignment.SOURCE_OBJECT_TEMPLATE
                    if template.pool_id
                    else EmployeeRewardAssignment.SOURCE_CLIENT_TEMPLATE
                ),
                basis_note="Предложено по шаблону клиента/объекта; требуется подтверждение факта участия.",
                proposed_by=proposed_by,
            )
            assignment.full_clean()
            assignment.save()
            created_count += 1
    return created_count


def confirmation_preview(organization, assignment_ids):
    assignments = list(
        EmployeeRewardAssignment.objects.filter(
            organization=organization,
            id__in=assignment_ids,
            status=EmployeeRewardAssignment.STATUS_PROPOSED,
        )
        .select_related("employee")
        .prefetch_related("lines")
        .order_by("period_month", "source_document_label", "role", "id")
    )
    if len(assignments) != len(set(assignment_ids)):
        raise ValidationError("Часть назначений недоступна или уже не ожидает подтверждения.")
    grouped = defaultdict(list)
    for item in assignments:
        if EmployeeRewardMonthClose.objects.filter(
            organization=organization,
            period_month=item.period_month,
        ).exists():
            raise ValidationError("Закрытый месяц нельзя менять через подтверждение участия.")
        if item.role == EmployeeRewardRule.ROLE_PROJECT and not item.lines.exists():
            raise ValidationError("Проектное участие без выбранных позиций нельзя подтвердить.")
        grouped[(item.period_month, item.role, item.scope_key)].append(item)

    warnings = []
    for (period_month, role, scope_key), selected in grouped.items():
        selected_ids = {item.id for item in selected}
        existing = EmployeeRewardAssignment.objects.filter(
            organization=organization,
            period_month=period_month,
            role=role,
            scope_key=scope_key,
            status=EmployeeRewardAssignment.STATUS_CONFIRMED,
        ).exclude(id__in=selected_ids)
        total = sum((item.share_percent for item in selected), ZERO) + sum(
            (item.share_percent for item in existing), ZERO
        )
        if total > HUNDRED:
            raise ValidationError(
                f"Подтверждение даст {total}% внутри одной роли/объёма, максимум 100%."
            )
        if total < HUNDRED:
            warnings.append(
                f"{selected[0].source_document_label}: после подтверждения распределено {total}%."
            )
    return assignments, warnings


@transaction.atomic
def confirm_assignments(organization, assignment_ids, actor):
    assignments, warnings = confirmation_preview(organization, assignment_ids)
    now = timezone.now()
    for item in assignments:
        before = {"status": item.status}
        item.status = EmployeeRewardAssignment.STATUS_CONFIRMED
        item.confirmed_by = actor
        item.confirmed_at = now
        item.save(update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"])
        EmployeeRewardAssignmentChange.objects.create(
            assignment=item,
            actor=actor,
            action="confirmed",
            before=before,
            after={"status": item.status},
        )
    return assignments, warnings


@transaction.atomic
def map_onec_author(identity, *, employee, technical, actor):
    if technical and employee is not None:
        raise ValidationError("Техническую учётную запись нельзя одновременно сопоставить сотруднику.")
    identity.employee = None if technical else employee
    identity.status = (
        EmployeeOneCUserIdentity.STATUS_TECHNICAL
        if technical
        else EmployeeOneCUserIdentity.STATUS_CONFIRMED
    )
    identity.confirmed_by = actor
    identity.confirmed_at = timezone.now()
    identity.full_clean()
    identity.save()
    if identity.status == EmployeeOneCUserIdentity.STATUS_CONFIRMED:
        rows = list(
            OneCMonthlyProfit.objects.active_for(identity.organization)
            .filter(source_data__author_user_guid=str(identity.onec_user_id))
            .select_related("import_batch")
        )
        # MySQL JSON equality/casing differs across old snapshots; fall back to bounded active rows.
        if not rows:
            rows = list(
                OneCMonthlyProfit.objects.active_for(identity.organization)
                .filter(period_month__gte=timezone.localdate().replace(month=1, day=1))
                .select_related("import_batch")
            )
            rows = [
                row for row in rows
                if _guid_text(_source_data(row).get("author_user_guid"))
                == str(identity.onec_user_id).lower()
            ]
        apply_period_analytics(rows)
        seed_author_paperwork_proposals(identity.organization, rows, proposed_by=actor)
    return identity


@transaction.atomic
def create_scheme_version(organization, effective_from, values, actor):
    current = (
        EmployeeRewardScheme.objects.filter(
            organization=organization,
            name="Тестовая схема №1",
        )
        .prefetch_related("rules")
        .order_by("-version")
        .first()
    )
    if current and effective_from <= current.effective_from:
        raise ValidationError("Новая версия должна начинаться позже текущей версии.")
    version = (current.version + 1) if current else 1
    scheme = EmployeeRewardScheme.objects.create(
        organization=organization,
        name="Тестовая схема №1",
        version=version,
        effective_from=effective_from,
        is_active=True,
        created_by=actor,
    )
    defaults = [
        (EmployeeRewardRule.ROLE_CLIENT_MANAGER, EmployeeRewardRule.UNIT_CLIENT_MANAGER, EmployeeRewardRule.KIND_INFORMATION, None, None),
        (EmployeeRewardRule.ROLE_PAPERWORK, EmployeeRewardRule.UNIT_PAPERWORK_RETAIL, EmployeeRewardRule.KIND_FIXED, Decimal("50.00"), None),
        (EmployeeRewardRule.ROLE_PAPERWORK, EmployeeRewardRule.UNIT_PAPERWORK_PACKAGE, EmployeeRewardRule.KIND_FIXED, Decimal("200.00"), None),
        (EmployeeRewardRule.ROLE_SALE, EmployeeRewardRule.UNIT_SALE, EmployeeRewardRule.KIND_PERCENT, None, Decimal("10.0000")),
        (EmployeeRewardRule.ROLE_PROJECT, EmployeeRewardRule.UNIT_PROJECT, EmployeeRewardRule.KIND_PERCENT, None, Decimal("5.0000")),
        (EmployeeRewardRule.ROLE_WORK, EmployeeRewardRule.UNIT_WORK, EmployeeRewardRule.KIND_PERCENT, None, Decimal("40.0000")),
    ]
    for role, unit, kind, fixed_default, rate_default in defaults:
        key = f"{role}:{unit}"
        payload = values.get(key, {})
        fixed = payload.get("fixed_amount", fixed_default)
        rate = payload.get("rate_percent", rate_default)
        EmployeeRewardRule.objects.create(
            scheme=scheme,
            role=role,
            unit_kind=unit,
            calculation_kind=kind,
            fixed_amount=fixed if kind == EmployeeRewardRule.KIND_FIXED else None,
            rate_percent=rate if kind == EmployeeRewardRule.KIND_PERCENT else None,
        )
    if current and current.effective_to is None:
        current.effective_to = effective_from - timedelta(days=1)
        current.save(update_fields=["effective_to"])
    return scheme


@transaction.atomic
def close_reward_month(organization, period_month, actor):
    existing = EmployeeRewardMonthClose.objects.filter(
        organization=organization,
        period_month=period_month,
    ).first()
    if existing:
        return existing
    scheme = active_scheme(organization, period_month)
    if not scheme:
        raise ValidationError("Нет действующей версии тестовой схемы.")
    data = _open_dashboard_data(organization, period_month)
    snapshot = _snapshot_dashboard(data)
    total = sum((row["total"] for row in data["rows"]), ZERO)
    return EmployeeRewardMonthClose.objects.create(
        organization=organization,
        period_month=period_month,
        scheme=scheme,
        result_data=snapshot,
        total_amount=quantize_money(total),
        closed_by=actor,
    )


@transaction.atomic
def create_adjustment(organization, period_month, employee, amount, reason, actor):
    if employee.organization_id != organization.id:
        raise ValidationError("Сотрудник относится к другой организации.")
    if not EmployeeRewardMonthClose.objects.filter(
        organization=organization,
        period_month=period_month,
    ).exists():
        raise ValidationError("Отдельная поздняя корректировка применяется только к закрытому месяцу.")
    value = quantize_money(amount)
    if value == 0:
        raise ValidationError("Сумма корректировки не может быть нулевой.")
    if not str(reason or "").strip():
        raise ValidationError("Укажите основание корректировки.")
    return EmployeeRewardAdjustment.objects.create(
        organization=organization,
        period_month=period_month,
        employee=employee,
        amount=value,
        reason=str(reason).strip()[:1000],
        created_by=actor,
    )


@transaction.atomic
def confirm_adjustment(adjustment, actor):
    if adjustment.status != EmployeeRewardAdjustment.STATUS_PROPOSED:
        raise ValidationError("Корректировка уже обработана.")
    adjustment.status = EmployeeRewardAdjustment.STATUS_CONFIRMED
    adjustment.confirmed_by = actor
    adjustment.confirmed_at = timezone.now()
    adjustment.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return adjustment
