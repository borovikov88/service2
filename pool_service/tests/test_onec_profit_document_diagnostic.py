import json
from unittest import TestCase
from urllib.parse import parse_qs, unquote, urlsplit

from scripts import inspect_onec_profit_documents as diagnostic


ORG = "10000000-0000-4000-8000-000000000001"
ORDER = "20000000-0000-4000-8000-000000000002"
SALE = "30000000-0000-4000-8000-000000000003"
OTHER_ORDER = "40000000-0000-4000-8000-000000000004"


METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
 <edmx:DataServices>
  <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="StandardODATA">
   <EntityType Name="SalesRow">
    <Property Name="Recorder" Type="Edm.String" />
    <Property Name="Recorder_Type" Type="Edm.String" />
    <Property Name="LineNumber" Type="Edm.Int64" />
    <Property Name="Period" Type="Edm.DateTime" />
    <Property Name="Active" Type="Edm.Boolean" />
    <Property Name="Организация_Key" Type="Edm.Guid" />
    <Property Name="Документ" Type="Edm.String" />
    <Property Name="Документ_Type" Type="Edm.String" />
    <Property Name="Сумма" Type="Edm.Double" />
    <Property Name="Себестоимость" Type="Edm.Double" />
   </EntityType>
   <EntityType Name="Order">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Number" Type="Edm.String" />
    <Property Name="Date" Type="Edm.DateTime" />
    <Property Name="Контрагент_Key" Type="Edm.Guid" />
   </EntityType>
   <EntityType Name="Sale">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Number" Type="Edm.String" />
    <Property Name="Date" Type="Edm.DateTime" />
    <Property Name="ЗаказПокупателя_Key" Type="Edm.Guid" />
    <Property Name="ОтчетОРозничныхПродажах" Type="Edm.String" />
    <Property Name="ОтчетОРозничныхПродажах_Type" Type="Edm.String" />
    <Property Name="ЧекККМ_Key" Type="Edm.Guid" />
    <Property Name="Ответственный_Key" Type="Edm.Guid" />
   </EntityType>
   <EntityContainer Name="Container">
    <EntitySet Name="AccumulationRegister_Продажи_RecordType" EntityType="StandardODATA.SalesRow" />
    <EntitySet Name="Document_ЗаказПокупателя" EntityType="StandardODATA.Order" />
    <EntitySet Name="Document_РеализацияТоваров" EntityType="StandardODATA.Sale" />
   </EntityContainer>
  </Schema>
 </edmx:DataServices>
