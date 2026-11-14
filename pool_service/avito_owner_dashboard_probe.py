"""Private deployment receipt: render saved owner screens, never export their content."""
from contextlib import contextmanager
from django.db import connection as database
from django.test import RequestFactory
from django.utils import timezone


@contextmanager
def _reads_only():
    database.ensure_connection()
    def execute(executor, sql, params, many, context):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise RuntimeError("owner_probe_write_refused")
        return executor(sql, params, many, context)
    with database.execute_wrapper(execute):
        yield


def runtime_evidence():
    from .avito_services_workplace_setup import _connection, _actor
    from .avito_management import avito_dashboard
    from .avito_owner_dashboard import display
    from .communication_models import ChannelConnection
    try:
        with _reads_only():
            services = _connection(4)
            actor = _actor(services)
            connections = [services]
            goods = ChannelConnection.objects.select_related("channel__organization").filter(
                pk=1, channel__organization_id=services.channel.organization_id,
                channel__kind="avito", is_active=True, channel__is_active=True).first()
            if goods:
                connections.append(goods)
            rows = []
            for selected in connections:
                request = RequestFactory().get("/avito/", {"account": str(selected.pk)},
                    secure=True, HTTP_HOST="service2.aqualine22.ru")
                request.user = actor
                request.session = {}
                response = avito_dashboard(request)
                content = response.content
                data = display(selected, actor)
                if response.status_code != 200 or b"data-avito-owner" not in content or not isinstance(data, dict):
                    raise RuntimeError("owner_probe_render_failed")
                source = data.get("source", {})
                chart = data.get("chart", {})
                rows.append({"connection_id": selected.pk, "http_status": response.status_code,
                    "rendered": True, "available": data.get("available") is True,
                    "known_kpis": sum(item.get("value") is not None for item in data.get("kpis", [])),
                    "chart_dates": len(chart.get("labels", [])),
                    "known_view_dates": sum(v is not None for v in chart.get("views", [])),
                    "known_contact_dates": sum(v is not None for v in chart.get("contacts", [])),
                    "source": source.get("kind"), "source_received_at": source["received_at"].isoformat() if hasattr(source.get("received_at"), "isoformat") else None,
                    "pages_complete": source.get("pages_complete") is True})
        return {"state": "rendered_saved_data", "checked_at": timezone.now().isoformat(),
            "provider_request_performed": False, "database_write_performed": False, "screens": rows}
    except Exception:
        # No exception strings, request details, HTML, sums or customer information leave this helper.
        return {"state": "unconfirmed", "code": "owner_dashboard_render_unconfirmed",
            "checked_at": timezone.now().isoformat(), "database_write_performed": False}
