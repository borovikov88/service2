"""Owner summary over saved, scoped Avito facts; no provider requests or writes."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.db.models import F
from django.utils import timezone

from . import avito_daily_statistics as daily, avito_period_statistics as periods
from . import avito_workspace as workspace_api
from .avito_statistics_models import AvitoDailyStatistic, AvitoStatisticsRun
from .communication_models import ChannelConnection
from .communication_services import organization_access, conversation_capability

CORE = ("views", "contacts", "spending")
CARDS = (
    ("views", "Просмотры", ""),
    ("contacts", "Контакты Авито", ""),
    ("spending", "Денежные расходы", "₽"),
    ("cost_view", "₽ / просмотр", "₽"),
    ("cost_contact", "₽ / контакт", "₽"),
)
MAX_DATES, MAX_ROWS = 30, 24000


def _day(value):
    try:
        return date.fromisoformat(value) if type(value) is str and len(value) == 10 else None
    except ValueError:
        return None


def _number(value, *, counter=False):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("1000000000000000"):
            return None
        if counter and result != result.to_integral():
            return None
        return result
    except (InvalidOperation, ValueError):
        return None


def _text(value):
    return format(value, "f") if value is not None else None


def _values(values):
    result = {key: _number(values.get(key), counter=key != "spending") for key in CORE}
    for key, denominator in (("cost_view", "views"), ("cost_contact", "contacts")):
        top, bottom = result["spending"], result[denominator]
        result[key] = ((top / bottom).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                       if top is not None and bottom is not None and bottom > 0 else None)
    return result


def _cards(values, known=None, expected=None, previous=None):
    return [{"key": key, "label": label, "value": _text(values.get(key)), "unit": unit,
             "reason": "" if values.get(key) is not None else
                 "Нет сопоставимых исходных данных или положительного знаменателя.",
             "known": (known or {}).get(key), "expected": expected,
             "delta": _text(values[key] - previous[key])
                 if previous and values.get(key) is not None and previous.get(key) is not None else None}
            for key, label, unit in CARDS]


def _window(period, first, last, now):
    cutoff = daily.collection_cutoff(now)
    key = period if period in {"7", "30", "custom"} else "30"
    start, end, error = cutoff - timedelta(days=int(key) - 1) if key != "custom" else None, cutoff, ""
    if key == "custom":
        start, end = _day(first), _day(last)
        if start is None or end is None or start > end or (end-start).days >= MAX_DATES or end > cutoff:
            start, end, key = cutoff-timedelta(days=29), cutoff, "30"
            error = "Выберите до 30 дат, не позднее доступной даты источника."
    return {"key": key, "from": start.isoformat(), "to": end.isoformat(),
            "label": f"{start:%d.%m.%Y}–{end:%d.%m.%Y}", "error": error}


def _history(connection, start, end, now=None):
    """Use the existing daily tables with current credential-generation provenance."""
    stamp = daily._stamp(connection)
    scoped = dict(connection=connection, account_id=str(connection.external_id),
                  organization_id_at_collection=connection.channel.organization_id)
    runs = AvitoStatisticsRun.objects.filter(**scoped, day__range=(start, end),
        binding_digest=stamp, source="avito_stats_v2_items", state="success",
        finished_at__lte=now or timezone.now(), pagination_complete=True).order_by("-pk")
    selected = {}
    for run in runs[:MAX_DATES * 10]:
        selected.setdefault(run.day, run)
    rows = list(AvitoDailyStatistic.objects.filter(**scoped,
        day__range=(start, end), source_run_id__in=[run.pk for run in selected.values()],
        source_run__connection=connection, source_run__account_id=str(connection.external_id),
        source_run__organization_id_at_collection=connection.channel.organization_id,
        source_run__binding_digest=stamp, source_run__day=F("day"))
        .select_related("source_run").order_by("day", "avito_id")[:MAX_ROWS])
    return rows, selected


def _aggregate(rows, runs, start, end, identifier=None):
    days = [start+timedelta(days=index) for index in range((end-start).days+1)]
    expected, selected, cohorts = set(), [], []
    for day in days:
        run = runs.get(day)
        if run is None:
            continue
        entries = [row for row in rows if row.day == day and row.source_run_id == run.pk]
        identifiers = daily._expected_ids(run) | {row.avito_id for row in entries}
        cohorts.append(identifiers)
        if identifier is not None:
            identifiers = {identifier}
        expected.update((day, item) for item in identifiers)
        selected.extend(row for row in entries if identifier is None or row.avito_id == identifier)
    stored = {(row.day, row.avito_id) for row in selected}
    reported = {(row.day, row.avito_id) for row in selected if row.provider_row_present}
    pages_complete = all(day in runs for day in days)
    rows_complete = bool(expected) and pages_complete and stored == expected and reported == expected
    known, totals = {}, {}
    for key in CORE:
        values = [_number(row.metrics.get(key), counter=key != "spending")
                  if row.provider_row_present and type(row.metrics) is dict else None for row in selected]
        known[key] = sum(value is not None for value in values)
        totals[key] = sum(values) if rows_complete and values and all(value is not None for value in values) else None
    values = _values(totals)
    known["cost_view"] = sum(row.provider_row_present and type(row.metrics) is dict
        and _number(row.metrics.get("views"), counter=True) is not None
        and _number(row.metrics.get("spending")) is not None for row in selected)
    known["cost_contact"] = sum(row.provider_row_present and type(row.metrics) is dict
        and _number(row.metrics.get("contacts"), counter=True) is not None
        and _number(row.metrics.get("spending")) is not None for row in selected)
    received = max((runs[day].finished_at for day in days
        if day in runs and runs[day].finished_at is not None), default=None)
    ids = sorted({item for _, item in expected})
    return {"values": values, "known": known, "expected": len(expected),
        "reported": len(reported), "pages_complete": pages_complete,
        "rows_complete": rows_complete, "completed_dates": sum(day in runs for day in days),
        "total_dates": len(days), "ids": ids, "received_at": received,
        "cohort_stable": pages_complete and bool(cohorts) and all(group == cohorts[0] for group in cohorts)}


def _chart(rows, runs, first, last):
    result = {"labels": [], **{key: [] for key, _, _ in CARDS}}
    for index in range((last-first).days+1):
        day = first+timedelta(days=index)
        summary = _aggregate(rows, runs, day, day)
        result["labels"].append(day.isoformat())
        for key, _, _ in CARDS:
            value = summary["values"][key]
            result[key].append(float(value) if value is not None else None)
    return result


def _workspace(connection):
    settings = connection.settings if type(connection.settings) is dict else {}
    saved = settings.get("avito_workspace")
    if type(saved) is not dict or saved.get("account_id") != str(connection.external_id):
        return {}
    sections = saved.get("sections")
    return sections if type(sections) is dict else {}


def _manual_snapshot(connection):
    """One complete saved page only; never declare a first page to be the account."""
    section = _workspace(connection).get("statistics")
    if type(section) is not dict or type(section.get("data")) is not dict:
        return None
    data = section["data"]
    rows, grouping, total = data.get("rows"), data.get("grouping"), data.get("total")
    start, end = _day(data.get("date_from")), _day(data.get("date_to"))
    if (type(rows) is not list or len(rows) > workspace_api.PAGE_SIZE or
            grouping not in {"item", "totals"} or type(total) is not int or total != len(rows)
            or type(data.get("offset")) is not int or data["offset"] != 0 or
            start is None or end is None or start > end or (grouping == "totals" and len(rows) > 1)):
        return None
    entries, identifiers = [], set()
    for row in rows:
        if type(row) is not dict or type(row.get("metrics")) is not list:
            return None
        identifier = workspace_api._identifier(row.get("id"))
        if not identifier or identifier in identifiers:
            return None
        identifiers.add(identifier)
        metrics, seen = {}, set()
        for metric in row["metrics"]:
            if type(metric) is not dict or type(metric.get("slug")) is not str or metric["slug"] in seen:
                return None
            seen.add(metric["slug"])
            key = metric["slug"]
            if key in CORE:
                metrics[key] = (_number(metric.get("value"), counter=key != "spending")
                    if metric.get("unit") == ("₽" if key == "spending" else "") else None)
        entries.append({"id": identifier, "values": _values(metrics)})
    values = _values({key: sum(entry["values"][key] for entry in entries)
        if entries and all(entry["values"][key] is not None for entry in entries) else None for key in CORE})
    return {"from": start.isoformat(), "to": end.isoformat(), "values": values,
        "known": {key: sum(entry["values"].get(key) is not None for entry in entries) for key, _, _ in CARDS},
        "expected": len(entries), "received_at": periods._time(section.get("success_at")),
        "pages_complete": True, "ids": sorted(identifiers) if grouping == "item" else [],
        "grouping": grouping, "entries": entries, "source": "saved_workspace_statistics"}


def _fallback(connection, actor, *, first=None, last=None, now=None):
    if connection.pk == 4 and str(connection.external_id) == daily.ACCOUNT:
        result = periods.display(connection, actor)
        reports = [report for report in result.get("reports", [])
            if report.get("received_at_display") is not None
            and report["received_at_display"] <= (now or timezone.now())]
        exact = next((report for report in reports
            if report["date_from"] == (first.isoformat() if first else "")
            and report["date_to"] == (last.isoformat() if last else "")), None)
        weekly = next((report for report in reports
            if (_day(report["date_to"]) - _day(report["date_from"])).days == 6
            and all(_number(report["metric_values"].get(key), counter=key != "spending")
                    is not None for key in CORE)), None)
        if reports:
            report = exact or weekly or reports[0]
            return {"from": report["date_from"], "to": report["date_to"],
                "values": _values(report["metric_values"]), "expected": report["group_count"],
                "known": {row["slug"]: row["known_groups"] for row in report["metrics"]},
                "received_at": report["received_at_display"], "pages_complete": True,
                "ids": report["ids"], "grouping": "item", "source": periods.SOURCE,
                "entries": [{"id": ad["avito_id"], "values": _values(ad["metric_values"])} for ad in report["ads"]]}
    snapshot = _manual_snapshot(connection)
    return snapshot if snapshot is None or snapshot["received_at"] is None or snapshot["received_at"] <= (now or timezone.now()) else None


def display(connection, actor, *, period="30", date_from="", date_to="", now=None):
    """Return only display-safe facts after fresh organization and account checks."""
    authenticated = getattr(actor, "is_authenticated", False)
    actor = get_user_model().objects.filter(pk=getattr(actor, "pk", None), is_active=True).first()
    current = ChannelConnection.objects.select_related("channel__organization").filter(
        pk=getattr(connection, "pk", None)).first()
    if (current is None or actor is None or not authenticated or not getattr(actor, "is_active", False) or
            not getattr(actor, "is_authenticated", False) or not current.is_active or
            not current.channel.is_active or current.channel.kind != "avito" or
            organization_access(actor, current.channel.organization) is None or
            not conversation_capability(actor, "can_manage_channels", current.channel.organization)):
        return None
    binding = daily._stamp(current)
    now = now or timezone.now()
    period_info = _window(period, date_from, date_to, now)
    first, last = _day(period_info["from"]), _day(period_info["to"])
    length = (last-first).days+1
    previous_from, previous_to = first-timedelta(days=length), first-timedelta(days=1)
    rows, runs = _history(current, previous_from, last, now)
    summary = _aggregate(rows, runs, first, last)
    previous = _aggregate(rows, runs, previous_from, previous_to)
    same_cohort = (summary["cohort_stable"] and previous["cohort_stable"]
                   and summary["ids"] == previous["ids"])
    kpis = _cards(summary["values"], summary["known"], summary["expected"],
                  previous["values"] if same_cohort else None)
    source = {"kind": "daily", "label": "Сохранённая суточная статистика Авито",
        "from": first.isoformat(), "to": last.isoformat(), "received_at": summary["received_at"],
        "provider_timezone": "unknown", "pages_complete": summary["pages_complete"],
        "completed_dates": summary["completed_dates"], "total_dates": summary["total_dates"],
        "reported_ads": summary["reported"], "expected_ads": summary["expected"],
        "coverage_label": f"Даты: {summary['completed_dates']}/{summary['total_dates']}; строки: {summary['reported']}/{summary['expected']}"}
    fallback = _fallback(current, actor, first=first, last=last, now=now)
    fallback_output = None
    if fallback is not None:
        fallback_output = {"from": fallback["from"], "to": fallback["to"],
            "received_at": fallback["received_at"], "label": "Отдельный сохранённый отчёт за период",
            "source": fallback["source"], "pages_complete": fallback["pages_complete"],
            "coverage_label": f"Группы: {fallback['expected']}; все страницы получены",
            "kpis": _cards(fallback["values"], fallback["known"], fallback["expected"])}
        # Exact-period snapshot is an independent valid KPI source, not a daily repair.
        if fallback["from"] == first.isoformat() and fallback["to"] == last.isoformat() and not summary["rows_complete"]:
            kpis = fallback_output["kpis"]
            source.update(kind="period", label="Сохранённый отчёт за выбранный период",
                received_at=fallback["received_at"], pages_complete=True,
                reported_ads=fallback["expected"] if fallback["grouping"] == "item" else None,
                expected_ads=fallback["expected"] if fallback["grouping"] == "item" else None,
                coverage_label=fallback_output["coverage_label"])
            fallback_output = None
    ads = []
    titles = {row.avito_id: workspace_api._text(row.title, 300) for row in rows}
    for identifier in summary["ids"][:200]:
        item = _aggregate(rows, runs, first, last, identifier)
        ads.append({"id": identifier, "title": titles.get(identifier, ""),
            **{key: _text(value) for key, value in item["values"].items()},
            "coverage_label": f"{item['reported']}/{item['total_dates']} дат"})
    final = ChannelConnection.objects.select_related("channel__organization").filter(pk=current.pk).first()
    if (final is None or daily._stamp(final) != binding or not final.is_active or not final.channel.is_active
            or organization_access(actor, final.channel.organization) is None
            or not conversation_capability(actor, "can_manage_channels", final.channel.organization)):
        return None
    return {"kind": "services" if current.pk == 4 and str(current.external_id) == daily.ACCOUNT else "goods",
        "title": "Услуги" if current.pk == 4 and str(current.external_id) == daily.ACCOUNT else "Товары",
        "connection_id": current.pk, "account_id": str(current.external_id),
        "date_from": first.isoformat(), "date_to": last.isoformat(), "period": period_info,
        "kpis": kpis, "chart": _chart(rows, runs, first, last), "source": source,
        "fallback": fallback_output, "ads": ads,
        "available": any(card["value"] is not None for card in kpis),
        "limitations": ["Контакты Авито не равны уникальным клиентам, заявкам и заказам.",
            "Часовой пояс дат источника не подтверждён; суточные агрегаты не сдвигаются.",
            "Пропуски не заменяются нулями. Периодный отчёт не создаёт суточные точки.",
            "Денежные расходы, остатки, комиссии и бонусы не складываются как независимые суммы."]}
