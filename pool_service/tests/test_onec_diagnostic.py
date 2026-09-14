import json
from unittest import TestCase
from urllib.parse import parse_qs, unquote, urlsplit

from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service import onec_diagnostic as diagnostic


ORG = "10000000-0000-4000-8000-000000000001"
DOC = "20000000-0000-4000-8000-000000000002"

METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
 <edmx:DataServices>
  <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="StandardODATA">
   <EntityType Name="Sale">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Number" Type="Edm.String" />
    <Property Name="Date" Type="Edm.DateTime" />
    <Property Name="Организация_Key" Type="Edm.Guid" />
    <Property Name="СчетФактураВыставлен" Type="Edm.Boolean" />
    <Property Name="Комментарий" Type="Edm.String" />
    <Property Name="SecretToken" Type="Edm.String" />
    <Property Name="Оклад" Type="Edm.String" />
   </EntityType>
   <EntityType Name="CatalogRow">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Description" Type="Edm.String" />
   </EntityType>
   <EntityType Name="EmployeePayroll">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Организация_Key" Type="Edm.Guid" />
    <Property Name="Сотрудник_Key" Type="Edm.Guid" />
    <Property Name="Начислено" Type="Edm.String" />
   </EntityType>
   <EntityContainer Name="Container">
    <EntitySet Name="Document_РасходнаяНакладная" EntityType="StandardODATA.Sale" />
    <EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.CatalogRow" />
    <EntitySet Name="InformationRegister_НачисленияСотрудников" EntityType="StandardODATA.EmployeePayroll" />
   </EntityContainer>
  </Schema>
 </edmx:DataServices>
</edmx:Edmx>""".encode()


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        raw = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return raw[:limit] if limit >= 0 else raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        assert request.method == "GET"
        parts = urlsplit(request.full_url)
        assert parts.scheme == "https"
        assert parts.netloc == "example.test"
        if parts.path.endswith("/$metadata"):
            return FakeResponse(METADATA)
        entity = unquote(parts.path.rsplit("/", 1)[-1])
        if entity != "Document_РасходнаяНакладная":
            raise AssertionError(entity)
        query = parse_qs(parts.query)
        selected = unquote(query["$select"][0]).split(",")
        assert "Организация_Key" in selected
        expression = unquote(query["$filter"][0])
        assert "Организация_Key eq guid'" + ORG + "'" in expression
        assert "Number eq 'РТ-000001'" in expression
        return FakeResponse({"value": [{
            "Number": "РТ-000001",
            "СчетФактураВыставлен": True,
            "Организация_Key": ORG,
        }]})


class OneCDiagnosticTests(TestCase):
    def config(self):
        return ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="reader",
            password="secret",
            organization_guids=(ORG,),
            timeout_seconds=5,
            max_pages=10,
            max_rows=50,
        )

    def test_describe_metadata_lists_business_entities_and_fields(self):
        result = diagnostic.describe_metadata(
            self.config(), query="счетфактура", metadata_raw=METADATA
        )
        self.assertEqual(result["entity_count"], 1)
        entity = result["entities"][0]
        self.assertEqual(entity["name"], "Document_РасходнаяНакладная")
        self.assertIn("СчетФактураВыставлен", entity["fields"])
        self.assertTrue(entity["row_access_allowed"])

    def test_entity_schema_marks_sensitive_fields(self):
        result = diagnostic.get_entity_schema(
            self.config(), "Document_РасходнаяНакладная", metadata_raw=METADATA
        )
        sensitivity = {item["name"]: item["sensitive"] for item in result["fields"]}
        self.assertTrue(sensitivity["SecretToken"])
        self.assertTrue(sensitivity["Оклад"])
        self.assertFalse(sensitivity["Number"])

    def test_read_entity_rows_enforces_organization_scope_and_structured_filter(self):
        opener = FakeOpener()
        result = diagnostic.read_entity_rows(
            self.config(),
            "Document_РасходнаяНакладная",
            fields=["Number", "СчетФактураВыставлен"],
            filters=[{"field": "Number", "op": "eq", "value": "РТ-000001"}],
            top=5,
            opener=opener,
            metadata_raw=METADATA,
        )
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"], [{
            "Number": "РТ-000001",
            "СчетФактураВыставлен": True,
        }])
        self.assertTrue(result["organization_scope_enforced"])
        self.assertTrue(result["personal_compensation_data_denied"])
        self.assertEqual(result["page_byte_limit"], diagnostic.MAX_ROW_PAGE_BYTES)
        self.assertTrue(all(request.method == "GET" for request in opener.requests))

    def test_read_rejects_entity_without_organization_scope(self):
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "ORGANIZATION_SCOPE_UNAVAILABLE"):
            diagnostic.read_entity_rows(
                self.config(),
                "Catalog_Номенклатура",
                fields=["Description"],
                filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
                metadata_raw=METADATA,
            )

    def test_read_rejects_credential_and_compensation_fields(self):
        for field in ("SecretToken", "Оклад"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "SENSITIVE_FIELD_DENIED"):
                    diagnostic.read_entity_rows(
                        self.config(),
                        "Document_РасходнаяНакладная",
                        fields=[field],
                        filters=[{"field": "Number", "op": "eq", "value": "РТ-000001"}],
                        metadata_raw=METADATA,
                    )

    def test_read_rejects_personnel_or_payroll_entity(self):
        schema = diagnostic.get_entity_schema(
            self.config(),
            "InformationRegister_НачисленияСотрудников",
            metadata_raw=METADATA,
        )
        self.assertFalse(schema["row_access_allowed"])
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "RESTRICTED_DATA_ENTITY"):
            diagnostic.read_entity_rows(
                self.config(),
                "InformationRegister_НачисленияСотрудников",
                fields=["Ref_Key"],
                filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
                metadata_raw=METADATA,
            )

    def test_read_requires_selective_filter(self):
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "SELECTIVE_FILTER_REQUIRED"):
            diagnostic.read_entity_rows(
                self.config(),
                "Document_РасходнаяНакладная",
                fields=["Number"],
                filters=[{"field": "СчетФактураВыставлен", "op": "eq", "value": True}],
                metadata_raw=METADATA,
            )

    def test_row_response_has_per_page_byte_limit(self):
        class OversizeOpener(FakeOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                payload = b"x" * (diagnostic.MAX_ROW_PAGE_BYTES + 1)
                return FakeResponse(payload)

        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "ODATA_PAGE_SIZE_LIMIT"):
            diagnostic.read_entity_rows(
                self.config(),
                "Document_РасходнаяНакладная",
                fields=["Number"],
                filters=[{"field": "Number", "op": "eq", "value": "РТ-000001"}],
                opener=OversizeOpener(),
                metadata_raw=METADATA,
            )

    def test_http_is_not_allowed_for_diagnostic_reads(self):
        config = ODataConfig(
            base_url="http://example.test/odata/standard.odata/",
            username="reader",
            password="secret",
            organization_guids=(ORG,),
        )
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "DIAGNOSTIC_ODATA_REQUIRES_HTTPS"):
            diagnostic.describe_metadata(config, metadata_raw=METADATA)
