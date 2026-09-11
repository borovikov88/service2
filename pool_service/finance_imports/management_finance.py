"""Canonical read-only management-finance aggregations.

The module deliberately does not create snapshots, classifications or mappings.
It is the shared server-side source for the cash-flow detail, owner overview and
the future internal read contract.  Callers that expose it over HTTP must still
perform their own organization and capability guard before passing an
organization here.
"""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import re

from django.db.models import F
from django.utils import timezone

from pool_service.finance_imports.cost_control import (
    get_onec_cost_anomalies,
    summarize_cost_anomalies,
)
from pool_service.finance_imports.profit_dashboard import apply_period_analytics
from pool_service.finance_imports.payroll_accrual_dashboard import (
    accrual_dashboard_data,
)
from pool_service.models import (
    CashFlowArticleMapping,
    CashFlowRow,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
)


ZERO = Decimal("0.00")
MONEY_KEYS = ("receipts", "payments", "net_cash_flow")
CONTRACT_VERSION = "management_finance.v1"
# The mapping screen may show a small, server-side source display preview for
# review.  It is deliberately bounded and never includes the raw source_data
# JSON (which may contain recorder and other technical identifiers).
CASHFLOW_SOURCE_PREVIEW_LIMIT = 20

# A cash-flow source row may retain technical OData labels in its display
# columns.  The mapping UI is useful for a human review only when it does not
# turn those values into a secondary technical-data endpoint.  This is
# deliberately fail-closed: a value with *any* technical signal is withheld in
# full, rather than trying to remove one fragment and accidentally leaving a
# useful part of an identifier or recorder payload behind.
_SOURCE_UUID_RE = re.compile(
    r"(?i)(?:urn:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_SOURCE_HEX_ID_RE = re.compile(r"(?i)\b[0-9a-f]{32}\b")
_SOURCE_ODATA_TYPE_RE = re.compile(
    r"(?i)\b(?:standardodata\.)?(?:"
    r"document|catalog|accumulationregister|informationregister|"
    r"accountingregister|calculationregister|chartofaccounts|"
    r"chartofcharacteristictypes|businessprocess|task|exchangeplan|"
    r"constant|enum)[._]"
)
_SOURCE_TECHNICAL_MARKER_RE = re.compile(
    r"(?i)\b(?:standardodata|recorder(?:_type)?|ref(?:_key)?|"
    r"source_identity|source_reference|line_number|guid|uuid)\b|"
    r"\b(?:id|key)\b\s*[:=]"
)
_SOURCE_URL_RE = re.compile(r"(?i)\bhttps?://[^\s]+")
_SOURCE_JSONISH_RE = re.compile(
    r"[{}\[\]]|(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*:"
)
_SOURCE_HTML_RE = re.compile(
    r"(?is)</?[a-z][^>]*>|<!--|&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);"
)
_SOURCE_REFERENCE_TOKEN_RE = re.compile(
    r"(?i)\b(?:ref|rec|recorder|document|doc)[-_][a-z0-9]+\b|\bd\d{2,}\b"
)
_SOURCE_UNSAFE_DISPLAY_PATTERNS = (
    _SOURCE_UUID_RE,
    _SOURCE_HEX_ID_RE,
    _SOURCE_ODATA_TYPE_RE,
    _SOURCE_TECHNICAL_MARKER_RE,
    _SOURCE_URL_RE,
    _SOURCE_JSONISH_RE,
    _SOURCE_HTML_RE,
    _SOURCE_REFERENCE_TOKEN_RE,
)

# ``liquidity`` is a supported read-model value.  It intentionally is not added
# to the model field choices in this first no-migration stage: changing those
# choices changes Django's migration state.  Existing mappings remain untouched.
FLOW_LIQUIDITY = CashFlowArticleMapping.FLOW_LIQUIDITY
MANAGEMENT_FLOW_TYPES = (
    CashFlowArticleMapping.FLOW_OPERATING,
    CashFlowArticleMapping.FLOW_INVESTING,
    CashFlowArticleMapping.FLOW_FINANCING,
    FLOW_LIQUIDITY,
    CashFlowArticleMapping.FLOW_INTERNAL,
    CashFlowArticleMapping.FLOW_UNCLASSIFIED,
)
EXTERNAL_FLOW_TYPES = (
    CashFlowArticleMapping.FLOW_OPERATING,
    CashFlowArticleMapping.FLOW_INVESTING,
    CashFlowArticleMapping.FLOW_FINANCING,
    FLOW_LIQUIDITY,
    CashFlowArticleMapping.FLOW_UNCLASSIFIED,
)
FLOW_LABELS = {
    CashFlowArticleMapping.FLOW_OPERATING: "Операционный",
    CashFlowArticleMapping.FLOW_INVESTING: "Инвестиционный",
    CashFlowArticleMapping.FLOW_FINANCING: "Финансовый",
    FLOW_LIQUIDITY: "Ликвидностный",
    CashFlowArticleMapping.FLOW_INTERNAL: "Внутренний",
    CashFlowArticleMapping.FLOW_UNCLASSIFIED: "Неклассифицировано",
}

ALLOCATION_EXTERNAL = "external"
ALLOCATION_INTERNAL = "internal"
ALLOCATION_NON_EXTERNAL = "non_external"
ALLOCATION_LABELS = {
    ALLOCATION_EXTERNAL: "Внешний поток",
    ALLOCATION_INTERNAL: "Внутренний оборот",
    ALLOCATION_NON_EXTERNAL: "Не включено во внешний поток",
}
REVIEW_REASON_LABELS = {
    "mapping_missing": "Нет mapping для статьи",
    "classification_not_confirmed": "Классификация не подтверждена",
    "unsupported_flow_type": "Тип потока не поддержан read-моделью",
    "flow_type_unclassified": "Тип потока не классифицирован",
    "internal_flag_overrides_configured_flow": "Внутренний признак заменяет настроенный тип",
    "excluded_from_external_requires_decision": "Исключено из внешнего потока без внутреннего признака",
    "liquidity_mapping_requires_review": "Ликвидностный тип требует явной проверки решения",
    "dividend_flag_requires_confirmed_financing": (
        "Признак дивидендов допустим только для подтверждённого финансового потока"
    ),
}


def _month_start(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.replace(day=1)
    if isinstance(value, str):
        try:
            return date.fromisoformat(
                f"{value}-01" if len(value) == 7 else value
            ).replace(day=1)
        except ValueError as exc:
            raise ValueError("Месяц должен быть в формате YYYY-MM.") from exc
    raise ValueError("Месяц должен быть датой или строкой YYYY-MM.")


def _next_month(month):
    return date(
        month.year + (month.month == 12),
        1 if month.month == 12 else month.month + 1,
        1,
    )


def _month_sequence(first_month, last_month):
    if first_month is None or last_month is None or first_month > last_month:
        return []
    result = []
    current = first_month
    while current <= last_month:
        result.append(current)
        current = _next_month(current)
    return result


def _empty_money():
    return {key: ZERO for key in MONEY_KEYS}


def _add_money(target, receipts, payments):
    receipts = receipts if receipts is not None else ZERO
    payments = payments if payments is not None else ZERO
    target["receipts"] += receipts
    target["payments"] += payments
    # Imported payments are stored as positive numbers.  The management net is
    # consequently derived here instead of trusting a source display column.
    target["net_cash_flow"] += receipts - payments


def _copy_money(value):
    return {key: value[key] for key in MONEY_KEYS}


def _sum_money(values):
    result = _empty_money()
    for value in values:
        for key in MONEY_KEYS:
            result[key] += value[key]
    return result


def _confirmed_cashflow_states(organization, first_month=None, last_month=None):
    states = OneCReportPeriodState.objects.filter(
        organization=organization,
        report_type=OneCImportBatch.TYPE_CASHFLOW,
        active_batch__organization=organization,
        active_batch__import_type=OneCImportBatch.TYPE_CASHFLOW,
        active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
    ).select_related("active_batch")
    if first_month is not None:
        states = states.filter(period_month__gte=first_month)
    if last_month is not None:
        states = states.filter(period_month__lte=last_month)
    return list(states.order_by("period_month"))


def _confirmed_cashflow_queryset(organization, first_month=None, last_month=None):
    """Return only current confirmed cash-flow facts for one organization."""
    rows = CashFlowRow.objects.active_for(
        organization, OneCImportBatch.TYPE_CASHFLOW
    ).filter(
        import_batch__organization=organization,
        import_batch__import_type=OneCImportBatch.TYPE_CASHFLOW,
        import_batch__status=OneCImportBatch.STATUS_CONFIRMED,
    )
    if first_month is not None:
        rows = rows.filter(period_month__gte=first_month)
    if last_month is not None:
        rows = rows.filter(period_month__lte=last_month)
    return rows


def _confirmed_cashflow_rows(organization, first_month=None, last_month=None):
    rows = _confirmed_cashflow_queryset(organization, first_month, last_month)
    return list(rows.values(
        "period_month",
        "article_raw",
        "normalized_article_name",
        "receipts",
        "payments",
    ))


def _safe_cashflow_source_display(value):
    """Return only a clean human display string from a stored source field.

    Stored values are never parsed or partially redacted.  A JSON-ish payload,
    recorder/reference marker, OData type, identifier, URL or HTML fragment
    makes the whole display unsafe, so it is omitted.  That prevents a new
    technical format from leaking its useful remainder into the mapping UI.
    """
    if not isinstance(value, str):
        return ""
    if not value.strip():
        return ""
    if any(pattern.search(value) for pattern in _SOURCE_UNSAFE_DISPLAY_PATTERNS):
        return ""
    return value


def cashflow_article_source_previews(
    organization,
    normalized_article_names,
    first_month=None,
    last_month=None,
    *,
    limit=CASHFLOW_SOURCE_PREVIEW_LIMIT,
):
    """Return a bounded display-only preview of active source movements.

    This is intentionally not a primary-document resolver.  OData cash-flow
    imports currently persist the 1C ``Аналитика`` display string in
    ``document_raw`` and generally leave ``source_reference`` empty.  The
    function exposes only server-redacted display fragments, period and money
    direction; it never returns ``source_data`` or technical identifiers.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("Лимит предпросмотра должен быть целым числом от 1 до 50.")
    keys = tuple(dict.fromkeys(
        value for value in normalized_article_names
        if isinstance(value, str) and value
    ))
    previews = {
        key: {"items": [], "row_count": 0, "truncated": False}
        for key in keys
    }
    if not keys:
        return previews

    rows = _confirmed_cashflow_queryset(
        organization, first_month, last_month
    ).filter(
        normalized_article_name__in=keys
    ).order_by(
        "normalized_article_name", "period_month", "document_raw",
        "source_reference", "source_row_number", "id",
    ).values(
        "normalized_article_name", "period_month", "document_raw",
        "source_reference", "receipts", "payments",
    )
    for row in rows:
        preview = previews[row["normalized_article_name"]]
        preview["row_count"] += 1
        if len(preview["items"]) >= limit:
            preview["truncated"] = True
            continue
        receipts = row["receipts"] if row["receipts"] is not None else ZERO
        payments = row["payments"] if row["payments"] is not None else ZERO
        preview["items"].append({
            "period_month": row["period_month"],
            "document_display": _safe_cashflow_source_display(
                row["document_raw"]
            ),
            "source_reference_display": _safe_cashflow_source_display(
                row["source_reference"]
            ),
            "receipts": receipts,
            "payments": payments,
            "net_cash_flow": receipts - payments,
        })
    return previews


def _mapping_index(organization):
    fields = (
        "id",
        "article_name",
        "normalized_article_name",
        "management_category",
        "flow_type",
        "classification_status",
        "is_internal_turnover",
        "include_in_external_cashflow",
        "is_dividend",
    )
    return {
        item["normalized_article_name"]: item
        for item in CashFlowArticleMapping.objects.filter(
            organization=organization
        ).values(*fields)
    }


def _classification(mapping):
    """Return presentation and allocation semantics without mutating a mapping."""
    unclassified = CashFlowArticleMapping.FLOW_UNCLASSIFIED
    reasons = []
    if mapping is None:
        return {
            "flow_type": unclassified,
            "configured_flow_type": None,
            "category": "Неклассифицировано",
            "classification_status": "missing",
            "allocation": ALLOCATION_EXTERNAL,
            "reasons": ("mapping_missing",),
            "mapping_id": None,
            "is_dividend": False,
        }

    configured_flow_type = mapping["flow_type"]
    is_confirmed = (
        mapping["classification_status"]
        == CashFlowArticleMapping.CLASS_CONFIRMED
    )
    if not is_confirmed:
        reasons.append("classification_not_confirmed")
    if configured_flow_type not in MANAGEMENT_FLOW_TYPES:
        reasons.append("unsupported_flow_type")
    elif configured_flow_type == unclassified:
        reasons.append("flow_type_unclassified")

    # A draft or explicitly unconfirmed mapping is not authoritative even when
    # it already carries flags.  It stays external unclassified until a person
    # confirms it; otherwise a speculative internal flag could hide cash flow.
    if not is_confirmed or configured_flow_type not in MANAGEMENT_FLOW_TYPES:
        flow_type = unclassified
        allocation = ALLOCATION_EXTERNAL
    elif (
        mapping["is_internal_turnover"]
        or configured_flow_type == CashFlowArticleMapping.FLOW_INTERNAL
    ):
        # The effective flow itself becomes internal.  This keeps the drilldown
        # hierarchy truthful and prevents an internal operating/liquidity item
        # from being displayed as an operating/liquidity flow.
        flow_type = CashFlowArticleMapping.FLOW_INTERNAL
        allocation = ALLOCATION_INTERNAL
        if (
            mapping["is_internal_turnover"]
            and configured_flow_type != CashFlowArticleMapping.FLOW_INTERNAL
        ):
            reasons.append("internal_flag_overrides_configured_flow")
    elif not mapping["include_in_external_cashflow"]:
        flow_type = configured_flow_type
        # An explicit non-external mapping is neither silently dropped nor
        # relabelled as internal without the internal-turnover flag.
        allocation = ALLOCATION_NON_EXTERNAL
        reasons.append("excluded_from_external_requires_decision")
    else:
        flow_type = configured_flow_type
        allocation = ALLOCATION_EXTERNAL

    if flow_type == unclassified:
        category = "Неклассифицировано"
    elif flow_type == CashFlowArticleMapping.FLOW_INTERNAL:
        category = (mapping["management_category"] or "").strip() or "Внутренние обороты"
    else:
        category = (mapping["management_category"] or "").strip() or "Без категории"

    if is_confirmed and configured_flow_type == FLOW_LIQUIDITY:
        # This is a registry item, not an automatic reclassification.  It makes
        # liquidity mappings observable while the mapping remains as saved.
        reasons.append("liquidity_mapping_requires_review")
    is_dividend = bool(mapping["is_dividend"])
    if is_dividend and not (
        is_confirmed
        and configured_flow_type == CashFlowArticleMapping.FLOW_FINANCING
        and allocation == ALLOCATION_EXTERNAL
    ):
        # Manual or legacy database edits must never make an unconfirmed or
        # non-financing row look like a dividend in the common read model.
        reasons.append("dividend_flag_requires_confirmed_financing")
        is_dividend = False
    return {
        "flow_type": flow_type,
        "configured_flow_type": configured_flow_type,
        "category": category,
        "classification_status": mapping["classification_status"],
        "allocation": allocation,
        "reasons": tuple(dict.fromkeys(reasons)),
        "mapping_id": mapping["id"],
        "is_dividend": is_dividend,
    }


def _new_month(month, *, has_data):
    return {
        "period_month": month,
        "has_data": has_data,
        "totals": _empty_money(),
        "flow_totals": {flow: _empty_money() for flow in MANAGEMENT_FLOW_TYPES},
        "external_flow_totals": {
            flow: _empty_money() for flow in EXTERNAL_FLOW_TYPES
        },
        "allocation_totals": {
            ALLOCATION_EXTERNAL: _empty_money(),
            ALLOCATION_INTERNAL: _empty_money(),
            ALLOCATION_NON_EXTERNAL: _empty_money(),
        },
        # The monthly drilldown is filled from the same classified rows as the
        # range totals.  It is intentionally an in-memory read model only.
        "article_buckets": {},
    }


def _present_month(item):
    if not item["has_data"]:
        missing_money = {key: None for key in MONEY_KEYS}
        return {
            "period_month": item["period_month"],
            "has_data": False,
            **missing_money,
            "operating": dict(missing_money),
            "external": dict(missing_money),
            "liquidity": dict(missing_money),
            "financing": dict(missing_money),
            "internal": dict(missing_money),
            "non_external": dict(missing_money),
            "unclassified": dict(missing_money),
            "flow_totals": {
                flow: dict(missing_money) for flow in MANAGEMENT_FLOW_TYPES
            },
            "external_flow_totals": {
                flow: dict(missing_money) for flow in EXTERNAL_FLOW_TYPES
            },
            "breakdown": [],
            "articles": [],
        }
    breakdown, articles = _article_breakdown(item["article_buckets"])
    return {
        "period_month": item["period_month"],
        "has_data": True,
        **_copy_money(item["totals"]),
        "operating": _copy_money(
            item["external_flow_totals"][CashFlowArticleMapping.FLOW_OPERATING]
        ),
        "external": _copy_money(item["allocation_totals"][ALLOCATION_EXTERNAL]),
        "liquidity": _copy_money(item["external_flow_totals"][FLOW_LIQUIDITY]),
        "financing": _copy_money(
            item["external_flow_totals"][CashFlowArticleMapping.FLOW_FINANCING]
        ),
        "internal": _copy_money(item["allocation_totals"][ALLOCATION_INTERNAL]),
        "non_external": _copy_money(
            item["allocation_totals"][ALLOCATION_NON_EXTERNAL]
        ),
        "unclassified": _copy_money(
            item["flow_totals"][CashFlowArticleMapping.FLOW_UNCLASSIFIED]
        ),
        "flow_totals": {
            flow: _copy_money(value)
            for flow, value in item["flow_totals"].items()
        },
        "external_flow_totals": {
            flow: _copy_money(value)
            for flow, value in item["external_flow_totals"].items()
        },
        "breakdown": breakdown,
        "articles": articles,
    }


def _presentation_totals(flow_totals, external_flow_totals, allocation_totals):
    return {
        "operating": _copy_money(
            external_flow_totals[CashFlowArticleMapping.FLOW_OPERATING]
        ),
        "investing": _copy_money(
            external_flow_totals[CashFlowArticleMapping.FLOW_INVESTING]
        ),
        "financing": _copy_money(
            external_flow_totals[CashFlowArticleMapping.FLOW_FINANCING]
        ),
        "liquidity": _copy_money(external_flow_totals[FLOW_LIQUIDITY]),
        "external_unclassified": _copy_money(
            external_flow_totals[CashFlowArticleMapping.FLOW_UNCLASSIFIED]
        ),
        "external": _copy_money(allocation_totals[ALLOCATION_EXTERNAL]),
        "internal": _copy_money(allocation_totals[ALLOCATION_INTERNAL]),
        "non_external": _copy_money(
            allocation_totals[ALLOCATION_NON_EXTERNAL]
        ),
        "unclassified": _copy_money(
            flow_totals[CashFlowArticleMapping.FLOW_UNCLASSIFIED]
        ),
        "flow_totals": {
            flow: _copy_money(value) for flow, value in flow_totals.items()
        },
        "external_flow_totals": {
            flow: _copy_money(value)
            for flow, value in external_flow_totals.items()
        },
    }


def _article_breakdown(article_buckets):
    by_flow = defaultdict(lambda: defaultdict(list))
    flattened = []
    for item in article_buckets.values():
        result = {
            "article_raw": item["article_raw"],
            "normalized_article_name": item["normalized_article_name"],
            "category": item["category"],
            "flow_type": item["flow_type"],
            "flow_label": FLOW_LABELS[item["flow_type"]],
            "allocation": item["allocation"],
            "allocation_label": ALLOCATION_LABELS[item["allocation"]],
            "is_external": item["allocation"] == ALLOCATION_EXTERNAL,
            "classification_status": item["classification_status"],
            "configured_flow_type": item["configured_flow_type"],
            "mapping_id": item["mapping_id"],
            "is_dividend": item["is_dividend"],
            "row_count": item["row_count"],
            **_copy_money(item["totals"]),
        }
        by_flow[result["flow_type"]][result["category"]].append(result)
        flattened.append(result)

    result = []
    for flow_type in MANAGEMENT_FLOW_TYPES:
        categories = []
        for category, articles in sorted(by_flow[flow_type].items()):
            articles.sort(key=lambda item: (item["article_raw"], item["normalized_article_name"]))
            categories.append({
                "category": category,
                "totals": _sum_money(articles),
                "articles": articles,
            })
        if categories:
            result.append({
                "flow_type": flow_type,
                "label": FLOW_LABELS[flow_type],
                "totals": _sum_money(category["totals"] for category in categories),
                "categories": categories,
            })
    flattened.sort(key=lambda item: (
        MANAGEMENT_FLOW_TYPES.index(item["flow_type"]),
        item["category"], item["article_raw"],
    ))
    return result, flattened


def _cashflow_grouped(data, group_by):
    """Materialize a requested flat grouping next to the canonical hierarchy."""
    if group_by == "month":
        return [
            {
                "month": item["period_month"],
                "available": item["has_data"],
                "all_cashflow": {
                    key: item[key] for key in MONEY_KEYS
                },
                "operating": item["operating"],
                "external": item["external"],
                "liquidity": item["liquidity"],
                "financing": item["financing"],
                "internal": item["internal"],
                "non_external": item["non_external"],
                "unclassified": item["unclassified"],
            }
            for item in data["monthly"]
        ]
    if group_by == "flow_type":
        return [
            {
                "flow_type": flow,
                "label": FLOW_LABELS[flow],
                "totals": data["flow_totals"][flow],
            }
            for flow in MANAGEMENT_FLOW_TYPES
        ]
    if group_by == "management_category":
        categories = defaultdict(list)
        for article in data["articles"]:
            categories[article["category"]].append(article)
        return [
            {
                "management_category": category,
                "totals": _sum_money(items),
                "article_count": len(items),
            }
            for category, items in sorted(categories.items())
        ]
    return data["articles"]


def _filter_values(value, *, field):
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)) and all(
        isinstance(item, str) for item in value
    ):
        return set(value)
    raise ValueError(f"Фильтр {field} должен быть строкой или списком строк.")


def _cashflow_filters(filters):
    filters = _validated_filters(filters, allowed={
        "article_names", "flow_types", "management_categories", "allocations",
    })
    result = {}
    if "article_names" in filters:
        result["article_names"] = _filter_values(
            filters["article_names"], field="article_names"
        )
    if "flow_types" in filters:
        values = _filter_values(filters["flow_types"], field="flow_types")
        unsupported = values - set(MANAGEMENT_FLOW_TYPES)
        if unsupported:
            raise ValueError("Неизвестные типы потока: " + ", ".join(sorted(unsupported)))
        result["flow_types"] = values
    if "management_categories" in filters:
        result["management_categories"] = _filter_values(
            filters["management_categories"], field="management_categories"
        )
    if "allocations" in filters:
        values = _filter_values(filters["allocations"], field="allocations")
        allowed = {
            ALLOCATION_EXTERNAL, ALLOCATION_INTERNAL, ALLOCATION_NON_EXTERNAL,
        }
        unsupported = values - allowed
        if unsupported:
            raise ValueError("Неизвестные группы потока: " + ", ".join(sorted(unsupported)))
        result["allocations"] = values
    return result


def _matches_cashflow_filters(row, classification, filters):
    return (
        (not filters.get("article_names")
         or row["normalized_article_name"] in filters["article_names"])
        and (not filters.get("flow_types")
             or classification["flow_type"] in filters["flow_types"])
        and (not filters.get("management_categories")
             or classification["category"] in filters["management_categories"])
        and (not filters.get("allocations")
             or classification["allocation"] in filters["allocations"])
    )


def _add_article_bucket(buckets, row, classification, receipts, payments):
    """Add one classified row to a range or monthly canonical article bucket."""
    article_key = (
        classification["flow_type"],
        classification["category"],
        row["normalized_article_name"],
        row["article_raw"],
        classification["allocation"],
    )
    article = buckets.setdefault(article_key, {
        "article_raw": row["article_raw"],
        "normalized_article_name": row["normalized_article_name"],
        "category": classification["category"],
        "flow_type": classification["flow_type"],
        "allocation": classification["allocation"],
        "classification_status": classification["classification_status"],
        "configured_flow_type": classification["configured_flow_type"],
        "mapping_id": classification["mapping_id"],
        "is_dividend": classification["is_dividend"],
        "row_count": 0,
        "totals": _empty_money(),
    })
    article["row_count"] += 1
    _add_money(article["totals"], receipts, payments)


def management_cashflow_data(
    organization, first_month=None, last_month=None, *, filters=None,
):
    """Aggregate active *confirmed* cash-flow rows with mapping semantics.

    ``totals`` remains the raw all-cash-flow compatibility total.  Management
    cards use ``operating`` (external operating only), while ``external`` is
    exactly the sum of operating, investing, financing, liquidity and external
    unclassified values.  ``internal`` and ``non_external`` remain separate so
    no amount is silently lost.
    """
    first_month = _month_start(first_month) if first_month is not None else None
    last_month = _month_start(last_month) if last_month is not None else None
    if first_month and last_month and first_month > last_month:
        raise ValueError("Начальный месяц не может быть позже конечного.")
    filters = _cashflow_filters(filters)

    states = _confirmed_cashflow_states(organization, first_month, last_month)
    state_by_month = {item.period_month: item for item in states}
    if first_month is None and states:
        first_month = states[0].period_month
    if last_month is None and states:
        last_month = states[-1].period_month
    requested_months = (
        _month_sequence(first_month, last_month)
        if first_month is not None and last_month is not None
        else list(state_by_month)
    )
    missing_months = [
        month for month in requested_months if month not in state_by_month
    ]

    months = {
        month: _new_month(month, has_data=month in state_by_month)
        for month in requested_months
    }
    totals = _empty_money()
    flow_totals = {flow: _empty_money() for flow in MANAGEMENT_FLOW_TYPES}
    external_flow_totals = {flow: _empty_money() for flow in EXTERNAL_FLOW_TYPES}
    allocation_totals = {
        ALLOCATION_EXTERNAL: _empty_money(),
        ALLOCATION_INTERNAL: _empty_money(),
        ALLOCATION_NON_EXTERNAL: _empty_money(),
    }
    article_buckets = {}
    review_buckets = {}
    mappings = _mapping_index(organization)

    for row in _confirmed_cashflow_rows(organization, first_month, last_month):
        month = months.setdefault(
            row["period_month"],
            _new_month(row["period_month"], has_data=True),
        )
        mapping = mappings.get(row["normalized_article_name"])
        classification = _classification(mapping)
        if not _matches_cashflow_filters(row, classification, filters):
            continue
        receipts = row["receipts"] if row["receipts"] is not None else ZERO
        payments = row["payments"] if row["payments"] is not None else ZERO
        _add_money(totals, receipts, payments)
        _add_money(month["totals"], receipts, payments)
        _add_money(flow_totals[classification["flow_type"]], receipts, payments)
        _add_money(
            month["flow_totals"][classification["flow_type"]], receipts, payments
        )
        _add_money(
            allocation_totals[classification["allocation"]], receipts, payments
        )
        _add_money(
            month["allocation_totals"][classification["allocation"]],
            receipts, payments,
        )
        if (
            classification["allocation"] == ALLOCATION_EXTERNAL
            and classification["flow_type"] in EXTERNAL_FLOW_TYPES
        ):
            _add_money(
                external_flow_totals[classification["flow_type"]], receipts, payments
            )
            _add_money(
                month["external_flow_totals"][classification["flow_type"]],
                receipts, payments,
            )

        _add_article_bucket(
            article_buckets, row, classification, receipts, payments
        )
        _add_article_bucket(
            month["article_buckets"], row, classification, receipts, payments
        )

        if classification["reasons"]:
            review = review_buckets.setdefault(row["normalized_article_name"], {
                "article_raw": row["article_raw"],
                "normalized_article_name": row["normalized_article_name"],
                "mapping_id": classification["mapping_id"],
                "flow_type": classification["flow_type"],
                "configured_flow_type": classification["configured_flow_type"],
                "classification_status": classification["classification_status"],
                "allocation": classification["allocation"],
                "reasons": set(),
                "totals": _empty_money(),
                "row_count": 0,
            })
            review["reasons"].update(classification["reasons"])
            review["row_count"] += 1
            _add_money(review["totals"], receipts, payments)

    breakdown, articles = _article_breakdown(article_buckets)
    registry = []
    for item in review_buckets.values():
        registry.append({
            **{key: value for key, value in item.items() if key not in {"reasons", "totals"}},
            "allocation_label": ALLOCATION_LABELS[item["allocation"]],
            "reasons": sorted(item["reasons"]),
            "reason_labels": [
                REVIEW_REASON_LABELS[reason]
                for reason in sorted(item["reasons"])
            ],
            **_copy_money(item["totals"]),
        })
    registry.sort(key=lambda item: (item["article_raw"], item["normalized_article_name"]))

    management_totals = _presentation_totals(
        flow_totals, external_flow_totals, allocation_totals
    )
    management_cards = (
        ("operating_receipts", "Операционные поступления", management_totals["operating"]["receipts"]),
        ("operating_payments", "Операционные платежи", management_totals["operating"]["payments"]),
        ("operating_net_cash_flow", "Операционный чистый поток", management_totals["operating"]["net_cash_flow"]),
        ("external_net_cash_flow", "Внешний чистый поток", management_totals["external"]["net_cash_flow"]),
        ("liquidity_net_cash_flow", "Ликвидностный поток", management_totals["liquidity"]["net_cash_flow"]),
        ("financing_net_cash_flow", "Финансовый поток", management_totals["financing"]["net_cash_flow"]),
        ("internal_net_cash_flow", "Внутренний поток", management_totals["internal"]["net_cash_flow"]),
        ("non_external_net_cash_flow", "Не включено во внешний поток", management_totals["non_external"]["net_cash_flow"]),
    )
    return {
        "period_first": first_month,
        "period_last": last_month,
        "totals": _copy_money(totals),
        **management_totals,
        "monthly": [_present_month(months[month]) for month in sorted(months)],
        "articles": articles,
        "breakdown": breakdown,
        "management_cards": [
            {"key": key, "label": label, "value": value}
            for key, label, value in management_cards
        ],
        "mapping_review_registry": registry,
        "unclassified_article_count": sum(
            1 for item in articles
            if item["flow_type"] == CashFlowArticleMapping.FLOW_UNCLASSIFIED
        ),
        "has_rows": bool(totals != _empty_money() or articles),
        "has_active_months": bool(states),
        "active_months": list(state_by_month),
        "missing_months": missing_months,
        "data_through": max(state_by_month, default=None),
        "last_updated": max(
            (state.updated_at for state in states), default=None
        ),
        "active_versions": [
            {
                "period_month": state.period_month,
                "report_type": OneCImportBatch.TYPE_CASHFLOW,
                "batch_id": str(state.active_batch_id),
                "confirmed_at": state.active_batch.confirmed_at,
                "source_type": state.active_batch.source_type,
            }
            for state in states
        ],
        "filters": {
            key: sorted(value) for key, value in filters.items()
        },
    }


def cashflow_article_trend_source(
    organization,
    first_month=None,
    last_month=None,
    *,
    cashflow_data=None,
):
    """Build chart-ready article/month values from canonical cash-flow buckets.

    The article chart has its own selection UI, but never gets a filtered
    aggregation.  Supplying the detail page's ``cashflow_data`` makes the chart
    and all management cards share the same read pass.
    """
    data = cashflow_data or management_cashflow_data(
        organization, first_month, last_month
    )
    labels = {}
    for article in data["articles"]:
        normalized = article["normalized_article_name"]
        label = article["article_raw"]
        if normalized not in labels or label < labels[normalized]:
            labels[normalized] = label

    months = []
    for item in data["monthly"]:
        article_values = {}
        for article in item["articles"]:
            normalized = article["normalized_article_name"]
            article_values[normalized] = (
                article_values.get(normalized, ZERO)
                + article["net_cash_flow"]
            )
        months.append({
            "period_month": item["period_month"],
            "has_data": item["has_data"],
            "net_cash_flow": item["net_cash_flow"],
            "article_net_cash_flow": article_values,
        })
    return {
        "months": months,
        "options": [
            {"normalized_article_name": normalized, "label": label}
            for normalized, label in sorted(
                labels.items(), key=lambda item: (item[1], item[0])
            )
        ],
        "has_active_months": data["has_active_months"],
    }


def _serialize_contract(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize_contract(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_contract(item) for item in value]
    return value


def _validated_filters(filters, *, allowed):
    """Validate a future-contract filter envelope without guessing semantics."""
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError("Фильтры должны быть объектом.")
    unknown = set(filters) - set(allowed)
    if unknown:
        raise ValueError(
            "Неподдерживаемые фильтры: " + ", ".join(sorted(unknown))
        )
    return dict(filters)


def _validated_group_by(group_by, *, allowed, default):
    if group_by is None:
        return tuple(default)
    if isinstance(group_by, str):
        group_by = (group_by,)
    elif isinstance(group_by, (list, tuple)):
        group_by = tuple(group_by)
    else:
        raise ValueError("group_by должен быть строкой или списком полей.")
    if group_by not in allowed:
        rendered = ", ".join("/".join(item) for item in allowed)
        raise ValueError(f"Неподдерживаемая группировка. Допустимо: {rendered}.")
    return group_by


def _preliminary_months(first_month, last_month):
    if first_month is None or last_month is None:
        return []
    current = timezone.localdate().replace(day=1)
    return [current] if first_month <= current <= last_month else []


def _contract_envelope(
    organization,
    *,
    first_month,
    last_month,
    available,
    complete,
    missing_months,
    active_versions,
    warnings=(),
    filters=None,
    group_by=(),
):
    complete = bool(available and complete)
    warning_items = list(warnings)
    if missing_months:
        warning_items.append({
            "code": "missing_months",
            "message": "Для части выбранного периода нет активной подтверждённой версии.",
        })
    if not available:
        warning_items.append({
            "code": "no_confirmed_active_data",
            "message": "Нет активных подтверждённых данных для выбранного источника.",
        })
    return {
        "as_of": timezone.now(),
        "period": {"from": first_month, "to": last_month},
        "organizations": [{"id": organization.pk, "name": organization.name}],
        "consolidation": {
            "scope": "single_organization",
            "complete": complete,
        },
        "metadata": {
            "contract_version": CONTRACT_VERSION,
            "source_scope": "active_confirmed_versions",
        },
        "available": available,
        "complete": complete,
        "missing_months": list(missing_months),
        "preliminary_months": _preliminary_months(first_month, last_month),
        "active_batch_ids": sorted({
            item["batch_id"] for item in active_versions if item.get("batch_id")
        }),
        "active_versions": list(active_versions),
        "warnings": warning_items,
        "filters": filters or {},
        "group_by": (
            [group_by] if isinstance(group_by, str) else list(group_by)
        ),
    }


def _status_for_report(organization, report_types, first_month=None, last_month=None):
    states = OneCReportPeriodState.objects.filter(
        organization=organization,
        report_type__in=report_types,
        active_batch__organization=organization,
        active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
    ).filter(
        active_batch__import_type=F("report_type")
    ).select_related("active_batch")
    if first_month is not None:
        states = states.filter(period_month__gte=first_month)
    if last_month is not None:
        states = states.filter(period_month__lte=last_month)
    states = list(states.order_by("period_month", "report_type"))
    periods = {item.period_month for item in states}
    # An omitted boundary means "the available reporting span", not an empty
    # requested range.  Otherwise Jan+Mar looked complete in a no-date status
    # call because no sequence was checked at all.  Keep explicitly supplied
    # boundaries intact and infer only the missing side from this source.
    resolved_first = first_month
    resolved_last = last_month
    if periods:
        if resolved_first is None:
            resolved_first = min(periods)
        if resolved_last is None:
            resolved_last = max(periods)
    requested = _month_sequence(resolved_first, resolved_last)
    missing_months = [month for month in requested if month not in periods]
    return {
        "available": bool(states),
        "complete": bool(states) and not missing_months,
        "period": {"from": resolved_first, "to": resolved_last},
        "missing_months": missing_months,
        "data_through": max(periods, default=None),
        "last_updated": max((item.updated_at for item in states), default=None),
        "active_versions": [
            {
                "period_month": item.period_month,
                "report_type": item.report_type,
                "batch_id": str(item.active_batch_id),
                "confirmed_at": item.active_batch.confirmed_at,
            }
            for item in states
        ],
    }


def _confirmed_profit_rows(organization, first_month, last_month):
    rows = OneCMonthlyProfit.objects.active_for(organization).filter(
        import_batch__organization=organization,
        import_batch__import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        import_batch__status=OneCImportBatch.STATUS_CONFIRMED,
        period_month__range=(first_month, last_month),
    )
    return list(rows)


def _profit_value(rows):
    revenue = sum((row.dashboard_revenue for row in rows), ZERO)
    incomplete_cost = any(row.dashboard_analytical_cost is None for row in rows)
    incomplete_profit = any(row.dashboard_gross_profit is None for row in rows)
    gross_profit = None if incomplete_profit else sum(
        (row.dashboard_gross_profit for row in rows), ZERO
    )
    cost = None if incomplete_cost else sum(
        (row.dashboard_analytical_cost for row in rows), ZERO
    )
    gross_margin = (
        None if gross_profit is None or revenue == ZERO
        else (gross_profit * Decimal("100") / revenue).quantize(Decimal("0.01"))
    )
    return {
        "revenue": revenue,
        "cost": cost,
        "gross_profit": gross_profit,
        "gross_margin": gross_margin,
        "complete": not incomplete_cost and not incomplete_profit,
    }


PROFIT_FILTER_FIELDS = {
    "manager": "manager_name",
    "client": "customer_name",
    "nomenclature": "nomenclature",
    "document": "document_name",
}


def _profit_filters(filters):
    filters = _validated_filters(filters, allowed=set(PROFIT_FILTER_FIELDS))
    return {
        key: _filter_values(value, field=key)
        for key, value in filters.items()
    }


def _matches_profit_filters(row, filters):
    return all(
        not values or getattr(row, PROFIT_FILTER_FIELDS[key]) in values
        for key, values in filters.items()
    )


def _profit_grouped(rows, group_by):
    field = PROFIT_FILTER_FIELDS[group_by]
    groups = defaultdict(list)
    for row in rows:
        groups[getattr(row, field) or None].append(row)
    return [
        {group_by: value, "totals": _profit_value(group_rows)}
        for value, group_rows in sorted(
            groups.items(), key=lambda item: (item[0] is None, item[0] or "")
        )
    ]


def _profit_data(organization, first_month, last_month, *, filters=None):
    status = _status_for_report(
        organization, (OneCImportBatch.TYPE_MONTHLY_PROFIT,), first_month, last_month
    )
    rows = _confirmed_profit_rows(organization, first_month, last_month)
    filters = _profit_filters(filters)
    if filters:
        rows = [row for row in rows if _matches_profit_filters(row, filters)]
    apply_period_analytics(rows)
    monthly = []
    for month in _month_sequence(first_month, last_month):
        available = month not in status["missing_months"]
        month_rows = [row for row in rows if row.period_month == month]
        values = _profit_value(month_rows) if available else {
            "revenue": None, "cost": None, "gross_profit": None,
            "gross_margin": None, "complete": False,
        }
        monthly.append({"period_month": month, "available": available, **values})
    values = _profit_value(rows) if status["available"] else {
        "revenue": None, "cost": None, "gross_profit": None,
        "gross_margin": None, "complete": False,
    }
    return {
        "status": status,
        "rows": rows,
        "monthly": monthly,
        "totals": values,
        "filters": {key: sorted(value) for key, value in filters.items()},
    }


def get_finance_data_status(
    organization, first_month=None, last_month=None, *, filters=None, group_by=None,
):
    """Return source coverage only; it never starts a refresh or import."""
    first_month = _month_start(first_month) if first_month is not None else None
    last_month = _month_start(last_month) if last_month is not None else None
    if first_month and last_month and first_month > last_month:
        raise ValueError("Начальный месяц не может быть позже конечного.")
    filters = _validated_filters(filters, allowed=set())
    group_by = _validated_group_by(group_by, allowed={()}, default=())
    sources = {
        "profit": _status_for_report(
            organization, (OneCImportBatch.TYPE_MONTHLY_PROFIT,), first_month, last_month
        ),
        "payroll": _status_for_report(
            organization,
            (OneCImportBatch.TYPE_PAYROLL, OneCImportBatch.TYPE_PAYROLL_ACCRUAL),
            first_month, last_month,
        ),
        "cashflow": _status_for_report(
            organization, (OneCImportBatch.TYPE_CASHFLOW,), first_month, last_month
        ),
    }
    source_periods = [
        source["period"] for source in sources.values()
        if source["period"]["from"] is not None
        and source["period"]["to"] is not None
    ]
    resolved_first = first_month or min(
        (period["from"] for period in source_periods), default=None
    )
    resolved_last = last_month or max(
        (period["to"] for period in source_periods), default=None
    )
    cashflow = management_cashflow_data(organization, first_month, last_month)
    anomalies = get_onec_cost_anomalies(organization).filter(
        import_batch__organization=organization,
        import_batch__status=OneCImportBatch.STATUS_CONFIRMED,
    )
    if first_month is not None:
        anomalies = anomalies.filter(period_month__gte=first_month)
    if last_month is not None:
        anomalies = anomalies.filter(period_month__lte=last_month)
    cost_anomalies = summarize_cost_anomalies(anomalies)
    active_versions = [
        version for source in sources.values()
        for version in source["active_versions"]
    ]
    warnings = []
    if cashflow["unclassified_article_count"]:
        warnings.append({
            "code": "unclassified_cashflow_articles",
            "message": "Есть статьи ДДС, не включённые в operating до подтверждения mapping.",
        })
    if cost_anomalies["row_count"]:
        warnings.append({
            "code": "cost_anomalies",
            "message": "Есть активные строки валовой прибыли с неопределённой себестоимостью.",
        })
    result = {
        **_contract_envelope(
            organization,
            first_month=resolved_first,
            last_month=resolved_last,
            available=any(source["available"] for source in sources.values()),
            complete=all(source["complete"] for source in sources.values()),
            missing_months=sorted({
                month for source in sources.values()
                for month in source["missing_months"]
            }),
            active_versions=active_versions,
            warnings=warnings,
            filters=filters,
            group_by=group_by,
        ),
        "sources": sources,
        "cashflow": {
            "unclassified": cashflow["unclassified"],
            "external_unclassified": cashflow["external_unclassified"],
            "mapping_review_count": len(cashflow["mapping_review_registry"]),
        },
        "cost_anomalies": {
            "available": sources["profit"]["available"],
            "complete": sources["profit"]["complete"] and not cost_anomalies["row_count"],
            "summary": cost_anomalies,
        },
    }
    return _serialize_contract(result)


def get_cashflow_breakdown(
    organization,
    first_month=None,
    last_month=None,
    *,
    filters=None,
    group_by=None,
):
    """Return the decimal-string read contract for management cash flow."""
    filters = _cashflow_filters(filters)
    group_by = _validated_group_by(
        group_by,
        allowed={
            ("month",),
            ("flow_type",),
            ("management_category",),
            ("article",),
        },
        default=("article",),
    )[0]
    data = management_cashflow_data(
        organization, first_month, last_month, filters=filters
    )
    # The canonical aggregation infers its active range when callers omit
    # dates.  Reuse that resolved period so the internal contract never
    # describes available data with a misleading null range.
    first = data["period_first"]
    last = data["period_last"]
    warnings = []
    if data["unclassified_article_count"]:
        warnings.append({
            "code": "unclassified_cashflow_articles",
            "message": "Неклассифицированные статьи не входят в операционный поток.",
        })
    if data["non_external"]["net_cash_flow"] != ZERO:
        warnings.append({
            "code": "non_external_cashflow",
            "message": "Есть подтверждённая сумма вне внешнего потока без признака внутреннего оборота.",
        })
    result = {
        **_contract_envelope(
            organization,
            first_month=first,
            last_month=last,
            available=data["has_active_months"],
            complete=not data["missing_months"],
            missing_months=data["missing_months"],
            active_versions=data["active_versions"],
            warnings=warnings,
            filters=data["filters"],
            group_by=group_by,
        ),
        "metadata": {
            "contract_version": CONTRACT_VERSION,
            "source_scope": "active_confirmed_versions",
            "data_through": data["data_through"],
            "last_updated": data["last_updated"],
            "active_versions": data["active_versions"],
        },
        "totals": {
            "all_cashflow": data["totals"],
            "operating": data["operating"],
            "investing": data["investing"],
            "financing": data["financing"],
            "liquidity": data["liquidity"],
            "external_unclassified": data["external_unclassified"],
            "external": data["external"],
            "internal": data["internal"],
            "non_external": data["non_external"],
            "unclassified": data["unclassified"],
        },
        "monthly": data["monthly"],
        # The nested tree is stable for UI callers.  ``grouped`` is the
        # separately materialized requested level for a future thin adapter.
        "breakdown": data["breakdown"],
        "grouped": _cashflow_grouped(data, group_by),
        "grouped_by": group_by,
        "mapping_review_registry": data["mapping_review_registry"],
    }
    return _serialize_contract(result)


def get_profit_breakdown(
    organization,
    first_month,
    last_month=None,
    *,
    filters=None,
    group_by=None,
):
    """Return active confirmed profit totals and honest unavailable dimensions."""
    first_month = _month_start(first_month)
    last_month = _month_start(last_month) if last_month is not None else first_month
    if first_month > last_month:
        raise ValueError("Начальный месяц не может быть позже конечного.")
    filters = _profit_filters(filters)
    group_by = _validated_group_by(
        group_by,
        allowed={
            ("manager",), ("client",), ("nomenclature",), ("document",),
            ("department",), ("object",),
        },
        default=("nomenclature",),
    )[0]
    data = _profit_data(
        organization,
        first_month,
        last_month,
        filters=filters,
    )

    if not data["status"]["available"]:
        dimension_reason = "no_confirmed_active_profit_data"
    elif not data["rows"]:
        dimension_reason = "active_profit_version_has_no_rows"
    else:
        # OneCMonthlyProfit has no department or business-object field; a
        # fabricated zero or inferred split would be materially misleading.
        dimension_reason = "active_profit_rows_have_no_department_or_object_dimension"
    unavailable_dimensions = {
        "available": False,
        "value": None,
        "reason": dimension_reason,
    }
    if group_by in {"department", "object"}:
        breakdown = {
            group_by: {
                "available": False,
                "items": None,
                "reason": dimension_reason,
            },
        }
    else:
        breakdown = {group_by: _profit_grouped(data["rows"], group_by)}
    warnings = []
    if not data["totals"]["complete"]:
        warnings.append({
            "code": "incomplete_profit_cost",
            "message": "Часть активных строк не имеет определённой аналитической себестоимости.",
        })
    result = {
        **_contract_envelope(
            organization,
            first_month=first_month,
            last_month=last_month,
            available=data["status"]["available"],
            complete=data["status"]["complete"] and data["totals"]["complete"],
            missing_months=data["status"]["missing_months"],
            active_versions=data["status"]["active_versions"],
            warnings=warnings,
            filters=data["filters"],
            group_by=group_by,
        ),
        "metadata": {
            "contract_version": CONTRACT_VERSION,
            "source_scope": "active_confirmed_versions",
            "data_through": data["status"]["data_through"],
            "last_updated": data["status"]["last_updated"],
            "active_versions": data["status"]["active_versions"],
        },
        "totals": data["totals"],
        "monthly": data["monthly"],
        "breakdown": breakdown,
        "dimensions": {
            "department": dict(unavailable_dimensions),
            "object": dict(unavailable_dimensions),
        },
    }
    return _serialize_contract(result)


def get_monthly_finance(
    organization,
    month=None,
    *,
    start_month=None,
    end_month=None,
    filters=None,
    group_by=None,
):
    """Return a month or explicit monthly range across confirmed finance sources."""
    if month is None and start_month is None:
        raise ValueError("Укажите month или start_month.")
    shorthand_month = _month_start(month) if month is not None else None
    first_month = _month_start(start_month) if start_month is not None else shorthand_month
    if shorthand_month is not None and first_month != shorthand_month:
        raise ValueError("month и start_month должны указывать один месяц.")
    last_month = _month_start(end_month) if end_month is not None else first_month
    if first_month > last_month:
        raise ValueError("Начальный месяц не может быть позже конечного.")

    filters = _validated_filters(
        filters, allowed={"cashflow", "profit", "profit_nomenclature"}
    )
    cashflow_filters = _cashflow_filters(filters.get("cashflow"))
    profit_filter_input = dict(filters.get("profit") or {})
    if "profit_nomenclature" in filters:
        if "nomenclature" in profit_filter_input:
            raise ValueError("Номенклатура задана одновременно в profit и profit_nomenclature.")
        profit_filter_input["nomenclature"] = filters["profit_nomenclature"]
    profit_filters = _profit_filters(profit_filter_input)
    group_by = _validated_group_by(
        group_by, allowed={(), ("month",)}, default=("month",)
    )
    cashflow = management_cashflow_data(
        organization, first_month, last_month, filters=cashflow_filters
    )
    profit = _profit_data(
        organization, first_month, last_month, filters=profit_filters
    )
    payroll = accrual_dashboard_data(organization, first_month, last_month)
    payroll_status = _status_for_report(
        organization,
        (OneCImportBatch.TYPE_PAYROLL, OneCImportBatch.TYPE_PAYROLL_ACCRUAL),
        first_month,
        last_month,
    )
    cashflow_available = cashflow["has_active_months"]
    payroll_available = payroll_status["available"] and payroll["has_data"]

    def profit_values(values, available):
        if not available:
            return {
                "revenue": None, "cost": None, "gross_profit": None,
                "gross_margin": None,
            }
        return {
            key: values[key]
            for key in ("revenue", "cost", "gross_profit", "gross_margin")
        }

    def cashflow_values(values, available):
        if not available:
            return {
                key: None for key in (
                    "all_cashflow", "operating", "investing", "external",
                    "liquidity", "financing", "internal", "non_external",
                    "unclassified",
                )
            }
        return {
            "all_cashflow": values.get("totals") or {
                key: values[key] for key in MONEY_KEYS
            },
            "operating": values["operating"],
            "investing": values.get("investing") or values["flow_totals"][
                CashFlowArticleMapping.FLOW_INVESTING
            ],
            "external": values["external"],
            "liquidity": values["liquidity"],
            "financing": values["financing"],
            "internal": values["internal"],
            "non_external": values["non_external"],
            "unclassified": values["unclassified"],
        }

    cashflow_totals_source = {
        "totals": cashflow["totals"],
        "operating": cashflow["operating"],
        "investing": cashflow["investing"],
        "external": cashflow["external"],
        "liquidity": cashflow["liquidity"],
        "financing": cashflow["financing"],
        "internal": cashflow["internal"],
        "non_external": cashflow["non_external"],
        "unclassified": cashflow["unclassified"],
    }
    payroll_months = {item["period_month"]: item for item in payroll["months"]}
    profit_months = {item["period_month"]: item for item in profit["monthly"]}
    cashflow_months = {item["period_month"]: item for item in cashflow["monthly"]}
    monthly = []
    for current_month in _month_sequence(first_month, last_month):
        profit_month = profit_months[current_month]
        payroll_month = payroll_months[current_month]
        cashflow_month = cashflow_months[current_month]
        monthly.append({
            "month": current_month,
            "profit": profit_values(profit_month, profit_month["available"]),
            "payroll": {
                "accrued": payroll_month["accrued"] if payroll_month["has_data"] else None,
            },
            "cashflow": cashflow_values(
                cashflow_month, cashflow_month["has_data"]
            ),
        })

    active_versions = [
        *cashflow["active_versions"],
        *profit["status"]["active_versions"],
        *payroll_status["active_versions"],
    ]
    missing_months = sorted({
        *cashflow["missing_months"],
        *profit["status"]["missing_months"],
        *payroll_status["missing_months"],
    })
    warnings = []
    if cashflow["unclassified_article_count"]:
        warnings.append({
            "code": "unclassified_cashflow_articles",
            "message": "Неклассифицированные статьи не входят в операционный поток.",
        })
    if not profit["totals"]["complete"]:
        warnings.append({
            "code": "incomplete_profit_cost",
            "message": "Часть активных строк прибыли не имеет определённой себестоимости.",
        })
    if payroll_status["available"] and not payroll["has_data"]:
        warnings.append({
            "code": "payroll_value_unavailable",
            "message": "Активная версия ФОТ не даёт доступного начисленного значения.",
        })
    source_complete = (
        cashflow_available and not cashflow["missing_months"]
        and profit["status"]["complete"] and profit["totals"]["complete"]
        and payroll_status["complete"] and payroll["has_data"]
    )
    result = {
        **_contract_envelope(
            organization,
            first_month=first_month,
            last_month=last_month,
            available=(
                cashflow_available
                or profit["status"]["available"]
                or payroll_status["available"]
            ),
            complete=source_complete,
            missing_months=missing_months,
            active_versions=active_versions,
            warnings=warnings,
            filters={
                "cashflow": {
                    key: sorted(value) for key, value in cashflow_filters.items()
                },
                "profit": {key: sorted(value) for key, value in profit_filters.items()},
            },
            group_by=group_by,
        ),
        "metadata": {
            "contract_version": CONTRACT_VERSION,
            "source_scope": "active_confirmed_versions",
            "data_through": {
                "cashflow": cashflow["data_through"],
                "profit": profit["status"]["data_through"],
                "payroll": payroll["data_through"],
            },
            "last_updated": {
                "cashflow": cashflow["last_updated"],
                "profit": profit["status"]["last_updated"],
                "payroll": payroll["last_updated"],
            },
        },
        "month": first_month if first_month == last_month else None,
        "totals": {
            "profit": profit_values(
                profit["totals"], profit["status"]["available"]
            ),
            "payroll": {"accrued": payroll["accrued"] if payroll["has_data"] else None},
            "cashflow": cashflow_values(cashflow_totals_source, cashflow_available),
        },
        "monthly": monthly,
        "sources": {
            "profit": {
                "available": profit["status"]["available"],
                "complete": profit["status"]["complete"] and profit["totals"]["complete"],
                "missing_months": profit["status"]["missing_months"],
                "values": profit_values(
                    profit["totals"], profit["status"]["available"]
                ),
            },
            "payroll": {
                "available": payroll_available,
                "complete": payroll_status["complete"] and payroll["has_data"],
                "missing_months": payroll_status["missing_months"],
                "values": {"accrued": payroll["accrued"] if payroll["has_data"] else None},
            },
            "cashflow": {
                "available": cashflow_available,
                "complete": cashflow_available and not cashflow["missing_months"],
                "missing_months": cashflow["missing_months"],
                "values": cashflow_values(cashflow_totals_source, cashflow_available),
            },
        },
    }
    return _serialize_contract(result)


__all__ = [
    "EXTERNAL_FLOW_TYPES",
    "FLOW_LABELS",
    "FLOW_LIQUIDITY",
    "MANAGEMENT_FLOW_TYPES",
    "cashflow_article_trend_source",
    "get_cashflow_breakdown",
    "get_finance_data_status",
    "get_monthly_finance",
    "get_profit_breakdown",
    "management_cashflow_data",
]
