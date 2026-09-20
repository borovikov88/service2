from django.urls import reverse

from .seo import is_indexable_host


BRAND_TAGLINE = "Система управления"


FINANCE_TOPBAR_DETAIL_CRUMBS = {
    "finance_employee_detail": [("Сотрудник", None)],
    "finance_transaction_create": [("Новая операция", None)],
    "finance_income_create": [("Новое поступление", None)],
    "finance_income_edit": [("Поступление", None)],
    "finance_expense_create": [("Новый расход", None)],
    "finance_expense_detail": [("Расход", None)],
    "finance_expense_edit": [("Расход", None)],
    "finance_report": [("Отчёт", None)],
    "finance_cash_dashboard": [("Операции кассы", None)],
    "finance_cash_income_create": [("Новое поступление", None)],
    "finance_cash_transfer_create": [("Перемещение", None)],
    "finance_cash_accountable_issue_create": [("Выдача под отчёт", None)],
    "finance_accountable_return_create": [("Возврат подотчёта", None)],
    "finance_cash_operation_detail": [("Операция", None)],
    "finance_cash_operation_edit": [("Операция", None)],
    "finance_card_transfer_create": [("Новое перечисление", None)],
    "finance_card_transfer_detail": [("Перечисление", None)],
    "finance_onec_cashflow_mapping": [("Классификация статей", None)],
    "finance_onec_import_list": [("История загрузок", None)],
    "finance_onec_import_detail": [("Загрузка", None)],
    "finance_onec_import_preview": [("Предпросмотр", None)],
    "finance_onec_cashflow_detail": [("Загрузка ДДС", None)],
    "finance_onec_cashflow_preview": [("Предпросмотр ДДС", None)],
    "finance_payroll_import_list": [("Загрузки ФОТ", None)],
    "finance_payroll_import_upload": [("Новая загрузка", None)],
    "finance_payroll_import_preview": [("Предпросмотр", None)],
    "finance_payroll_employee_mapping": [("Сопоставление сотрудников", None)],
    "finance_payroll_employee_list": [("Сотрудники", None)],
    "finance_payroll_employee_profile": [
        ("Сотрудники", "finance_payroll_employee_list"),
        ("Карточка сотрудника", None),
    ],
}


def _active_finance_item(navigation):
    for group in navigation or []:
        for item in group.get("items", []):
            if item.get("active"):
                return item
    return None


def _finance_topbar_breadcrumbs(
    management_navigation,
    operations_navigation,
    current_route,
):
    management_item = _active_finance_item(management_navigation)
    operations_item = _active_finance_item(operations_navigation)

    if current_route == "finance_operations" or operations_item:
        area = "operations"
        root_label = "Операции"
        root_route = "finance_operations"
        active_item = operations_item
    else:
        area = "management"
        root_label = "Управленческие финансы"
        root_route = "finance_dashboard"
        active_item = management_item

    root = {
        "label": root_label,
        "url": "" if current_route == root_route else reverse(root_route),
    }
    breadcrumbs = [root]
    if current_route == root_route:
        return breadcrumbs

    if not active_item:
        return breadcrumbs

    if current_route == active_item["route_name"]:
        breadcrumbs.append({"label": active_item["label"], "url": ""})
        return breadcrumbs

    breadcrumbs.append({
        "label": active_item["label"],
        "url": active_item.get("url") or "",
    })
    details = FINANCE_TOPBAR_DETAIL_CRUMBS.get(current_route)
    if details:
        for label, route_name in details:
            breadcrumbs.append({
                "label": label,
                "url": reverse(route_name) if route_name else "",
            })
    return breadcrumbs


