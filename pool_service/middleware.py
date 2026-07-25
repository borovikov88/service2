from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .models import OrganizationAccess, Profile
from .seo import is_indexable_host


class TimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                profile = request.user.profile
            except Profile.DoesNotExist:
                profile = Profile.objects.create(user=request.user, timezone="Europe/Moscow")
            timezone.activate(profile.timezone or "Europe/Moscow")
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
