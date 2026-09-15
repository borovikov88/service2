import os

from django.apps import AppConfig
from django.conf import settings


_DIAGNOSTIC_ENV_SETTINGS = (
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_RESOURCE_URL",
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_AUTH_ISSUER",
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_ACCESS_TOKEN_TTL_SECONDS",
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_REFRESH_TOKEN_TTL_SECONDS",
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_AUTHORIZATION_CODE_TTL_SECONDS",
)


def _wire_onec_diagnostic_mcp_environment():
    """Expose Diagnostic MCP env vars through the normal Django settings object."""
    raw_enabled = os.getenv("ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED")
    if raw_enabled is not None:
        settings.ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED = (
            raw_enabled.strip().lower() in {"1", "true", "yes", "on"}
        )

    raw_origins = os.getenv("ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_ORIGINS")
    if raw_origins is not None:
        settings.ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_ORIGINS = {
            origin.strip() for origin in raw_origins.split(",") if origin.strip()
        }

    for name in _DIAGNOSTIC_ENV_SETTINGS:
        value = os.getenv(name)
        if value is not None:
            setattr(settings, name, value)


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
        _wire_onec_diagnostic_mcp_environment()
        # Импортируем модуль с сигналами, чтобы он был зарегистрирован при запуске приложения
        import pool_service.signals
