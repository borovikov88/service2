from datetime import date
from decimal import Decimal
from io import StringIO
import json
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from pool_service.finance_imports.odata_profit import (
    NoRedirectHandler,
    ODataConfig,
    ODataPreviewError,
    read_profit_preview,
    validate_config,
)
from pool_service.models import OneCImportBatch, OneCMonthlyProfit, OneCReportPeriodState


ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
ITEM = "33333333-3333-3333-3333-333333333333"
CUSTOMER = "44444444-4444-4444-4444-444444444444"
RECORDER = "55555555-5555-5555-5555-555555555555"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
BASE_URL = "https://fresh.example/odata/standard.odata/"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class FakeOpener:
    def __init__(self, *payloads, error=None):
        self.payloads = list(payloads)
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.payloads.pop(0))


def row(
    line=1, *, organization=ORG_A, active=True, period="2026-05-15T10:00:00Z",
    quantity="1", revenue="10.25", vat="1.25", cost="4.25", customer=CUSTOMER,
):
    return {
        "Recorder": RECORDER,
        "LineNumber": line,
        "Period": period,
        "Active": active,
        "Организация_Key": organization,
        "Номенклатура_Key": ITEM,
        "Контрагент_Key": customer,
        "Количество": quantity,
        "Сумма": revenue,
        "СуммаНДС": vat,
        "Себестоимость": cost,
    }


def config(**overrides):
    values = {
        "base_url": BASE_URL,
        "organization_guids": (ORG_A,),
        "timeout_seconds": 7,
        "max_pages": 10,
    }
    values.update(overrides)
    return ODataConfig(**values)


class ODataProfitReaderTests(SimpleTestCase):
    def preview(self, payloads, **kwargs):
        opener = FakeOpener(*payloads)
        result = read_profit_preview(
            kwargs.pop("config", config()), "2026-05", "2026-05",
            kwargs.pop("organizations", None), opener=opener,
        )
        return result, opener

    def test_decimal_json_number_inactive_zero_customer_and_zero_cost(self):
        raw = (
            '{"value":['
            '{"Recorder":"%s","LineNumber":1,"Period":"2026-05-15T00:00:00Z",'
            '"Active":true,"Организация_Key":"%s","Номенклатура_Key":"%s",'
            '"Контрагент_Key":"%s","Количество":1.125,"Сумма":10.25,'
            '"СуммаНДС":1.25,"Себестоимость":0},'
            '{"Recorder":"%s","LineNumber":2,"Period":"2026-05-15T00:00:00Z",'
            '"Active":false}]}'
        ) % (RECORDER, ORG_A, ITEM, ZERO_GUID, RECORDER)
        result, _ = self.preview([raw.encode()])
        self.assertEqual(result["total"]["row_count"], 1)
        self.assertEqual(result["total"]["quantity"], Decimal("1.125"))
        self.assertEqual(result["total"]["revenue"], Decimal("10.25"))
        self.assertEqual(result["total"]["cost"], Decimal("0"))
        self.assertEqual(result["total"]["gross_profit"], Decimal("10.25"))

    def test_843_may_rows_control_totals_and_get_query_allowlist(self):
        rows = [row(line=index) for index in range(843)]
        result, opener = self.preview([{"value": rows}])
        self.assertEqual(result["total"]["row_count"], 843)
        self.assertEqual(result["total"]["quantity"], Decimal("843"))
        self.assertEqual(result["total"]["revenue"], Decimal("8640.75"))
        self.assertEqual(result["total"]["vat"], Decimal("1053.75"))
        self.assertEqual(result["total"]["cost"], Decimal("3582.75"))
        self.assertEqual(result["total"]["gross_profit"], Decimal("5058.00"))
        request = opener.requests[0][0]
        self.assertEqual(request.get_method(), "GET")
        query = parse_qs(urlsplit(request.full_url).query)
        filter_value = query["$filter"][0]
        self.assertIn(ORG_A, filter_value)
        self.assertIn("2026-05-01", filter_value)
        self.assertIn("2026-06-01", filter_value)
        self.assertNotIn(ORG_B, filter_value)
        self.assertIn("AccumulationRegister_Продажи_RecordType", unquote(request.full_url))

    def test_multiple_requested_organizations_are_filtered_and_aggregated(self):
        cfg = config(organization_guids=(ORG_A, ORG_B))
        result, opener = self.preview(
            [{"value": [row(1), row(2, organization=ORG_B, revenue="20", cost="5")]}],
            config=cfg, organizations=[ORG_A, ORG_B],
        )
        self.assertEqual(set(result["organizations"]), {ORG_A, ORG_B})
        self.assertEqual(result["organizations"][ORG_B]["gross_profit"], Decimal("15"))
        filter_value = parse_qs(urlsplit(opener.requests[0][0].full_url).query)["$filter"][0]
        self.assertIn(ORG_A, filter_value)
        self.assertIn(ORG_B, filter_value)

    def test_modern_and_legacy_pagination(self):
        second = BASE_URL + "AccumulationRegister_x?$skiptoken=2"
        third = BASE_URL + "AccumulationRegister_x?$skiptoken=3"
        result, opener = self.preview([
            {"value": [row(1)], "@odata.nextLink": second},
            {"value": [row(2)], "odata.nextLink": third},
            {"d": {"results": [row(3)]}},
        ])
        self.assertEqual(result["total"]["row_count"], 3)
        self.assertEqual(result["total"]["page_count"], 3)
        self.assertEqual(len(opener.requests), 3)

    def test_legacy_d_next(self):
        next_url = BASE_URL + "AccumulationRegister_x?$skiptoken=2"
        result, _ = self.preview([
            {"d": {"results": [row(1)], "__next": next_url}},
            {"d": {"results": [row(2)]}},
        ])
        self.assertEqual(result["total"]["row_count"], 2)

    def test_pagination_loop_and_page_limit_are_rejected(self):
        next_url = BASE_URL + "AccumulationRegister_x?$skiptoken=2"
        with self.assertRaisesRegex(ODataPreviewError, "loop"):
            self.preview([
                {"value": [row(1)], "@odata.nextLink": next_url},
                {"value": [row(2)], "@odata.nextLink": next_url},
            ])
        with self.assertRaisesRegex(ODataPreviewError, "page limit"):
            self.preview(
                [{"value": [row(1)], "@odata.nextLink": next_url}],
                config=config(max_pages=1),
            )

    def test_external_and_encoded_traversal_next_links_are_rejected(self):
        malicious = (
            "https://evil.example/odata/standard.odata/x",
            BASE_URL + "%2e%2e/private",
            BASE_URL + "%252e%252e/private",
            BASE_URL + "../private",
            "https://fresh.example:444/odata/standard.odata/x",
        )
        for next_url in malicious:
            with self.subTest(next_url=next_url):
                with self.assertRaises(ODataPreviewError):
                    self.preview([{"value": [row(1)], "@odata.nextLink": next_url}])

    def test_redirect_is_error_without_second_request(self):
        error = HTTPError(BASE_URL, 302, "Found", {"Location": BASE_URL + "other"}, None)
        opener = FakeOpener(error=error)
        with self.assertRaisesRegex(ODataPreviewError, "HTTP error 302"):
            read_profit_preview(config(), "2026-05", "2026-05", opener=opener)
        self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(NoRedirectHandler().redirect_request(
            mock.Mock(), mock.Mock(), 302, "Found", {}, BASE_URL + "other"
        ))

    def test_default_opener_installs_no_redirect_handler(self):
        opener = FakeOpener({"value": []})
        with mock.patch(
            "pool_service.finance_imports.odata_profit.build_opener", return_value=opener
        ) as builder:
            read_profit_preview(config(), "2026-05", "2026-05")
        self.assertIsInstance(builder.call_args.args[0], NoRedirectHandler)

    def test_invalid_base_urls_and_partial_credentials(self):
        invalid_urls = (
            "", "ftp://fresh.example/odata/standard.odata/",
            "https://user:pass@fresh.example/odata/standard.odata/",
            "https://fresh.example/odata/standard.odata/?x=1",
            "https://fresh.example/odata/standard.odata/#fragment",
            "https://fresh.example/odata/other/",
            "https://fresh.example/odata/%2e%2e/odata/standard.odata/",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url), self.assertRaises(ODataPreviewError):
                validate_config(config(base_url=base_url))
        for username, password in (("user", ""), ("", "pass")):
            with self.subTest(username=username), self.assertRaisesRegex(
                ODataPreviewError, "set together"
            ):
                validate_config(config(username=username, password=password))

    def test_out_of_range_and_non_allowlisted_response_rows_are_rejected(self):
        with self.assertRaisesRegex(ODataPreviewError, "outside the requested month"):
            self.preview([{"value": [row(period="2026-06-01T00:00:00Z")]}])
        with self.assertRaisesRegex(ODataPreviewError, "outside the allowlist"):
            self.preview([{"value": [row(organization=ORG_B)]}])

    def test_requested_guid_must_be_allowlisted(self):
        with self.assertRaisesRegex(ODataPreviewError, "configured allowlist"):
            self.preview([{"value": []}], organizations=[ORG_B])


