def brand_context(request):
    host = request.get_host().split(":", 1)[0].lower()

    default_brand = {
        "name": "RovikPool",
        "logo": "pool_service/img/logo.png",
        "favicon": "assets/images/favicon.png",
    }
    brands_by_host = {
        "rovikpool.ru": default_brand,
        "www.rovikpool.ru": default_brand,
        "service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine.png",
            "favicon": "assets/images/aqualine.png",
        },
        "www.service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine.png",
            "favicon": "assets/images/aqualine.png",
        },
    }

    brand = brands_by_host.get(host, default_brand)
    return {
        "brand_name": brand["name"],
        "brand_logo": brand["logo"],
        "brand_favicon": brand["favicon"],
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
        trial_ends_at,
    )

    personal_user = is_personal_user(user)
    is_org_admin = OrganizationAccess.objects.filter(user=user, role="admin").exists() or user.is_superuser
    personal_free = is_personal_free(user)
    context = {
        "is_personal_user": personal_user,
        "is_personal_free": personal_free,
        "is_org_admin": is_org_admin,
        "payment_url": reverse("billing"),
        "access_blocked": False,
    }

    if personal_free:
        context["plan_badge"] = {"type": "personal_free"}
        return context

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