def brand_context(request):
    host = request.get_host().split(":", 1)[0].lower()

    default_brand = {
        "name": "RovikPool",
        "logo": "assets/images/rovikpool-favicon.png",
        "favicon": "assets/images/rovikpool-favicon.png",
        "icon_192": "assets/images/rovikpool-app-192.png",
        "icon_512": "assets/images/rovikpool-app-512.png",
        "hide_text_mobile": False,
        "logo_wide": False,
    }
    brands_by_host = {
        "rovikpool.ru": default_brand,
        "www.rovikpool.ru": default_brand,
        "service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine-favicon.png",
            "favicon": "assets/images/aqualine-favicon.png",
            "icon_192": "assets/images/aqualine-app-192.png",
            "icon_512": "assets/images/aqualine-app-512.png",
            "hide_text_mobile": False,
            "logo_wide": False,
        },
        "www.service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine-favicon.png",
            "favicon": "assets/images/aqualine-favicon.png",
            "icon_192": "assets/images/aqualine-app-192.png",
            "icon_512": "assets/images/aqualine-app-512.png",
            "hide_text_mobile": False,
            "logo_wide": False,
        },
    }

    brand = brands_by_host.get(host, default_brand)
    return {
        "brand_name": brand["name"],
        "brand_tagline": BRAND_TAGLINE,
        "brand_logo": brand["logo"],
        "brand_favicon": brand["favicon"],
        "brand_icon_192": brand.get("icon_192", default_brand["icon_192"]),
        "brand_icon_512": brand.get("icon_512", default_brand["icon_512"]),
        "brand_hide_text_on_mobile": brand.get("hide_text_mobile", False),
        "brand_logo_wide": brand.get("logo_wide", False),
        "allow_indexing": is_indexable_host(host),
    }


