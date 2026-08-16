from .seo import is_indexable_host


BRAND_TAGLINE = "Система управления"


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
    from django.urls import reverse
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
        organization_for_user,
        personal_pool,
        trial_ends_at,
    )

    personal_user = is_personal_user(user)
    org_roles = list(OrganizationAccess.objects.filter(user=user).values_list("role", flat=True))
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
    security_passkey_enabled = WebAuthnCredential.objects.filter(user=user).exists()
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
        can_access_finance_section,
        can_import_payroll,
        can_manage_employee_mapping,
        can_view_payroll_summary,
        finance_navigation,
    )
    current_route = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    context["can_access_finance"] = can_access_finance_section(user, org)
    context["finance_navigation"] = finance_navigation(
        user, org, current_route=current_route
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

    context["can_access_development"] = user.is_superuser or OrganizationAccess.objects.filter(
        user=user,
        organization=org,
        role__in={"owner", "admin"},
    ).exists()

    if "service" in org_roles:
        context["home_url"] = reverse("readings_all")
    elif "manager" in org_roles:
        context["home_url"] = reverse("finance_kkm_cash_dashboard")
    elif can_access_finance:
        context["home_url"] = reverse("finance_dashboard")

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
