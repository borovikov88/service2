from .seo import is_indexable_host


def brand_context(request):
    host = request.get_host().split(":", 1)[0].lower()

    default_brand = {
        "name": "RovikPool",
        "logo": "pool_service/img/logo.png",
        "favicon": "assets/images/favicon.png",
        "icon_192": "assets/images/rovikpool-192.png",
        "icon_512": "assets/images/rovikpool-512.png",
        "hide_text_mobile": False,
        "logo_wide": False,
    }
    brands_by_host = {
        "rovikpool.ru": default_brand,
        "www.rovikpool.ru": default_brand,
        "service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine.png",
            "favicon": "assets/images/aqualine.png",
            "icon_192": "assets/images/aqualine-192.png",
            "icon_512": "assets/images/aqualine-512.png",
            "hide_text_mobile": True,
            "logo_wide": True,
        },
        "www.service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine.png",
            "favicon": "assets/images/aqualine.png",
            "icon_192": "assets/images/aqualine-192.png",
            "icon_512": "assets/images/aqualine-512.png",
            "hide_text_mobile": True,
            "logo_wide": True,
        },
    }

    brand = brands_by_host.get(host, default_brand)
    return {
        "brand_name": brand["name"],
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
    from pool_service.models import OrganizationAccess
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
    is_org_admin = "admin" in org_roles or "owner" in org_roles or user.is_superuser
    can_access_crm = is_org_admin or "service" in org_roles or user.is_superuser
    is_org_staff = bool(org_roles)
    personal_free = is_personal_free(user)
    context = {
        "is_personal_user": personal_user,
        "is_personal_free": personal_free,
        "is_org_admin": is_org_admin,
        "is_org_staff": is_org_staff,
        "can_access_crm": can_access_crm,
        "payment_url": reverse("billing"),
        "access_blocked": False,
        "personal_pool_url": None,
    }

    if personal_free:
        context["plan_badge"] = {"type": "personal_free"}
    if personal_user:
        pool = personal_pool(user)
        if pool:
            context["personal_pool_url"] = reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})
        else:
            context["personal_pool_url"] = reverse("pool_create")

    org = organization_for_user(user)
    if not org:
        return context

    now = timezone.now()
    context["access_blocked"] = is_org_access_blocked(user, now=now)

    if org.paid_until and org.paid_until >= now:
        context["plan_badge"] = {"type": "company_paid", "paid_until": org.paid_until}
        return context

    trial_end = trial_ends_at(org)
    if trial_end and trial_end > now:
        context["plan_badge"] = {
            "type": "company_trial",
            "days_left": company_trial_days_left(org, now=now),
        }
        return context

    if trial_end and trial_end <= now:
        context["plan_badge"] = {"type": "company_expired", "days_left": 0}
        return context

    if not company_has_access(org, now=now):
        context["plan_badge"] = {"type": "company_expired", "days_left": 0}
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
    from pool_service.models import Client, ClientAccess, OrganizationAccess

    has_org_access = OrganizationAccess.objects.filter(user=user).exists()
    has_client_access = ClientAccess.objects.filter(user=user).exists()
    has_client_profile = Client.objects.filter(user=user).exists()
    public_key = getattr(settings, "VAPID_PUBLIC_KEY", "")
    private_key = getattr(settings, "VAPID_PRIVATE_KEY", "")
    enabled = bool((has_org_access or has_client_access or has_client_profile) and public_key and private_key)
    return {
        "push_enabled": enabled,
        "push_public_key": public_key or "",
    }