@override_settings(
    ONEC_ODATA_BASE_URL=BASE_URL,
    ONEC_ODATA_USERNAME="",
    ONEC_ODATA_PASSWORD="",
    ONEC_ODATA_ORGANIZATION_GUIDS=(ORG_A,),
    ONEC_ODATA_TIMEOUT_SECONDS="5",
    ONEC_ODATA_MAX_PAGES="2",
)
class ODataProfitCommandTests(TestCase):
    def test_command_outputs_aggregates_only_and_does_not_write_database(self):
        result = {
            "total": {
                "row_count": 1, "page_count": 1, "quantity": Decimal("1"),
                "revenue": Decimal("10"), "vat": Decimal("2"),
                "cost": Decimal("4"), "gross_profit": Decimal("6"),
            },
            "organizations": {},
        }
        before = (
            OneCImportBatch.objects.count(), OneCMonthlyProfit.objects.count(),
            OneCReportPeriodState.objects.count(),
        )
        stdout = StringIO()
        with mock.patch(
            "pool_service.management.commands.onec_odata_profit_preview.read_profit_preview",
            return_value=result,
        ) as reader:
            call_command(
                "onec_odata_profit_preview", start_month="2026-05", end_month="2026-05",
                organization_guids=[ORG_A], stdout=stdout,
            )
        self.assertEqual(before, (
            OneCImportBatch.objects.count(), OneCMonthlyProfit.objects.count(),
            OneCReportPeriodState.objects.count(),
        ))
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["total"]["gross_profit"], "6")
        self.assertNotIn(BASE_URL, stdout.getvalue())
        self.assertEqual(reader.call_args.args[1:3], ("2026-05", "2026-05"))

    @override_settings(ONEC_ODATA_BASE_URL="bad")
    def test_configuration_error_becomes_command_error(self):
        with self.assertRaises(CommandError):
            call_command(
                "onec_odata_profit_preview", start_month="2026-05", end_month="2026-05"
            )
