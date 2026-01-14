from django.utils import timezone
from django.shortcuts import redirect
from .models import Profile
from .seo import is_indexable_host


class TimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            profile, _ = Profile.objects.get_or_create(user=request.user, defaults={"timezone": "Europe/Moscow"})
            timezone.activate(profile.timezone)
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


class RobotsTagMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        host = request.get_host().split(":", 1)[0].lower()
        if not is_indexable_host(host):
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response
