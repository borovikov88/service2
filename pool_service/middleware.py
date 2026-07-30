from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .models import OrganizationAccess, Profile
from .seo import is_indexable_host
from .security import (
    SESSION_LAST_ACTIVITY_KEY,
    SESSION_LOCKED_KEY,
    has_security_pin,
    idle_timeout_seconds,
    lock_session,
    timestamp_now,
    unlock_url,
)


class TimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                profile = request.user.profile
            except Profile.DoesNotExist:
                profile = Profile.objects.create(user=request.user, timezone="Europe/Moscow")
            try:
                timezone.activate(ZoneInfo(profile.timezone or "Europe/Moscow"))
            except ZoneInfoNotFoundError:
                timezone.activate(ZoneInfo("Europe/Moscow"))
        else:
            timezone.deactivate()
        return self.get_response(request)


class AuthRedirectMiddleware:
    """
    Redirect unauthenticated users to login page except for allowed paths.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        allowed_prefixes = [
            "/accounts/login/",
            "/accounts/logout/",
            "/accounts/password-reset/",
            "/accounts/reset/",
            "/accounts/confirm-email/",
            "/accounts/confirm-phone/",
            "/invite/",
            "/client-invite/",
            "/register/",
            "/signup/personal/",
            "/signup/company/",
            "/api/smsru/",
            "/static/",
            "/consent/",
            "/sw.js",
            "/manifest.webmanifest",
            "/robots.txt",
            "/sitemap.xml",
        ]
        if not request.user.is_authenticated:
            path = request.path
            if path not in {"/", "/index/"} and not any(path.startswith(p) for p in allowed_prefixes):
                return redirect("/accounts/login/")
        return self.get_response(request)


class SessionSecurityMiddleware:
    allowed_prefixes = (
        "/accounts/logout/",
        "/security/unlock/",
        "/security/lock/",
        "/security/passkeys/authenticate/",
        "/static/",
        "/manifest.webmanifest",
        "/sw.js",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if not user.is_authenticated:
            return self.get_response(request)

        path = request.path
        if any(path.startswith(prefix) for prefix in self.allowed_prefixes):
            return self.get_response(request)

        has_passkey = user.webauthn_credentials.exists()
        if not has_security_pin(user) and not has_passkey:
            request.session[SESSION_LOCKED_KEY] = False
            request.session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now()
            return self.get_response(request)

        now_ts = timestamp_now()
        last_activity = int(request.session.get(SESSION_LAST_ACTIVITY_KEY) or now_ts)
        locked = bool(request.session.get(SESSION_LOCKED_KEY))
        if not locked and now_ts - last_activity >= idle_timeout_seconds():
            lock_session(request, next_url=request.get_full_path())
            locked = True

        if locked:
            if request.headers.get("x-requested-with") == "XMLHttpRequest" or path.startswith("/api/"):
                return JsonResponse({"locked": True, "unlock_url": unlock_url(request.get_full_path())}, status=423)
            return redirect(unlock_url(request.get_full_path()))

        request.session[SESSION_LAST_ACTIVITY_KEY] = now_ts
        request.session.modified = True
        return self.get_response(request)


class FinanceOnlyRoleMiddleware:
    operational_roles = {"owner", "admin", "manager", "service", "installer"}
    allowed_prefixes = (
        "/accounts/",
        "/api/push/",
        "/billing/",
        "/consent/",
        "/finance/",
        "/media/",
        "/notifications/",
        "/profile/",
        "/security/",
        "/static/",
        "/manifest.webmanifest",
        "/sw.js",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not user.is_superuser:
            roles = set(
                OrganizationAccess.objects.filter(user=user).values_list("role", flat=True)
            )
            finance_only = "accountant" in roles and not bool(roles & self.operational_roles)
            if finance_only and request.path.startswith("/pools/"):
                if request.path.endswith("/service-details/"):
                    return self.get_response(request)
                if request.method in {"GET", "HEAD", "OPTIONS"}:
                    return self.get_response(request)
                return HttpResponseForbidden()
            if finance_only and not any(request.path.startswith(prefix) for prefix in self.allowed_prefixes):
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    return HttpResponseForbidden()
                return redirect(reverse("finance_dashboard"))
        return self.get_response(request)


class RobotsTagMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        host = request.get_host().split(":", 1)[0].lower()
        if not is_indexable_host(host):
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response
