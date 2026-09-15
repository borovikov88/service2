from django.apps import AppConfig


class PoolServiceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pool_service'

    def import_models(self):
        super().import_models()
        # Auxiliary model modules belong to the existing pool_service app and
        # must be registered before migration state/checks are evaluated.
        import pool_service.finance_position_models  # noqa: F401
        import pool_service.onec_diagnostic_mcp_models  # noqa: F401

    def ready(self):
        # Импортируем модуль с сигналами, чтобы он был зарегистрирован при запуске приложения
        import pool_service.signals