def plan_status_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    from django.utils import timezone
    from pool_service.models import OrganizationAccess, WebAuthnCredential
    from pool_service.security import (
        has_fresh_password_login,
        idle_timeout_seconds,
        passkey_prompt_dismissed,
    )
    from pool_service.services.permissions import (
        company_has_access,
        company_trial_days_left,
        is_personal_user,
        is_personal_free,
        is_org_access_blocked,
        organization_accesses_for_user,
        organization_for_user,
        personal_pool,
        trial_ends_at,
    )
    from pool_service.services.finance import automatic_lock_is_disabled

    accesses = organization_accesses_for_user(user)
    personal_user = is_personal_user(user)
    org_roles = [access.role for access in accesses]
    operational_roles = {"owner", "admin", "manager", "service", "installer"}
    finance_only_user = (
        not user.is_superuser
        and "accountant" in org_roles
        and not bool(operational_roles & set(org_roles))
    )
    is_org_admin = "admin" in org_roles or "owner" in org_roles or user.is_superuser
    can_access_crm = is_org_admin or "service" in org_roles or "installer" in org_roles or "manager" in org_roles or user.is_superuser
    crm_service_only = (
        bool({"service", "installer"} & set(org_roles))
        and not is_org_admin
        and "manager" not in org_roles
        and not user.is_superuser
    )
    can_access_finance = bool({"owner", "admin", "manager", "service", "installer", "accountant"} & set(org_roles))
    can_manage_company_cash = user.is_superuser or bool({"owner", "admin", "accountant"} & set(org_roles))
    can_access_kkm_cash = can_manage_company_cash or "manager" in org_roles
    can_access_users = user.is_superuser or is_org_admin or "service" in org_roles
    is_org_staff = bool(operational_roles & set(org_roles))
    personal_free = is_personal_free(user)
    security_pin_enabled = bool(getattr(getattr(user, "profile", None), "security_pin_hash", ""))
    security_passkey_enabled = getattr(user, "_has_passkey_cache", None)
    if security_passkey_enabled is None:
        security_passkey_enabled = WebAuthnCredential.objects.filter(user=user).exists()
        user._has_passkey_cache = security_passkey_enabled
    security_show_quick_setup_prompt = (
        has_fresh_password_login(request)
        and (not security_pin_enabled or not security_passkey_enabled)
        and not passkey_prompt_dismissed(request)
    )
    context = {
        "is_personal_user": personal_user,
        "is_personal_free": personal_free,
        "is_org_admin": is_org_admin,
        "is_org_staff": is_org_staff,
        "can_access_crm": can_access_crm,
        "can_access_finance": can_access_finance,
        "can_manage_company_cash": can_manage_company_cash,
        "can_access_kkm_cash": can_access_kkm_cash,
        "can_access_users": can_access_users,
        "can_access_development": False,
        "crm_service_only": crm_service_only,
        "finance_only_user": finance_only_user,
        "payment_url": reverse("billing"),
        "access_blocked": False,
        "personal_pool_url": None,
        "home_url": reverse("pool_list"),
        "show_plan_badge": False,
        "security_pin_enabled": security_pin_enabled,
        "security_passkey_enabled": security_passkey_enabled,
        "security_quick_unlock_enabled": security_pin_enabled or security_passkey_enabled,
        "security_automatic_lock_enabled": not automatic_lock_is_disabled(user),
        "security_show_quick_setup_prompt": security_show_quick_setup_prompt,
        "security_idle_timeout_seconds": idle_timeout_seconds(),
    }

    if personal_free:
        context["plan_badge"] = {"type": "personal_free"}
        context["show_plan_badge"] = True
    if personal_user:
        pool = personal_pool(user)
        if pool:
            context["personal_pool_url"] = reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})
        else:
            context["personal_pool_url"] = reverse("pool_create")
        context["home_url"] = context["personal_pool_url"]

    org = organization_for_user(user)
    if not org:
        return context

    from pool_service.services.finance import (
        can_access_finance_operations,
        can_access_finance_section,
        can_access_management_finance,
        can_import_payroll,
        can_manage_employee_mapping,
        can_view_payroll_summary,
        finance_navigation,
        finance_operations_navigation,
        management_finance_navigation,
    )
    current_route = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    management_finance_access = can_access_management_finance(user, org)
    finance_operations_access = can_access_finance_operations(user, org)
    context["can_access_finance"] = can_access_finance_section(user, org)
    context["can_access_management_finance"] = management_finance_access
    context["can_access_finance_operations"] = finance_operations_access
    context["management_finance_navigation"] = management_finance_navigation(
        user, org, current_route=current_route
    )
    context["finance_operations_navigation"] = finance_operations_navigation(
        user, org, current_route=current_route
    )
    context["finance_navigation"] = finance_navigation(
        user, org, current_route=current_route
    )
    context["active_finance_area"] = (
        "operations"
        if current_route == "finance_operations"
        or _active_finance_item(context["finance_operations_navigation"])
        else "management"
        if current_route.startswith("finance_")
        else ""
    )
    if current_route.startswith("finance_"):
        context["topbar_breadcrumbs"] = _finance_topbar_breadcrumbs(
            context["management_finance_navigation"],
            context["finance_operations_navigation"],
            current_route,
        )
    payroll_summary_access = can_view_payroll_summary(user, org)
    payroll_import_access = can_import_payroll(user, org)
    payroll_mapping_access = can_manage_employee_mapping(user, org)
    context["can_view_payroll_summary"] = payroll_summary_access
    context["can_access_payroll"] = any((
        payroll_summary_access,
        payroll_import_access,
        payroll_mapping_access,
    ))
    if payroll_summary_access:
        context["payroll_entry_url"] = reverse("finance_payroll_dashboard")
    elif payroll_import_access:
        context["payroll_entry_url"] = reverse("finance_payroll_import_list")
    elif payroll_mapping_access:
        context["payroll_entry_url"] = reverse("finance_payroll_employee_mapping")

    context["can_access_development"] = user.is_superuser or bool(
        {"owner", "admin"} & set(org_roles)
    )

    if "service" in org_roles:
        context["home_url"] = reverse("readings_all")
    elif "manager" in org_roles:
        context["home_url"] = reverse("finance_kkm_cash_dashboard")
    elif management_finance_access:
        context["home_url"] = reverse("finance_dashboard")
    elif finance_operations_access:
        context["home_url"] = reverse("finance_operations")

    now = timezone.now()
    context["access_blocked"] = is_org_access_blocked(user, now=now)

    if org.paid_until and org.paid_until >= now:
        context["plan_badge"] = {"type": "company_paid", "paid_until": org.paid_until}
        context["show_plan_badge"] = (org.paid_until - now).days < 30
        return context

    trial_end = trial_ends_at(org)
    if trial_end and trial_end > now:
        context["plan_badge"] = {
            "type": "company_trial",
            "days_left": company_trial_days_left(org, now=now),
        }
        context["show_plan_badge"] = True
        return context

    if trial_end and trial_end <= now:
        context["plan_badge"] = {"type": "company_expired", "days_left": 0}
        context["show_plan_badge"] = True
        return context

    if not company_has_access(org, now=now):
        context["plan_badge"] = {"type": "company_expired", "days_left": 0}
        context["show_plan_badge"] = True
        return context

    return context


def notifications_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    from pool_service.models import Notification

    unread_count = Notification.objects.filter(
        user=user,
        is_read=False,
        is_resolved=False,
    ).count()
    return {"notifications_unread_count": unread_count}


def push_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    from django.conf import settings
    public_key = getattr(settings, "VAPID_PUBLIC_KEY", "")
    private_key = getattr(settings, "VAPID_PRIVATE_KEY", "")
    enabled = bool(user.is_active and public_key and private_key)
    return {
        "push_enabled": enabled,
        "push_public_key": public_key or "",
    }
