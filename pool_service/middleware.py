from django.utils import timezone
from .models import Profile  # убедитесь, что импортируете модель Profile

class TimezoneMiddleware:
    """
    Middleware, который активирует часовой пояс пользователя, если он авторизован.
    Если профиль отсутствует, создаёт его с часовым поясом по умолчанию.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            # Попытка получить профиль, или создать его, если отсутствует
            profile, created = Profile.objects.get_or_create(user=request.user, defaults={'timezone': 'Europe/Moscow'})
            timezone.activate(profile.timezone)
        else:
            timezone.deactivate()
        response = self.get_response(request)
        return response
