"""Actual Chromium owner-dashboard acceptance; synthetic data, no Avito I/O."""
from copy import deepcopy
from datetime import date, timedelta
import json
import os
from pathlib import Path
import subprocess
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess


@skipUnless(os.environ.get("AVITO_OWNER_BROWSER_TEST") == "1", "Explicit owner-dashboard Chromium gate")
class AvitoOwnerDashboardBrowserTests(StaticLiveServerTestCase):
    """UI coverage is distinct from the adapter's real saved-data regression tests."""

    def test_services_goods_charts_periods_search_and_mobile_themes(self):
        organization = Organization.objects.create(
            name="Synthetic Avito owner browser",
            paid_until=timezone.now() + timedelta(days=30),
        )
        owner = get_user_model().objects.create_user(username="synthetic-avito-browser")
        OrganizationAccess.objects.create(user=owner, organization=organization, role="owner")
        channel = CommunicationChannel.objects.create(
            organization=organization, kind="avito", name="Synthetic Avito"
        )

        def workspace(account_id):
            return {
                "avito_workspace": {
                    "account_id": account_id,
                    "sections": {
                        "items": {
                            "status": "ok",
                            "checked_at": timezone.now().isoformat(),
                            "data": {
                                "rows": [
                                    {"id": "91001", "title": "Synthetic Water", "status": "active",
                                     "status_label": "Активно", "price": "12.00", "address": "Synthetic City"},
                                    {"id": "91002", "title": "Synthetic Pool", "status": "active",
                                     "status_label": "Активно", "price": "18.00", "address": "Synthetic City"},
                                ],
                                "page": 1, "per_page": 50, "total": 2,
                                "has_previous": False, "has_next": False, "status": "",
                            },
                        }
                    },
                }
            }

        goods = ChannelConnection.objects.create(
            pk=1, channel=channel, name="Товары", external_id="700001",
            settings=workspace("700001"),
        )
        services = ChannelConnection.objects.create(
            pk=4, channel=channel, name="Услуги", external_id="446312402",
            settings=workspace("446312402"),
        )
        original_goods = deepcopy(goods.settings)
        original_services = deepcopy(services.settings)
        start, end = date(2026, 1, 1), date(2026, 1, 7)
        labels = [(start + timedelta(days=index)).isoformat() for index in range(7)]

        def display(connection, actor, **options):
            if connection is None:
                return None
            is_services = connection.pk == 4
            chart = {
                "labels": labels,
                "views": [10, 12, None, 18, 0, 15, 20],
                "contacts": [1, 0, 0, 2, 0, 1, 2],
                "spending": [100, 100, 0, 200, 0, 100, 200],
                "cost_view": [10, 8.33, None, 11.11, None, 6.67, 10],
                "cost_contact": [100, None, None, 100, None, 100, 100],
            }
            if not is_services:
                chart = {key: labels if key == "labels" else [None] * len(labels) for key in chart}
            raw_period = options.get("period", "30")
            source_from = options.get("date_from") or start.isoformat()
            source_to = options.get("date_to") or end.isoformat()
            values = (
                ("views", "Просмотры", None, ""),
                ("contacts", "Контакты Авито", "6", ""),
                ("spending", "Денежные расходы", "700.00", "₽"),
                ("cost_view", "₽ / просмотр", None, "₽"),
                ("cost_contact", "₽ / контакт", "116.67", "₽"),
            )
            return {
                "available": is_services,
                "kind": "services" if is_services else "goods",
                "title": "Услуги" if is_services else "Товары",
                "period": {"key": raw_period, "from": source_from, "to": source_to,
                           "label": f"{source_from}–{source_to}", "error": ""},
                "kpis": [
                    {"key": key, "label": label, "value": value if is_services else None,
                     "unit": unit, "known": (6 if key in {"views", "cost_view"} else 7) if is_services else 0, "expected": 7,
                     "delta": None, "reason": "Неизвестные входы" if is_services and value is None else ("" if is_services else "Нет сохранённых данных")}
                    for key, label, value, unit in values
                ],
                "chart": chart,
                "source": {
                    "kind": "daily" if is_services else "unavailable",
                    "label": "Synthetic saved statistics",
                    "from": source_from, "to": source_to, "received_at": timezone.now(),
                    "provider_timezone": "unknown", "pages_complete": is_services,
                    "completed_dates": 7 if is_services else 0, "total_dates": 7,
                    "reported_ads": 2 if is_services else 0, "expected_ads": 2,
                    "coverage_label": "Страницы: 7/7; просмотры известны: 6/7" if is_services else "Нет сохранённых данных",
                },
                "fallback": None,
                "ads": [],
                "limitations": ["Часовой пояс источника не подтверждён."],
            }

        self.client.force_login(owner)
        payload = {
            "base": self.live_server_url,
            "url": reverse("avito_dashboard"),
            "servicesId": services.pk,
            "goodsId": goods.pk,
            "sessionCookie": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            "sessionCookieName": settings.SESSION_COOKIE_NAME,
        }
        with (
            patch("pool_service.avito_owner_dashboard.display", side_effect=display),
            patch("pool_service.avito_management.access_token") as token,
            patch("pool_service.avito_workspace._get") as provider_get,
        ):
            result = subprocess.run(
                ["node", str(Path(__file__).with_name("avito_owner_dashboard.browser.cjs"))],
                input=json.dumps(payload), text=True, capture_output=True, timeout=120,
            )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-6000:])
        token.assert_not_called()
        provider_get.assert_not_called()
        goods.refresh_from_db()
        services.refresh_from_db()
        self.assertEqual(goods.settings, original_goods)
        self.assertEqual(services.settings, original_services)
