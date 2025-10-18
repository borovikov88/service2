from django.apps import AppConfig


class PoolServiceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pool_service'

    def ready(self):
        # Импортируем модуль с сигналами, чтобы он был зарегистрирован при запуске приложения
        import pool_service.signals