from django import template
from django.template.defaultfilters import date as date_filter
from django.utils.html import format_html

from pool_service.services.finance import format_money, user_display_name


register = template.Library()


@register.filter
def finance_employee_name(user):
    if not user:
        return "—"
    return user_display_name(user)


@register.filter
def money(value):
    return format_money(value)


@register.filter
def cash_denominations(value):
    if not isinstance(value, dict):
        return "—"
    labels = [
        ("bill_5000", "5000 ₽"),
        ("bill_2000", "2000 ₽"),
        ("bill_1000", "1000 ₽"),
        ("bill_500", "500 ₽"),
        ("bill_200", "200 ₽"),
        ("bill_100", "100 ₽"),
        ("bill_50", "50 ₽"),
        ("bill_10", "10 ₽"),
        ("coin_5", "5 ₽"),
        ("coin_2", "2 ₽"),
        ("coin_1", "1 ₽"),
    ]
    parts = []
    for key, label in labels:
        count = value.get(key)
        if count:
            parts.append(f"{label} × {count}")
    manual = value.get("manual_amount")
    if manual and str(manual) not in {"0", "0.00"}:
        parts.append(f"Дополнительно {format_money(manual)}")
    return ", ".join(parts) or "—"


@register.simple_tag
def finance_status_icon(record, pending_label="На проверке"):
    is_voided = bool(getattr(record, "is_voided", False))
    pending_action = getattr(record, "pending_action", "")
    status = getattr(record, "status", "")
    reviewed_by = getattr(record, "reviewed_by", None) or getattr(record, "decided_by", None)
    reviewed_at = getattr(record, "reviewed_at", None) or getattr(record, "decided_at", None)

    if is_voided:
        label, color, icon = "Аннулировано", "text-secondary", "bi-slash-circle"
    elif pending_action == "edit":
        label, color, icon = "Изменение на проверке", "text-warning", "bi-pencil-square"
    elif pending_action == "delete":
        label, color, icon = "Удаление на проверке", "text-warning", "bi-trash"
    elif status == "approved":
        label, color, icon = "Подтверждено", "text-success", "bi-check-circle"
    elif status == "rejected":
        label, color, icon = "Отклонено", "text-danger", "bi-x-circle"
    else:
        label, color, icon = pending_label, "text-warning", "bi-hourglass-split"

    tooltip = label
    if reviewed_by and reviewed_at and status in {"approved", "rejected"}:
        tooltip = f"{label}: {user_display_name(reviewed_by)} · {date_filter(reviewed_at, 'd.m.Y H:i')}"

    return format_html(
        '<button type="button" class="expense-inline-icon expense-status-icon finance-status-icon {}" '
        'data-bs-toggle="tooltip" data-bs-trigger="manual" data-bs-title="{}" '
        'aria-label="{}"><i class="bi {}"></i></button>',
        color,
        tooltip,
        label,
        icon,
    )
