from django.apps import AppConfig


class PoolServiceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pool_service'

    def ready(self):
        # Additional finance-position models belong to the existing pool_service app.
        import pool_service.finance_position_models  # noqa: F401
        # Импортируем модуль с сигналами, чтобы он был зарегистрирован при запуске приложения
        import pool_service.signals