</edmx:Edmx>""".encode()


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, limit=-1):
        return self.payload[:limit] if limit >= 0 else self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProfitDiagnosticOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self._assert_safe_get(request)
        entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
        if entity == diagnostic.ENTITY_SET:
            return FakeResponse({"value": [
                {
                    "Recorder": ORDER,
                    "Recorder_Type": "StandardODATA.Document_ЗаказПокупателя",
                    "LineNumber": 1,
                    "Period": "2026-05-04T09:00:00",
                    "Active": True,
                    "Организация_Key": ORG,
                    "Документ": ORDER,
                    "Документ_Type": "StandardODATA.Document_ЗаказПокупателя",
                    "Сумма": "98765.43",
                    "Себестоимость": "0",
                },
                {
                    "Recorder": SALE,
                    "Recorder_Type": "StandardODATA.Document_РеализацияТоваров",
                    "LineNumber": 1,
                    "Period": "2026-05-05T10:00:00",
                    "Active": True,
                    "Организация_Key": ORG,
                    "Документ": SALE,
                    "Документ_Type": "StandardODATA.Document_РеализацияТоваров",
                    "Сумма": "0",
                    "Себестоимость": "54321.09",
                },
            ]})
        if entity == "Document_ЗаказПокупателя":
            return FakeResponse({"value": [{
                "Ref_Key": ORDER,
                "Number": "ЗП-000001",
                "Date": "2026-05-04T09:00:00",
            }]})
        if entity == "Document_РеализацияТоваров":
            query = parse_qs(urlsplit(request.full_url).query)
            selected = unquote(query["$select"][0]).split(",")
            if "Контрагент_Key" in selected or "Ответственный_Key" in selected:
                raise AssertionError("unrelated person/customer fields must not be requested")
            self.assert_document_fields(selected)
            return FakeResponse({"value": [{
                "Ref_Key": SALE,
                "Number": "РТ-000002",
                "Date": "2026-05-05T10:00:00",
                "ЗаказПокупателя_Key": ORDER,
            }]})
        raise AssertionError(f"unexpected entity: {entity}")

    @staticmethod
    def _assert_safe_get(request):
        parts = urlsplit(request.full_url)
        assert request.method == "GET"
        assert parts.scheme == "https"
        assert parts.netloc == "example.test"
        assert parts.path.startswith(diagnostic.BASE_PATH)

    @staticmethod
    def assert_document_fields(selected):
        assert {
            "Ref_Key", "Number", "Date", "ЗаказПокупателя_Key",
            "ОтчетОРозничныхПродажах", "ОтчетОРозничныхПродажах_Type", "ЧекККМ_Key",
        }.issubset(selected)


class ProfitDocumentDiagnosticTests(TestCase):
    def config(self):
        return {
            "ONEC_ODATA_BASE_URL": "https://example.test/odata/standard.odata/",
            "ONEC_ODATA_USERNAME": "reader",
            "ONEC_ODATA_PASSWORD": "secret",
            "ONEC_ODATA_ORGANIZATION_GUIDS": ORG,
        }

    def test_reports_document_display_and_anonymous_join_without_sensitive_values(self):
        opener = ProfitDiagnosticOpener()
        result = diagnostic.inspect(
            self.config(), month="2026-05", opener=opener, metadata_raw=METADATA
        )

        self.assertEqual(result["register_fields"]["Recorder_Type"], "Edm.String")
        self.assertEqual(result["register_fields"]["Документ_Type"], "Edm.String")
        self.assertEqual(
            {(item["type"], item["number"], item["date"]) for item in result["documents"]},
            {
                ("Document_ЗаказПокупателя", "ЗП-000001", "2026-05-04"),
                ("Document_РеализацияТоваров", "РТ-000002", "2026-05-05"),
            },
        )
        self.assertEqual(len(result["join_candidates"]), 1)
        candidate = result["join_candidates"][0]
        self.assertEqual(candidate["key"]["type"], "Document_ЗаказПокупателя")
        self.assertEqual(
            candidate["paths"],
            ["Document_РеализацияТоваров.ЗаказПокупателя_Key", "register.Recorder", "register.Документ"],
        )
        self.assertEqual(candidate["revenue_row_count"], 1)
        self.assertEqual(candidate["cost_row_count"], 1)

        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in (ORG, ORDER, SALE, "98765.43", "54321.09", "reader", "secret"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("Контрагент", serialized)
        self.assertNotIn("Ответственный", serialized)
        self.assertNotIn("revenue\": \"", serialized)
        self.assertTrue(all(request.method == "GET" for request in opener.requests))

    def test_rejects_cross_origin_pagination_before_second_request(self):
        class CrossOriginOpener(ProfitDiagnosticOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                return FakeResponse({"value": [], "@odata.nextLink": "https://other.test/data"})

        opener = CrossOriginOpener()
        with self.assertRaisesRegex(diagnostic.ProbeError, "UNSAFE_ODATA_URL"):
            diagnostic.inspect(
                self.config(), month="2026-05", opener=opener, metadata_raw=METADATA
            )
        self.assertEqual(len(opener.requests), 1)

    def test_missing_primary_document_is_reported_without_stopping_available_analysis(self):
        class MissingPrimaryOpener(ProfitDiagnosticOpener):
            def open(self, request, timeout=None):
                entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
                if entity == "Document_ЗаказПокупателя":
                    self.requests.append(request)
                    self._assert_safe_get(request)
                    return FakeResponse({"value": []})
                return super().open(request, timeout=timeout)

        opener = MissingPrimaryOpener()
        result = diagnostic.inspect(
            self.config(), month="2026-05", opener=opener, metadata_raw=METADATA
        )

        self.assertEqual(result["missing_documents"], [{
            "type": "Document_ЗаказПокупателя", "count": 1,
        }])
        self.assertEqual(
            [item["type"] for item in result["documents"]],
            ["Document_РеализацияТоваров"],
        )
        self.assertFalse(result["join_candidates"][0]["key"]["resolved"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(ORDER, serialized)
        self.assertNotIn(SALE, serialized)
        order_requests = [
            request for request in opener.requests
            if unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
            == "Document_ЗаказПокупателя"
        ]
        self.assertEqual(len(order_requests), 1)

    def test_missing_linked_document_is_reported_without_raw_lookup(self):
        class MissingLinkedOpener(ProfitDiagnosticOpener):
            def open(self, request, timeout=None):
                entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
                if entity == "Document_ЗаказПокупателя" and OTHER_ORDER in unquote(request.full_url):
                    self.requests.append(request)
                    self._assert_safe_get(request)
                    return FakeResponse({"value": []})
                response = super().open(request, timeout=timeout)
                if entity == "Document_РеализацияТоваров":
                    payload = json.loads(response.payload)
                    payload["value"][0]["ЗаказПокупателя_Key"] = OTHER_ORDER
                    return FakeResponse(payload)
                return response

        result = diagnostic.inspect(
            self.config(), month="2026-05", opener=MissingLinkedOpener(), metadata_raw=METADATA
        )

        self.assertEqual(result["missing_documents"], [{
            "type": "Document_ЗаказПокупателя", "count": 1,
        }])
        self.assertEqual(len(result["documents"]), 2)
        self.assertTrue(all(item["resolved"] for item in result["documents"]))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(OTHER_ORDER, serialized)
        self.assertNotIn(ORDER, serialized)
        self.assertNotIn(SALE, serialized)

    def test_rejects_recorder_type_not_published_as_document_entity(self):
        class UnsupportedTypeOpener(ProfitDiagnosticOpener):
            def open(self, request, timeout=None):
                response = super().open(request, timeout=timeout)
                entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
                if entity == diagnostic.ENTITY_SET:
                    payload = json.loads(response.payload)
                    payload["value"][0]["Recorder_Type"] = "StandardODATA.Catalog_Контрагенты"
                    return FakeResponse(payload)
                return response

        with self.assertRaisesRegex(diagnostic.ProbeError, "UNSUPPORTED_RECORDER_TYPE"):
            diagnostic.inspect(
                self.config(), month="2026-05", opener=UnsupportedTypeOpener(), metadata_raw=METADATA
            )

    def test_safe_url_requires_same_origin_and_odata_path(self):
        base = self.config()["ONEC_ODATA_BASE_URL"]
        with self.assertRaisesRegex(diagnostic.ProbeError, "UNSAFE_ODATA_URL"):
            diagnostic._safe_url(base, base, "https://other.test/odata/standard.odata/data")
        with self.assertRaisesRegex(diagnostic.ProbeError, "UNSAFE_ODATA_URL"):
            diagnostic._safe_url(base, base, "https://example.test/private/data")
        with self.assertRaisesRegex(diagnostic.ProbeError, "UNSAFE_ODATA_URL"):
            diagnostic._safe_url(base, base, "%2e%2e/private/data")
