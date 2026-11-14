"""Saved operational priorities for the existing Avito dashboard. No provider I/O."""
from datetime import timedelta
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from .communication_services import organization_access, conversation_capability
from .avito_autoload_presentation import for_display


def _time(value, now):
    try:
        parsed = value if hasattr(value, "utcoffset") else parse_datetime(value or "")
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and timezone.is_aware(parsed) and parsed <= now + timedelta(minutes=5) else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 10000000 else None


def build(connection, actor, workspace, *, now=None):
    now = now or timezone.now()
    operations = {"active_count": None, "active_scope_label": "Полный список ещё не подтверждён",
        "attention_summary": "", "checked_at": None, "stale": True}
    if (connection is None or not connection.is_active or not connection.channel.is_active
            or connection.channel.kind != "avito"
            or organization_access(actor, connection.channel.organization) is None
            or not conversation_capability(actor, "can_manage_channels", connection.channel.organization)):
        return operations, []
    actions = []
    def add(label, detail, url, tone="warning"):
        actions.append({"label": label, "detail": detail, "url": url, "tone": tone})
    from .communication_models import AvitoStatusMonitor, AvitoListingStatus
    from .avito_status_monitor import _scope_error
    monitor = AvitoStatusMonitor.objects.select_related("recipient", "organization").filter(connection=connection).first()
    if monitor and not _scope_error(monitor, connection) and _time(monitor.last_success_at, now):
        statuses = list(AvitoListingStatus.objects.filter(monitor=monitor, last_seen_at=monitor.last_success_at).values_list("status", flat=True)[:10001])
        if len(statuses) == monitor.last_item_count and all(s in {"active", "old", "removed", "blocked", "rejected"} for s in statuses):
            operations.update(active_count=statuses.count("active"), checked_at=monitor.last_success_at,
                active_scope_label="Полная сохранённая проверка",
                stale=now-monitor.last_success_at > timedelta(hours=5))
            bad = sum(s in {"blocked", "rejected"} for s in statuses)
            if bad:
                add(f"Отклонены или заблокированы: {bad}", "Посмотреть причины и затронутые объявления.", "#avito-listings-details")
    audit_state = workspace.get("audit", {})
    audit = audit_state.get("data", {}) if isinstance(audit_state, dict) else {}
    checked = _time(audit_state.get("checked_at"), now) if isinstance(audit_state, dict) else None
    complete_audit = isinstance(audit, dict) and audit.get("complete") is True and checked is not None
    if complete_audit and operations["active_count"] is None:
        counts = audit.get("counts", [])
        if isinstance(counts, list) and all(isinstance(r, dict) and _count(r.get("count")) is not None for r in counts):
            active = [r["count"] for r in counts if r.get("status") == "active"]
            total = _count(audit.get("total"))
            if len(active) == 1 and total is not None and sum(r["count"] for r in counts) == total:
                operations.update(active_count=active[0], checked_at=checked,
                    active_scope_label="Полный сохранённый аудит", stale=now-checked > timedelta(hours=5))
    advance = workspace.get("advance", {})
    data = advance.get("data", {}) if isinstance(advance, dict) else {}
    kopeks = data.get("kopeks") if isinstance(data, dict) else None
    if type(kopeks) is int and kopeks <= 10000:
        add("Проверить аванс", "Сохранённый аванс исчерпан или не превышает 100 ₽. Актуальность суммы указана отдельно.",
            "#avito-finance-details", "danger" if kopeks <= 0 else "warning")
    report_state = workspace.get("autoload_report", {})
    report = report_state.get("data", {}) if isinstance(report_state, dict) else {}
    report = report if isinstance(report, dict) else {}
    presentation = for_display(connection, report)
    attention = presentation["has_attention"] if presentation["applicable"] else (
        report.get("complete") is True and (report.get("upload_status") == "error" or any(
            isinstance(m, dict) and m.get("type") in {"error", "warning", "alarm"}
            for row in report.get("rows", []) if isinstance(row, dict)
            for m in (row.get("messages") if isinstance(row.get("messages"), list) else []))))
    if attention:
        add("Проверить ошибки автозагрузки", "Открыть конкретные сообщения последнего сохранённого отчёта.",
            "#avito-report-details", "danger")
    if connection.pk == 4 and str(connection.external_id) == "446312402":
        from .avito_feed_models import AvitoServicesFeed
        from .avito_services_feed import entries, bound
        from .avito_placement_continuity import continuity_context
        feed = AvitoServicesFeed.objects.filter(connection=connection).first()
        if feed and bound(feed, connection):
            observed = {(r.get("id"), r.get("avito_id")): r for r in feed.latest_observations
                if isinstance(r, dict)} if isinstance(feed.latest_observations, list) else {}
            rows = [dict(r, observation=observed.get((r["Id"], r["AvitoId"]), {}))
                for r in entries(feed.draft)]
            continuity = continuity_context(connection, feed, rows, now=now) or {}
            near = sum(r["state"] in {"due_soon", "refresh_required"} for r in continuity.get("rows", []))
            if near:
                add(f"Проверить сроки: {near}", "Близкая или прошедшая сохранённая дата требует проверки статуса. Бесплатное восстановление не подтверждено.",
                    reverse("avito_services_feed", args=[connection.pk]))
    if connection.pk == 4 and str(connection.external_id) == "446312402":
        from .avito_crm_coverage import coverage
        crm = coverage(connection, actor, now=now) or {}
        unknown = _count(crm.get("ad_source_unknown_dialogs"))
        if unknown:
            add(f"Проверить источник обращений: {unknown}",
                "В CRM подтвердить объявление-источник, оценку качества и следующий шаг. Диалог сам по себе не подтверждает заказ.",
                reverse("avito_crm_attribution", args=[connection.pk]), "info")
    if connection.pk == 1 and complete_audit:
        stock = audit.get("active_source_audit", {})
        if isinstance(stock, dict) and stock.get("complete") is True:
            mismatch = _count(stock.get("in_stock_nonpositive_rows"))
            if mismatch:
                add(f"Расхождения наличия: {mismatch}", "Объявления «в наличии» с неположительным остатком. Товары под заказ учитываются отдельно.",
                    "#avito-audit-details")
        duplicates = _count(audit.get("duplicate_candidate_count"))
        if duplicates:
            add(f"Возможные дубли: {duplicates}", "Сравнить карточки и привязку к магазину; автоматически ничего не удаляется.",
                "#avito-audit-details")
    # No stale global failure, unknown date or routine service notice becomes an incident.
    operations["attention_summary"] = "Есть вопросы для проверки" if actions else "Подтверждённых задач из сохранённых данных нет"
    return operations, actions[:3]
