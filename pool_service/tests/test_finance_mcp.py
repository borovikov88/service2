import base64
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.management_finance import (
    get_cashflow_breakdown as canonical_cashflow_breakdown,
)
from pool_service.finance_mcp_auth import (
    CHATGPT_CLIENT_ID_METADATA_URL,
    FINANCE_READ_SCOPE,
    OFFLINE_ACCESS_SCOPE,
    fetch_trusted_chatgpt_client_metadata,
)
from pool_service.finance_mcp_views import MCP_PROTOCOL_VERSION, TOOL_NAMES
from pool_service.models import (
    CashFlowArticleMapping,
    CashFlowRow,
    FinanceMcpAccessToken,
    FinanceMcpAuditEvent,
    FinanceMcpClient,
    FinanceMcpGrant,
    FinanceMcpPrincipal,
    FinanceMcpPrincipalOrganization,
    FinanceMcpRefreshToken,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
    PayrollAccrualMonth,
)


MCP_SETTINGS = {
    "ADVISOR_FINANCE_MCP_ENABLED": True,
    "SITE_URL": "https://service2.example.test",
    "ADVISOR_FINANCE_MCP_RESOURCE_URL": "https://service2.example.test/mcp/finance",
    "ADVISOR_FINANCE_MCP_AUTH_ISSUER": "https://service2.example.test",
    "ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS": {"https://chatgpt.com"},
}


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@override_settings(**MCP_SETTINGS)
class FinanceMcpTests(TestCase):
    month = date(2026, 1, 1)
    redirect_uri = "https://chatgpt.com/connector_platform_oauth_redirect"

    def setUp(self):
        self.owner = User.objects.create_user("finance-mcp-owner", password="pass")
        self.organization = Organization.objects.create(
            name="Разрешённая организация",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.foreign_organization = Organization.objects.create(name="Чужая организация")
        OrganizationAccess.objects.create(
            user=self.owner, organization=self.organization, role="owner"
        )
        self.principal = FinanceMcpPrincipal.objects.create(
            subject="chatgpt:finance-test:v1", display_name="Финансовый тест"
        )
        FinanceMcpPrincipalOrganization.objects.create(
            principal=self.principal,
            organization=self.organization,
            granted_by=self.owner,
        )
        self.oauth_client = FinanceMcpClient.objects.create(
            client_id=CHATGPT_CLIENT_ID_METADATA_URL,
            display_name="ChatGPT Finance Test",
            principal=self.principal,
            redirect_uris=[self.redirect_uri],
            client_metadata_sha256="a" * 64,
            client_metadata_verified_at=timezone.now(),
        )
        self._populate_finance_data()
        self.raw_access_token = self._create_access_token()

    def _batch(self, organization, import_type, suffix, *, source_type=None):
        values = {}
        if source_type is not None:
            values["source_type"] = source_type
        return OneCImportBatch.objects.create(
            organization=organization,
            import_type=import_type,
            original_filename=f"{suffix}.xlsx",
            stored_file=f"test/{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64],
            uploaded_by=self.owner,
            status=OneCImportBatch.STATUS_CONFIRMED,
            **values,
        )

    def _activate(self, organization, batch, report_type):
        OneCReportPeriodState.objects.update_or_create(
            organization=organization,
            report_type=report_type,
            period_month=self.month,
            defaults={"active_batch": batch, "updated_by": self.owner},
        )

    def _populate_finance_data(self):
        cashflow_batch = self._batch(
            self.organization, OneCImportBatch.TYPE_CASHFLOW, "mcp-cashflow"
        )
        self.cashflow_batch = cashflow_batch
        self._activate(
            self.organization, cashflow_batch, OneCImportBatch.TYPE_CASHFLOW
        )
        for article, flow in (
            ("Оплата от покупателей", CashFlowArticleMapping.FLOW_OPERATING),
            ("Оплата поставщикам", CashFlowArticleMapping.FLOW_OPERATING),
            ("Размещение депозита", CashFlowArticleMapping.FLOW_LIQUIDITY),
        ):
            CashFlowArticleMapping.objects.create(
                organization=self.organization,
                article_name=article,
                normalized_article_name=article.casefold(),
                management_category="Тестовая категория",
                flow_type=flow,
                classification_status=CashFlowArticleMapping.CLASS_CONFIRMED,
            )
        for number, article, receipts, payments in (
            (1, "Оплата от покупателей", "100.00", "0.00"),
            (2, "Оплата поставщикам", "0.00", "40.00"),
            (3, "Размещение депозита", "0.00", "10.00"),
        ):
            CashFlowRow.objects.create(
                organization=self.organization,
                import_batch=cashflow_batch,
                period_month=self.month,
                source_row_number=number,
                article_raw=article,
                normalized_article_name=article.casefold(),
                document_raw="Документ",
                receipts=Decimal(receipts),
                payments=Decimal(payments),
                net_cash_flow=Decimal("0"),
            )
        # A confirmed but never activated batch must not affect the MCP: it
        # exercises the exact active-version semantics of the canonical service.
        inactive_cashflow_batch = self._batch(
            self.organization, OneCImportBatch.TYPE_CASHFLOW, "mcp-cashflow-inactive"
        )
        CashFlowRow.objects.create(
            organization=self.organization,
            import_batch=inactive_cashflow_batch,
            period_month=self.month,
            source_row_number=1,
            article_raw="Оплата от покупателей",
            normalized_article_name="оплата от покупателей",
            document_raw="Неактивный документ",
            receipts=Decimal("999.00"),
            payments=Decimal("0.00"),
            net_cash_flow=Decimal("0"),
        )

        profit_batch = self._batch(
            self.organization, OneCImportBatch.TYPE_MONTHLY_PROFIT, "mcp-profit"
        )
        self._activate(
            self.organization, profit_batch, OneCImportBatch.TYPE_MONTHLY_PROFIT
        )
        OneCMonthlyProfit.objects.create(
            organization=self.organization,
            import_batch=profit_batch,
            period_month=self.month,
            source_row_number=1,
            nomenclature="Услуга",
            manager_name="Менеджер",
            customer_name="Клиент",
            document_name="Реализация",
            revenue=Decimal("100.00"),
            cost=Decimal("40.00"),
            gross_profit=Decimal("60.00"),
            analytical_gross_profit=Decimal("60.00"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
        )

        payroll_batch = self._batch(
            self.organization,
            OneCImportBatch.TYPE_PAYROLL_ACCRUAL,
            "mcp-payroll",
            source_type=OneCImportBatch.SOURCE_ODATA,
        )
        self._activate(
            self.organization, payroll_batch, OneCImportBatch.TYPE_PAYROLL_ACCRUAL
        )
        PayrollAccrualMonth.objects.create(
            organization=self.organization,
            import_batch=payroll_batch,
            period_month=self.month,
            accrued=Decimal("30.00"),
            currency_guid=uuid.uuid4(),
            source_organization_guids=[],
            source_rows=1,
        )

    def _add_active_cashflow_row(self, number, article, receipts, payments):
        return CashFlowRow.objects.create(
            organization=self.organization,
            import_batch=self.cashflow_batch,
            period_month=self.month,
            source_row_number=number,
            article_raw=article,
            normalized_article_name=article.casefold(),
            document_raw="Проверочный документ",
            receipts=Decimal(receipts),
            payments=Decimal(payments),
            net_cash_flow=Decimal("0"),
        )

    @staticmethod
    def _financial_read_snapshot():
        """All finance source/configuration rows MCP must not mutate."""
        return {
            "cashflow_rows": list(CashFlowRow.objects.order_by("pk").values()),
            "profit_rows": list(OneCMonthlyProfit.objects.order_by("pk").values()),
            "payroll_accrual_months": list(
                PayrollAccrualMonth.objects.order_by("pk").values()
            ),
            "cashflow_mappings": list(
                CashFlowArticleMapping.objects.order_by("pk").values()
            ),
            "active_versions": list(
                OneCReportPeriodState.objects.order_by("pk").values()
            ),
            "import_batches": list(OneCImportBatch.objects.order_by("pk").values()),
        }

    def _create_access_token(self, *, expired=False):
        grant = FinanceMcpGrant.objects.create(
            client=self.oauth_client,
            principal=self.principal,
            authorized_by=self.owner,
            scopes=[FINANCE_READ_SCOPE],
            resource=MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
        )
        raw = "access-token-" + uuid.uuid4().hex
        FinanceMcpAccessToken.objects.create(
            grant=grant,
            token_hash=_hash(raw),
            audience=MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
            scopes=[FINANCE_READ_SCOPE],
            expires_at=timezone.now() - timedelta(seconds=1) if expired else timezone.now() + timedelta(minutes=5),
        )
        return raw

    def _mcp_post(self, payload, *, token=None, **headers):
        extra = {
            "HTTP_ACCEPT": "application/json, text/event-stream",
            "HTTP_MCP_PROTOCOL_VERSION": MCP_PROTOCOL_VERSION,
        }
        if token is not None:
            extra["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        extra.update(headers)
        return self.client.post(
            reverse("finance_mcp"),
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def _tool(self, name, arguments, *, token=None):
        return self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 17,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            token=token or self.raw_access_token,
        )

    def _authorization_params(self, *, client_id=None, state="state-123"):
        verifier = "x" * 43
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        return verifier, {
            "response_type": "code",
            "client_id": client_id or self.oauth_client.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": f"{FINANCE_READ_SCOPE} {OFFLINE_ACCESS_SCOPE}",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
        }

    def test_unauthenticated_streamable_post_is_bearer_challenge_not_login_redirect(self):
        response = self._mcp_post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response["WWW-Authenticate"])
        self.assertIn("resource_metadata=", response["WWW-Authenticate"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_streamable_http_validates_present_origin_before_bearer_or_body(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ChatGPT", "version": "test"},
            },
        }
        valid = self._mcp_post(
            payload,
            token=self.raw_access_token,
            HTTP_ORIGIN="https://chatgpt.com",
        )
        self.assertEqual(valid.status_code, 200)
        invalid = self._mcp_post(
            payload,
            HTTP_ORIGIN="https://untrusted.example.test",
        )
        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(invalid["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", invalid)

    def test_metadata_and_tools_list_publish_only_read_only_finance_tools(self):
        metadata = self.client.get(reverse("finance_mcp_protected_resource_metadata"))
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(
            metadata.json()["resource"], MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"]
        )
        self.assertEqual(metadata.json()["scopes_supported"], [FINANCE_READ_SCOPE])
        authorization_metadata = self.client.get(
            reverse("finance_mcp_authorization_server_metadata")
        ).json()
        self.assertEqual(authorization_metadata["code_challenge_methods_supported"], ["S256"])
        self.assertIn("refresh_token", authorization_metadata["grant_types_supported"])
        self.assertTrue(authorization_metadata["client_id_metadata_document_supported"])
        self.assertEqual(authorization_metadata["token_endpoint_auth_methods_supported"], ["none"])
        response = self._mcp_post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            token=self.raw_access_token,
        )
        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        self.assertEqual(tuple(item["name"] for item in tools), TOOL_NAMES)
        self.assertTrue(all(item["annotations"]["readOnlyHint"] for item in tools))
        self.assertTrue(all(not item["annotations"]["destructiveHint"] for item in tools))

    def test_trusted_chatgpt_cimd_is_pinned_and_arbitrary_client_is_rejected(self):
        metadata_document = {
            "client_id": CHATGPT_CLIENT_ID_METADATA_URL,
            "client_name": "ChatGPT",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
            # The fetch helper intentionally ignores this public URL rather
            # than retaining it or fetching anything at OAuth runtime.
            "jwks_uri": "https://chatgpt.com/oauth/jwks.json",
        }

        class Response:
            def __init__(self, body):
                self.headers = {
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                }
                self._body = body
                self.closed = False

            def getcode(self):
                return 200

            def read(self, size):
                value, self._body = self._body[:size], self._body[size:]
                return value

            def close(self):
                self.closed = True

        class Opener:
            def __init__(self, response):
                self.response = response
                self.request = None
                self.timeout = None

            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return self.response

        response = Response(json.dumps(metadata_document).encode("utf-8"))
        opener = Opener(response)
        with patch("pool_service.finance_mcp_auth.build_opener", return_value=opener) as factory:
            verified = fetch_trusted_chatgpt_client_metadata()
        self.assertEqual(verified["client_id"], CHATGPT_CLIENT_ID_METADATA_URL)
        self.assertEqual(verified["redirect_uris"], (self.redirect_uri,))
        self.assertEqual(len(verified["sha256"]), 64)
        self.assertEqual(opener.request.full_url, CHATGPT_CLIENT_ID_METADATA_URL)
        self.assertEqual(opener.timeout, 8)
        self.assertTrue(response.closed)
        handler = factory.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, None, None, None))

        missing_name = dict(metadata_document)
        missing_name.pop("client_name")
        with patch(
            "pool_service.finance_mcp_auth.build_opener",
            return_value=Opener(Response(json.dumps(missing_name).encode("utf-8"))),
        ):
            with self.assertRaisesMessage(RuntimeError, "не содержит client_name"):
                fetch_trusted_chatgpt_client_metadata()

        arbitrary_client = FinanceMcpClient.objects.create(
            client_id="https://untrusted.example.test/oauth/client.json",
            display_name="Untrusted client",
            principal=self.principal,
            redirect_uris=[self.redirect_uri],
            client_metadata_sha256="b" * 64,
            client_metadata_verified_at=timezone.now(),
        )
        _verifier, params = self._authorization_params(
            client_id=arbitrary_client.client_id,
            state="arbitrary-client",
        )
        self.client.force_login(self.owner)
        denied = self.client.get(reverse("finance_mcp_authorize"), params)
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn("Location", denied)

    def test_list_organizations_returns_only_the_server_side_principal_scope(self):
        response = self._tool("list_finance_organizations", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["result"]["structuredContent"]["organizations"],
            [{"id": self.organization.id, "name": self.organization.name}],
        )
        envelope = response.json()["result"]["structuredContent"]
        self.assertEqual(envelope["period"], {"from": None, "to": None})
        self.assertEqual(envelope["missing_months"], [])
        self.assertEqual(envelope["preliminary_months"], [])
        self.assertEqual(envelope["active_batch_ids"], [])
        self.assertEqual(envelope["active_versions"], [])
        audit = FinanceMcpAuditEvent.objects.get()
        self.assertEqual(audit.organization_ids, [self.organization.id])

    def test_omitted_organization_filter_audits_the_resolved_scope(self):
        response = self._tool("get_finance_data_status", {})
        self.assertEqual(response.status_code, 200)
        audit = FinanceMcpAuditEvent.objects.get()
        self.assertEqual(audit.tool_name, "get_finance_data_status")
        self.assertEqual(audit.organization_ids, [self.organization.id])

    @override_settings(ADVISOR_FINANCE_MCP_ENABLED=False)
    def test_disabled_finance_endpoints_are_not_cacheable(self):
        for response in (
            self.client.post(reverse("finance_mcp"), data=b"{}", content_type="application/json"),
            self.client.get(reverse("finance_mcp_protected_resource_metadata")),
            self.client.get(reverse("finance_mcp_authorization_server_metadata")),
            self.client.get(reverse("finance_mcp_authorize")),
            self.client.post(reverse("finance_mcp_token"), data=b"", content_type="application/x-www-form-urlencoded"),
        ):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response["Cache-Control"], "no-store")

    def test_mcp_monthly_result_matches_canonical_service_decimal_for_decimal(self):
        before = self._financial_read_snapshot()
        response = self._tool("get_monthly_finance", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "organization_ids": [self.organization.id],
        })
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        month = data["organization_results"][0]["monthly"][0]
        self.assertEqual(month["profit"], {
            "revenue": "100.00",
            "source_cost": "40.00",
            "analytical_cost": "40.00",
            "gross_profit": "60.00",
            "gross_margin": "60.00",
        })
        self.assertEqual(month["payroll_accrued"], "30.00")
        self.assertEqual(month["cashflow"]["operating_net_cash_flow"], "60.00")
        self.assertEqual(month["cashflow"]["liquidity_net_cash_flow"], "-10.00")
        self.assertEqual(month["cashflow"]["external_net_cash_flow"], "50.00")
        canonical = canonical_cashflow_breakdown(
            self.organization, "2026-01", "2026-01"
        )
        self.assertEqual(
            data["organization_results"][0]["totals"]["cashflow"]["operating_net_cash_flow"],
            canonical["totals"]["operating"]["net_cash_flow"],
        )
        self.assertEqual(before, self._financial_read_snapshot())
        audit = FinanceMcpAuditEvent.objects.get()
        self.assertEqual(audit.result, FinanceMcpAuditEvent.RESULT_SUCCESS)
        self.assertIn("get_monthly_finance", str(audit))
        self.assertIn(f"principal={self.principal.id}", str(audit))

    def test_mcp_matches_owner_dashboard_and_keeps_internal_and_unclassified_separate(self):
        CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name="Внутреннее перемещение",
            normalized_article_name="внутреннее перемещение",
            management_category="Внутренние обороты",
            flow_type=CashFlowArticleMapping.FLOW_OPERATING,
            is_internal_turnover=True,
            classification_status=CashFlowArticleMapping.CLASS_CONFIRMED,
        )
        CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name="Черновая статья",
            normalized_article_name="черновая статья",
            management_category="Не применять",
            flow_type=CashFlowArticleMapping.FLOW_OPERATING,
            classification_status=CashFlowArticleMapping.CLASS_NEEDS_REVIEW,
        )
        self._add_active_cashflow_row(4, "Внутреннее перемещение", "25.00", "5.00")
        self._add_active_cashflow_row(5, "Нет mapping", "7.00", "2.00")
        self._add_active_cashflow_row(6, "Черновая статья", "3.00", "1.00")

        monthly = self._tool("get_monthly_finance", {
            "start_month": "2026-01",
            "end_month": "2026-01",
        })
        self.assertEqual(monthly.status_code, 200)
        metrics = monthly.json()["result"]["structuredContent"]["organization_results"][0]["monthly"][0]["cashflow"]
        # 999.00 from the inactive batch must not leak into the active MCP
        # view; the confirmed operating rows remain exactly 100 - 40.
        self.assertEqual(metrics["operating_net_cash_flow"], "60.00")
        self.assertEqual(metrics["liquidity_net_cash_flow"], "-10.00")
        self.assertEqual(metrics["internal_net_cash_flow"], "20.00")
        # Missing mapping (+5) and unconfirmed mapping (+2) stay visible in
        # external unclassified rather than being silently moved to operating.
        self.assertEqual(metrics["external_unclassified_net_cash_flow"], "7.00")
        self.assertEqual(metrics["external_net_cash_flow"], "57.00")

        breakdown = self._tool("get_cashflow_breakdown", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "group_by": "flow_type",
        })
        grouped = {
            item["flow_type"]: item
            for item in breakdown.json()["result"]["structuredContent"]["consolidated"]["items"]
        }
        self.assertEqual(grouped["internal"]["net_cash_flow"], "20.00")
        self.assertEqual(grouped["unclassified"]["net_cash_flow"], "7.00")

        self.client.force_login(self.owner)
        dashboard = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01",
            "period_to": "2026-01",
        })
        self.assertEqual(dashboard.status_code, 200)
        # This calls the real owner DДС view.  Its context and the MCP adapter
        # must remain byte-for-byte equal after Decimal serialization.
        self.assertEqual(metrics["operating_net_cash_flow"], format(
            dashboard.context["operating"]["net_cash_flow"], "f"
        ))
        self.assertEqual(metrics["internal_net_cash_flow"], format(
            dashboard.context["internal"]["net_cash_flow"], "f"
        ))
        self.assertEqual(metrics["external_unclassified_net_cash_flow"], format(
            dashboard.context["external_unclassified"]["net_cash_flow"], "f"
        ))
        self.assertEqual(metrics["external_net_cash_flow"], format(
            dashboard.context["external"]["net_cash_flow"], "f"
        ))

    def test_unknown_organization_denies_the_whole_call_and_audits_without_data(self):
        response = self._tool("get_monthly_finance", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "organization_ids": [self.organization.id, self.foreign_organization.id],
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["isError"])
        audit = FinanceMcpAuditEvent.objects.get()
        self.assertEqual(audit.result, FinanceMcpAuditEvent.RESULT_DENIED)
        self.assertEqual(audit.organization_ids, [self.organization.id, self.foreign_organization.id])

    def test_missing_values_remain_null_and_period_cap_and_pagination_are_enforced(self):
        missing = self._tool("get_monthly_finance", {
            "start_month": "2026-02",
            "end_month": "2026-02",
        })
        data = missing.json()["result"]["structuredContent"]
        record = data["organization_results"][0]["monthly"][0]
        self.assertIsNone(record["profit"]["revenue"])
        self.assertIsNone(record["payroll_accrued"])
        self.assertIsNone(record["cashflow"]["operating_net_cash_flow"])
        page = self._tool("get_cashflow_breakdown", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "group_by": "article",
            "page": 1,
            "page_size": 1,
        })
        paged = page.json()["result"]["structuredContent"]["consolidated"]
        self.assertEqual(len(paged["items"]), 1)
        self.assertEqual(paged["next_page"], 2)
        too_wide = self._tool("get_monthly_finance", {
            "start_month": "2024-01",
            "end_month": "2026-01",
        })
        self.assertTrue(too_wide.json()["result"]["isError"])
        too_many_flow_types = self._tool("get_cashflow_breakdown", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "group_by": "article",
            "flow_types": ["operating"] * 7,
        })
        self.assertTrue(too_many_flow_types.json()["result"]["isError"])
        too_many_categories = self._tool("get_cashflow_breakdown", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "group_by": "article",
            "management_categories": ["category"] * 51,
        })
        self.assertTrue(too_many_categories.json()["result"]["isError"])
        too_many_organizations = self._tool("get_monthly_finance", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "organization_ids": [self.organization.id] * 51,
        })
        self.assertTrue(too_many_organizations.json()["result"]["isError"])

    def test_profit_customer_alias_and_unavailable_department_are_honest(self):
        customer = self._tool("get_profit_breakdown", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "group_by": "customer",
        })
        item = customer.json()["result"]["structuredContent"]["consolidated"]["items"][0]
        self.assertEqual(item["customer"], "Клиент")
        department = self._tool("get_profit_breakdown", {
            "start_month": "2026-01",
            "end_month": "2026-01",
            "group_by": "department",
        })
        payload = department.json()["result"]["structuredContent"]
        self.assertFalse(payload["available"])
        self.assertEqual(
            payload["unavailable"]["reason"],
            "active_profit_rows_have_no_department_or_object_dimension",
        )

    def test_expired_or_revoked_access_token_is_rejected(self):
        expired = self._create_access_token(expired=True)
        expired_response = self._mcp_post(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
            token=expired,
        )
        self.assertEqual(expired_response.status_code, 401)
        token = FinanceMcpAccessToken.objects.get(token_hash=_hash(self.raw_access_token))
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        revoked_response = self._mcp_post(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
            token=self.raw_access_token,
        )
        self.assertEqual(revoked_response.status_code, 401)

    def test_oauth_authorization_code_pkce_refresh_rotation_and_reuse_revocation(self):
        verifier = "x" * 43
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        params = {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": self.redirect_uri,
            "state": "state-123",
            "scope": f"{FINANCE_READ_SCOPE} {OFFLINE_ACCESS_SCOPE}",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
        }
        self.client.force_login(self.owner)
        preview = self.client.get(reverse("finance_mcp_authorize"), params)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(FinanceMcpGrant.objects.count(), 1)  # direct bearer test grant only
        approved = self.client.post(
            reverse("finance_mcp_authorize"), {**params, "decision": "approve"}
        )
        self.assertEqual(approved.status_code, 302)
        query = parse_qs(urlsplit(approved["Location"]).query)
        code = query["code"][0]
        self.assertNotEqual(code, "")
        token_response = self.client.post(
            reverse("finance_mcp_token"),
            data=urlencode({
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.oauth_client.client_id,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
                "resource": MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
            }),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(token_response.status_code, 200)
        issued = token_response.json()
        self.assertIn("access_token", issued)
        self.assertIn("refresh_token", issued)
        self.assertFalse(FinanceMcpAccessToken.objects.filter(token_hash=issued["access_token"]).exists())
        self.assertFalse(FinanceMcpRefreshToken.objects.filter(token_hash=issued["refresh_token"]).exists())
        refresh_response = self.client.post(
            reverse("finance_mcp_token"),
            data=urlencode({
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": self.oauth_client.client_id,
                "resource": MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
            }),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(refresh_response.status_code, 200)
        replay_response = self.client.post(
            reverse("finance_mcp_token"),
            data=urlencode({
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": self.oauth_client.client_id,
                "resource": MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
            }),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(replay_response.status_code, 400)
        latest_grant = FinanceMcpGrant.objects.order_by("-id").first()
        self.assertIsNotNone(latest_grant.revoked_at)

    def test_valid_oauth_request_without_finance_permission_redirects_access_denied_with_iss(self):
        unprivileged = User.objects.create_user("mcp-no-finance-right", password="pass")
        _verifier, params = self._authorization_params(state="state-denied-with-iss")
        self.client.force_login(unprivileged)
        response = self.client.get(reverse("finance_mcp_authorize"), params)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response["Location"]).query)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["state-denied-with-iss"])
        self.assertEqual(query["iss"], [MCP_SETTINGS["ADVISOR_FINANCE_MCP_AUTH_ISSUER"]])
        self.assertNotIn("code", query)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_unauthenticated_authorize_preserves_oauth_request_through_login(self):
        verifier = "y" * 43
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        params = {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": self.redirect_uri,
            "state": "state-kept-through-login",
            "scope": FINANCE_READ_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP_SETTINGS["ADVISOR_FINANCE_MCP_RESOURCE_URL"],
        }
        response = self.client.get(reverse("finance_mcp_authorize"), params)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/accounts/login/?next="))
        self.assertEqual(response["Cache-Control"], "no-store")
        next_url = parse_qs(urlsplit(response["Location"]).query)["next"][0]
        self.assertIn("state=state-kept-through-login", next_url)
        self.assertIn("code_challenge_method=S256", next_url)
        self.assertIn("resource=", next_url)
        self.client.force_login(self.owner)
        consent = self.client.get(next_url)
        self.assertEqual(consent.status_code, 200)
        self.assertContains(consent, self.oauth_client.display_name)

    def test_provision_requires_finance_authority_for_every_scoped_organization(self):
        unauthorized = User.objects.create_user("mcp-untrusted-actor", password="pass")
        with self.assertRaisesMessage(CommandError, "не обладает правом управления финансами"):
            call_command(
                "manage_finance_mcp_access",
                "provision",
                "--client-id", CHATGPT_CLIENT_ID_METADATA_URL,
                "--client-name", "Untrusted test",
                "--principal-subject", "chatgpt:finance-test:untrusted",
                "--principal-name", "Недопустимый тест",
                "--redirect-uri", self.redirect_uri,
                "--organization-id", str(self.organization.id),
                "--actor-id", str(unauthorized.id),
                "--confirm", CHATGPT_CLIENT_ID_METADATA_URL,
            )
        self.assertFalse(
            FinanceMcpPrincipal.objects.filter(subject="chatgpt:finance-test:untrusted").exists()
        )

    def test_owner_safe_management_command_provisions_then_revokes_without_tokens(self):
        # A distinct subject is mandatory so no pre-existing scope can widen
        # client used elsewhere in this test.  Only the exact trusted CIMD ID
        # is legal, so remove the local fixture's client and assert that the
        # owner-operated command creates the replacement from verified metadata.
        FinanceMcpGrant.objects.all().delete()
        self.oauth_client.delete()
        verified = {
            "client_id": CHATGPT_CLIENT_ID_METADATA_URL,
            "redirect_uris": (self.redirect_uri,),
            "sha256": "c" * 64,
        }
        with patch(
            "pool_service.management.commands.manage_finance_mcp_access.fetch_trusted_chatgpt_client_metadata",
            return_value=verified,
        ):
            call_command(
                "manage_finance_mcp_access",
                "provision",
                "--client-id", CHATGPT_CLIENT_ID_METADATA_URL,
                "--client-name", "ChatGPT second finance test",
                "--principal-subject", "chatgpt:finance-test:v2",
                "--principal-name", "Финансовый тест 2",
                "--redirect-uri", self.redirect_uri,
                "--organization-id", str(self.organization.id),
                "--actor-id", str(self.owner.id),
                "--confirm", CHATGPT_CLIENT_ID_METADATA_URL,
            )
        client = FinanceMcpClient.objects.get(client_id=CHATGPT_CLIENT_ID_METADATA_URL)
        self.assertFalse(client.grants.exists())
        self.assertEqual(client.client_metadata_sha256, "c" * 64)
        self.assertIsNotNone(client.client_metadata_verified_at)
        call_command(
            "manage_finance_mcp_access",
            "revoke",
            "--client-id", client.client_id,
            "--actor-id", str(self.owner.id),
            "--reason", "test revocation",
            "--confirm", client.client_id,
        )
        client.refresh_from_db()
        self.assertFalse(client.is_active)

    def test_provision_rejects_arbitrary_client_id_before_any_metadata_fetch(self):
        with self.assertRaisesMessage(CommandError, "только доверенный ChatGPT CIMD"):
            call_command(
                "manage_finance_mcp_access",
                "provision",
                "--client-id", "chatgpt-service2-finance-v1",
                "--client-name", "Wrong client",
                "--principal-subject", "chatgpt:finance-test:wrong-client",
                "--principal-name", "Недопустимый client",
                "--redirect-uri", self.redirect_uri,
                "--organization-id", str(self.organization.id),
                "--actor-id", str(self.owner.id),
                "--confirm", "chatgpt-service2-finance-v1",
            )
        self.assertFalse(
            FinanceMcpPrincipal.objects.filter(subject="chatgpt:finance-test:wrong-client").exists()
        )
