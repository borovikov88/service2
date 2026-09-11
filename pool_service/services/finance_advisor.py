"""Shared read-only adapter for a scoped financial adviser.

This module intentionally contains no OneC access, no mapping logic and no
financial source formulas.  It invokes the canonical per-organization
``management_finance`` contract, then performs only the unavoidable
cross-organization response shaping/consolidation.  The owner UI continues to
use the same canonical calculations, and a future HTTP or MCP transport must
use these helpers instead of rebuilding numbers in a view.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json

from django.utils import timezone

from pool_service.finance_imports.management_finance import (
    CONTRACT_VERSION,
    get_cashflow_breakdown as canonical_cashflow_breakdown,
    get_finance_data_status as canonical_finance_data_status,
    get_monthly_finance as canonical_monthly_finance,
    get_profit_breakdown as canonical_profit_breakdown,
)
from pool_service.models import Organization


MAX_MONTHS = 24
MAX_ORGANIZATIONS = 50
MAX_DETAIL_PAGE_SIZE = 100
MAX_DETAIL_PAGE = 10_000
MONEY_KEYS = ("receipts", "payments", "net_cash_flow")
SOURCE = {
    "contract_version": CONTRACT_VERSION,
    "source_scope": "active_confirmed_versions",
    "calculation": "pool_service.finance_imports.management_finance",
}


class FinanceAdvisorScopeDenied(PermissionError):
    """The request asked for an organization outside the bearer-token scope."""


class FinanceAdvisorValidationError(ValueError):
    """The finite read-only adviser contract was used with invalid parameters."""


def _month(value: str) -> date:
    if not isinstance(value, str) or len(value) != 7:
        raise FinanceAdvisorValidationError("Месяц должен иметь формат YYYY-MM.")
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise FinanceAdvisorValidationError("Месяц должен иметь формат YYYY-MM.") from exc
    return parsed


def validate_month_range(start_month: str, end_month: str) -> tuple[date, date]:
    """Validate the shared maximum request range before any finance query."""
    first = _month(start_month)
    last = _month(end_month)
    if first > last:
        raise FinanceAdvisorValidationError("Начальный месяц не может быть позже конечного.")
    months = (last.year - first.year) * 12 + last.month - first.month + 1
    if months > MAX_MONTHS:
        raise FinanceAdvisorValidationError(
            f"Период финансового MCP ограничен {MAX_MONTHS} месяцами."
        )
    return first, last


def _date_string(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _normalize_organization_ids(organization_ids):
    if organization_ids is None:
        return None
    if not isinstance(organization_ids, list) or not organization_ids:
        raise FinanceAdvisorValidationError(
            "organization_ids должен быть непустым списком целых идентификаторов."
        )
    if len(organization_ids) > MAX_ORGANIZATIONS:
        raise FinanceAdvisorValidationError(
            f"В одном запросе допустимо не более {MAX_ORGANIZATIONS} организаций."
        )
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in organization_ids):
        raise FinanceAdvisorValidationError(
            "organization_ids должен содержать только положительные целые идентификаторы."
        )
    result = list(dict.fromkeys(organization_ids))
    return result


def organizations_for_principal(principal, organization_ids=None) -> list[Organization]:
    """Resolve a full allowed scope; one foreign organization denies all data."""
    requested = _normalize_organization_ids(organization_ids)
    allowed_ids = set(
        principal.organization_scopes.values_list("organization_id", flat=True)
    )
    if requested is None:
        if len(allowed_ids) > MAX_ORGANIZATIONS:
            # ``list_finance_organizations`` has no pagination in v1.  Do not
            # let an accidentally broad principal turn an omitted filter into
            # an unbounded database fan-out or response; split/re-provision
            # the identity instead.
            raise FinanceAdvisorValidationError(
                f"Scope финансовой идентичности ограничен {MAX_ORGANIZATIONS} организациями."
            )
        selected_ids = sorted(allowed_ids)
    else:
        requested_ids = set(requested)
        if not requested_ids.issubset(allowed_ids):
            # Deliberately do not reveal whether an individual foreign ID exists.
            raise FinanceAdvisorScopeDenied(
                "Запрошена организация вне разрешённого финансового scope."
            )
        selected_ids = requested
    if not selected_ids:
        raise FinanceAdvisorScopeDenied("У идентичности нет разрешённых организаций.")
    organizations = list(Organization.objects.filter(pk__in=selected_ids).order_by("name", "id"))
    found_ids = {organization.id for organization in organizations}
    if set(selected_ids) != found_ids:
        raise FinanceAdvisorScopeDenied("Запрошена организация вне разрешённого финансового scope.")
    return organizations


def _organization_descriptor(organization: Organization) -> dict:
    return {"id": organization.pk, "name": organization.name}


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceAdvisorValidationError("Канонический финансовый сервис вернул некорректную сумму.") from exc


def _money_string(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _sum_decimal_strings(values) -> str | None:
    decimals = [_decimal(value) for value in values]
    if any(value is None for value in decimals):
        return None
    return _money_string(sum(decimals, Decimal("0")))


def _sum_money_maps(values) -> dict | None:
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return {
        key: _sum_decimal_strings(value.get(key) for value in values)
        for key in MONEY_KEYS
    }


def _merge_warnings(results) -> list[dict]:
    seen = set()
    merged = []
    for result in results:
        for warning in result.get("warnings", []):
            fingerprint = json.dumps(warning, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint not in seen:
                seen.add(fingerprint)
                merged.append(warning)
    return merged


def _union_months(results, key: str) -> list[str]:
    values = {
        month for result in results for month in result.get(key, []) if month is not None
    }
    return sorted(values)


def _active_versions(results, organizations) -> tuple[list[str], list[dict]]:
    entries = []
    batch_ids = set()
    for organization, result in zip(organizations, results):
        for version in result.get("active_versions", []):
            value = dict(version)
            value["organization"] = _organization_descriptor(organization)
            entries.append(value)
            if value.get("batch_id"):
                batch_ids.add(str(value["batch_id"]))
    entries.sort(
        key=lambda item: (
            item.get("period_month") or "",
            item.get("report_type") or "",
            item["organization"]["id"],
            item.get("batch_id") or "",
        )
    )
    return sorted(batch_ids), entries


def _base_envelope(organizations, results, *, period: dict) -> dict:
    active_batch_ids, active_versions = _active_versions(results, organizations)
    return {
        "as_of": timezone.now().isoformat(),
        "period": period,
        "organizations": [_organization_descriptor(organization) for organization in organizations],
        "available": any(result.get("available", False) for result in results),
        "complete": bool(results) and all(result.get("complete", False) for result in results),
        "warnings": _merge_warnings(results),
        "missing_months": _union_months(results, "missing_months"),
        "preliminary_months": _union_months(results, "preliminary_months"),
        "active_batch_ids": active_batch_ids,
        "active_versions": active_versions,
        "source": dict(SOURCE),
    }


def _profit_metrics(profit: dict | None) -> dict:
    if not profit:
        return {
            "revenue": None,
            "source_cost": None,
            "analytical_cost": None,
            "gross_profit": None,
            "gross_margin": None,
        }
    return {
        "revenue": profit.get("revenue"),
        "source_cost": profit.get("source_cost"),
        "analytical_cost": profit.get("analytical_cost", profit.get("cost")),
        "gross_profit": profit.get("gross_profit"),
        "gross_margin": profit.get("gross_margin"),
    }


def _cashflow_metrics(cashflow: dict | None) -> dict:
    cashflow = cashflow or {}

    def value(name, key="net_cash_flow"):
        group = cashflow.get(name)
        return group.get(key) if isinstance(group, dict) else None

    return {
        "operating_receipts": value("operating", "receipts"),
        "operating_payments": value("operating", "payments"),
        "operating_net_cash_flow": value("operating"),
        "investing_net_cash_flow": value("investing"),
        "financing_net_cash_flow": value("financing"),
        "liquidity_net_cash_flow": value("liquidity"),
        "internal_net_cash_flow": value("internal"),
        "external_unclassified_net_cash_flow": value("external_unclassified"),
        "external_net_cash_flow": value("external"),
    }


def _combine_profit_metrics(metrics) -> dict:
    metrics = list(metrics)
    revenue = _sum_decimal_strings(item["revenue"] for item in metrics)
    gross_profit = _sum_decimal_strings(item["gross_profit"] for item in metrics)
    margin = None
    revenue_decimal = _decimal(revenue)
    gross_decimal = _decimal(gross_profit)
    if revenue_decimal not in (None, Decimal("0")) and gross_decimal is not None:
        margin = _money_string(
            (gross_decimal * Decimal("100") / revenue_decimal).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    return {
        "revenue": revenue,
        "source_cost": _sum_decimal_strings(item["source_cost"] for item in metrics),
        "analytical_cost": _sum_decimal_strings(item["analytical_cost"] for item in metrics),
        "gross_profit": gross_profit,
        "gross_margin": margin,
    }


def _combine_cashflow_metrics(metrics) -> dict:
    return {
        key: _sum_decimal_strings(item[key] for item in metrics)
        for key in (
            "operating_receipts",
            "operating_payments",
            "operating_net_cash_flow",
            "investing_net_cash_flow",
            "financing_net_cash_flow",
            "liquidity_net_cash_flow",
            "internal_net_cash_flow",
            "external_unclassified_net_cash_flow",
            "external_net_cash_flow",
        )
    }


def _month_record(item: dict) -> dict:
    return {
        "month": item["month"],
        "available": bool(
            any(value is not None for value in _profit_metrics(item.get("profit")).values())
            or item.get("payroll", {}).get("accrued") is not None
            or any(value is not None for value in _cashflow_metrics(item.get("cashflow")).values())
        ),
        "profit": _profit_metrics(item.get("profit")),
        "payroll_accrued": item.get("payroll", {}).get("accrued"),
        "cashflow": _cashflow_metrics(item.get("cashflow")),
    }


def _combine_month_records(records_by_organization: list[list[dict]]) -> list[dict]:
    by_month = defaultdict(list)
    for records in records_by_organization:
        for record in records:
            by_month[record["month"]].append(record)
    result = []
    for month in sorted(by_month):
        records = by_month[month]
        profit = _combine_profit_metrics(record["profit"] for record in records)
        cashflow = _combine_cashflow_metrics(record["cashflow"] for record in records)
        payroll = _sum_decimal_strings(record["payroll_accrued"] for record in records)
        result.append({
            "month": month,
            "available": all(record["available"] for record in records),
            "profit": profit,
            "payroll_accrued": payroll,
            "cashflow": cashflow,
        })
    return result


def get_finance_data_status(principal, organization_ids=None) -> dict:
    """Status over the exact scoped organizations, with no import side effects."""
    organizations = organizations_for_principal(principal, organization_ids)
    results = [canonical_finance_data_status(organization) for organization in organizations]
    envelope = _base_envelope(organizations, results, period={"from": None, "to": None})
    envelope.update({
        "organization_results": [
            {
                "organization": _organization_descriptor(organization),
                "sources": result["sources"],
                "cashflow": result["cashflow"],
                "cost_anomalies": result["cost_anomalies"],
                "complete": result["complete"],
                "warnings": result["warnings"],
            }
            for organization, result in zip(organizations, results)
        ],
        "unclassified_totals": {
            "all": _sum_money_maps(
                result["cashflow"].get("unclassified") for result in results
            ),
            "external": _sum_money_maps(
                result["cashflow"].get("external_unclassified") for result in results
            ),
        },
    })
    return envelope


def get_monthly_finance(principal, start_month: str, end_month: str, organization_ids=None) -> dict:
    """Return exact canonical monthly metrics and only a cross-org consolidation."""
    first, last = validate_month_range(start_month, end_month)
    organizations = organizations_for_principal(principal, organization_ids)
    results = [
        canonical_monthly_finance(
            organization,
            start_month=first,
            end_month=last,
            group_by="month",
        )
        for organization in organizations
    ]
    envelope = _base_envelope(
        organizations,
        results,
        period={"from": _date_string(first), "to": _date_string(last)},
    )
    # Keep per-organization canonical results separate from the consolidation;
    # this avoids hiding an incomplete source behind another organization's data.
    per_organization = []
    all_month_records = []
    for organization, result in zip(organizations, results):
        monthly = [_month_record(item) for item in result["monthly"]]
        all_month_records.append(monthly)
        totals = {
            "profit": _profit_metrics(result["totals"].get("profit")),
            "payroll_accrued": result["totals"].get("payroll", {}).get("accrued"),
            "cashflow": _cashflow_metrics(result["totals"].get("cashflow")),
        }
        per_organization.append({
            "organization": _organization_descriptor(organization),
            "complete": result["complete"],
            "warnings": result["warnings"],
            "missing_months": result["missing_months"],
            "preliminary_months": result["preliminary_months"],
            "active_batch_ids": result["active_batch_ids"],
            "totals": totals,
            "monthly": monthly,
        })
    consolidated_monthly = _combine_month_records(all_month_records)
    envelope.update({
        "organization_results": per_organization,
        "consolidated": {
            "complete": envelope["complete"],
            "monthly": consolidated_monthly,
            "totals": {
                "profit": _combine_profit_metrics(
                    item["totals"]["profit"] for item in per_organization
                ),
                "payroll_accrued": _sum_decimal_strings(
                    item["totals"]["payroll_accrued"] for item in per_organization
                ),
                "cashflow": _combine_cashflow_metrics(
                    item["totals"]["cashflow"] for item in per_organization
                ),
            },
        },
    })
    return envelope


def _validate_page(page, page_size) -> tuple[int, int]:
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_DETAIL_PAGE:
        raise FinanceAdvisorValidationError("page должен быть целым числом в допустимом диапазоне.")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_DETAIL_PAGE_SIZE:
        raise FinanceAdvisorValidationError(
            f"page_size должен быть целым числом от 1 до {MAX_DETAIL_PAGE_SIZE}."
        )
    return page, page_size


def _paginate(items, page, page_size) -> dict:
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "next_page": page + 1 if end < total else None,
    }


def _cashflow_item(item: dict, group_by: str) -> dict:
    if group_by == "month":
        amounts = item["all_cashflow"]
        return {
            "month": item["month"],
            "available": item["available"],
            **amounts,
        }
    if group_by == "flow_type":
        return {"flow_type": item["flow_type"], **item["totals"]}
    if group_by == "management_category":
        return {
            "management_category": item["management_category"],
            "article_count": item["article_count"],
            **item["totals"],
        }
    return {
        "article": item["article_raw"],
        "normalized_article_name": item["normalized_article_name"],
        "flow_type": item["flow_type"],
        "management_category": item["category"],
        "allocation": item["allocation"],
        "classification_status": item["classification_status"],
        "row_count": item["row_count"],
        **{key: item[key] for key in MONEY_KEYS},
    }


def _cashflow_item_key(item: dict, group_by: str):
    if group_by == "month":
        return (item["month"],)
    if group_by == "flow_type":
        return (item["flow_type"],)
    if group_by == "management_category":
        return (item["management_category"],)
    return (
        item["normalized_article_name"],
        item["flow_type"],
        item["management_category"],
        item["allocation"],
        item["classification_status"],
    )


def _combine_cashflow_items(items, group_by: str) -> list[dict]:
    buckets = {}
    for item in items:
        key = _cashflow_item_key(item, group_by)
        if key not in buckets:
            buckets[key] = dict(item)
            if "row_count" in item:
                buckets[key]["row_count"] = int(item["row_count"])
            if "article_count" in item:
                buckets[key]["article_count"] = int(item["article_count"])
            continue
        target = buckets[key]
        for money_key in MONEY_KEYS:
            target[money_key] = _sum_decimal_strings(
                (target[money_key], item[money_key])
            )
        for count_key in ("row_count", "article_count"):
            if count_key in item:
                target[count_key] += int(item[count_key])
        if group_by == "month":
            target["available"] = bool(target["available"] and item["available"])
    return [buckets[key] for key in sorted(buckets)]


def get_cashflow_breakdown(
    principal,
    start_month: str,
    end_month: str,
    *,
    organization_ids=None,
    group_by="article",
    flow_types=None,
    management_categories=None,
    page=1,
    page_size=50,
) -> dict:
    """Expose a bounded grouping generated by the canonical cash-flow service."""
    if group_by not in {"month", "flow_type", "management_category", "article"}:
        raise FinanceAdvisorValidationError("Недопустимая группировка ДДС.")
    first, last = validate_month_range(start_month, end_month)
    page, page_size = _validate_page(page, page_size)
    if flow_types is not None and (
        not isinstance(flow_types, list)
        or not flow_types
        or len(flow_types) > 6
        or any(not isinstance(value, str) or len(value) > 64 for value in flow_types)
    ):
        raise FinanceAdvisorValidationError(
            "flow_types должен быть непустым списком не более чем из 6 строк."
        )
    if management_categories is not None and (
        not isinstance(management_categories, list)
        or not management_categories
        or len(management_categories) > 50
        or any(not isinstance(value, str) or len(value) > 300 for value in management_categories)
    ):
        raise FinanceAdvisorValidationError(
            "management_categories должен быть непустым списком не более чем из 50 строк."
        )
    filters = {}
    if flow_types is not None:
        filters["flow_types"] = list(dict.fromkeys(flow_types))
    if management_categories is not None:
        filters["management_categories"] = list(dict.fromkeys(management_categories))
    organizations = organizations_for_principal(principal, organization_ids)
    results = [
        canonical_cashflow_breakdown(
            organization,
            first,
            last,
            filters=filters,
            group_by=group_by,
        )
        for organization in organizations
    ]
    envelope = _base_envelope(
        organizations,
        results,
        period={"from": _date_string(first), "to": _date_string(last)},
    )
    records = []
    per_organization = []
    for organization, result in zip(organizations, results):
        items = [_cashflow_item(item, group_by) for item in result["grouped"]]
        records.extend(items)
        per_organization.append({
            "organization": _organization_descriptor(organization),
            "complete": result["complete"],
            "warnings": result["warnings"],
            "totals": result["totals"],
            **_paginate(items, page, page_size),
        })
    consolidated = _combine_cashflow_items(records, group_by)
    envelope.update({
        "group_by": group_by,
        "filters": filters,
        "organization_results": per_organization,
        "consolidated": {
            "complete": envelope["complete"],
            **_paginate(consolidated, page, page_size),
        },
    })
    return envelope


def _profit_item(item: dict, group_by: str) -> dict:
    total = _profit_metrics(item["totals"])
    key = "customer" if group_by == "client" else group_by
    return {key: item.get(group_by), **total}


def _profit_item_key(item: dict, group_by: str):
    key = "customer" if group_by == "client" else group_by
    return (item.get(key),)


def _combine_profit_items(items, group_by: str) -> list[dict]:
    buckets = defaultdict(list)
    for item in items:
        buckets[_profit_item_key(item, group_by)].append(item)
    combined = []
    for key in sorted(buckets, key=lambda value: (value[0] is None, value[0] or "")):
        rows = buckets[key]
        label_key = "customer" if group_by == "client" else group_by
        combined.append({label_key: rows[0].get(label_key), **_combine_profit_metrics(rows)})
    return combined


def get_profit_breakdown(
    principal,
    start_month: str,
    end_month: str,
    *,
    organization_ids=None,
    group_by="nomenclature",
    page=1,
    page_size=50,
) -> dict:
    """Return a canonical profit dimension, or an explicit unavailable split."""
    aliases = {"customer": "client"}
    internal_group_by = aliases.get(group_by, group_by)
    if internal_group_by not in {
        "month", "manager", "client", "nomenclature", "document", "department", "object"
    }:
        raise FinanceAdvisorValidationError("Недопустимая группировка валовой прибыли.")
    first, last = validate_month_range(start_month, end_month)
    page, page_size = _validate_page(page, page_size)
    organizations = organizations_for_principal(principal, organization_ids)
    # ``month`` is already a canonical, read-only member of every profit
    # contract.  Forward it instead of inventing a second aggregation.
    canonical_group_by = "nomenclature" if internal_group_by == "month" else internal_group_by
    results = [
        canonical_profit_breakdown(
            organization, first, last, group_by=canonical_group_by
        )
        for organization in organizations
    ]
    envelope = _base_envelope(
        organizations,
        results,
        period={"from": _date_string(first), "to": _date_string(last)},
    )
    if internal_group_by in {"department", "object"}:
        unavailable = [
            {
                "organization": _organization_descriptor(organization),
                **result["breakdown"][internal_group_by],
            }
            for organization, result in zip(organizations, results)
        ]
        envelope.update({
            "group_by": group_by,
            "available": False,
            "complete": False,
            "unavailable": {
                "dimension": group_by,
                "reason": "active_profit_rows_have_no_department_or_object_dimension",
                "organization_results": unavailable,
            },
        })
        return envelope

    per_organization = []
    records = []
    if internal_group_by == "month":
        for organization, result in zip(organizations, results):
            items = [
                {
                    "month": month["period_month"],
                    "available": month["available"],
                    **_profit_metrics(month),
                }
                for month in result["monthly"]
            ]
            per_organization.append({
                "organization": _organization_descriptor(organization),
                "complete": result["complete"],
                "warnings": result["warnings"],
                **_paginate(items, page, page_size),
            })
            records.extend(items)
        grouped = defaultdict(list)
        for item in records:
            grouped[item["month"]].append(item)
        consolidated = []
        for month in sorted(grouped):
            records_for_month = grouped[month]
            consolidated.append({
                "month": month,
                "available": all(item["available"] for item in records_for_month),
                **_combine_profit_metrics(records_for_month),
            })
    else:
        for organization, result in zip(organizations, results):
            items = [
                _profit_item(item, internal_group_by)
                for item in result["breakdown"][internal_group_by]
            ]
            per_organization.append({
                "organization": _organization_descriptor(organization),
                "complete": result["complete"],
                "warnings": result["warnings"],
                **_paginate(items, page, page_size),
            })
            records.extend(items)
        consolidated = _combine_profit_items(records, internal_group_by)
    envelope.update({
        "group_by": group_by,
        "organization_results": per_organization,
        "consolidated": {
            "complete": envelope["complete"],
            **_paginate(consolidated, page, page_size),
        },
    })
    return envelope


__all__ = [
    "FinanceAdvisorScopeDenied",
    "FinanceAdvisorValidationError",
    "MAX_DETAIL_PAGE_SIZE",
    "MAX_MONTHS",
    "get_cashflow_breakdown",
    "get_finance_data_status",
    "get_monthly_finance",
    "get_profit_breakdown",
    "organizations_for_principal",
    "validate_month_range",
]
