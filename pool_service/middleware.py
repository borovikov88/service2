from django.utils import timezone
from django.shortcuts import redirect
from .models import Profile


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
        allowed_paths = [
            "/accounts/login/",
            "/accounts/logout/",
            "/accounts/password-reset/",
            "/accounts/reset/",
            "/accounts/confirm-email/",
            "/register/",
            "/static/",
            "/consent/",
            "/sw.js",
        ]
        if not request.user.is_authenticated:
            path = request.path
            if not any(path.startswith(p) for p in allowed_paths):
                return redirect("/accounts/login/")
        return self.get_response(request)
