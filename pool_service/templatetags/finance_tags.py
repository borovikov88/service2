from django import template

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
