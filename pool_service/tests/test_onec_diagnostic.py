import json
from urllib.parse import parse_qs, unquote, urlsplit

from django.contrib.auth.models import AnonymousUser, User
from django.test import TestCase

from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service.models import Organization, OrganizationAccess
from pool_service import onec_diagnostic as diagnostic


ORG = "10000000-0000-4000-8000-000000000001"
DOC = "20000000-0000-4000-8000-000000000002"

METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
 <edmx:DataServices>
  <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="StandardODATA">
   <ComplexType Name="PrivateContact">
    <Property Name="Паспорт" Type="Edm.String" />
    <Property Name="РасчетныйСчет" Type="Edm.String" />
   </ComplexType>
   <EntityType Name="Sale">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Number" Type="Edm.String" />
    <Property Name="Date" Type="Edm.DateTime" />
    <Property Name="Организация_Key" Type="Edm.Guid" />
    <Property Name="СчетФактураВыставлен" Type="Edm.Boolean" />
    <Property Name="Комментарий" Type="Edm.String" />
    <Property Name="SecretToken" Type="Edm.String" />
   </EntityType>
   <EntityType Name="CatalogRow">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Description" Type="Edm.String" />
   </EntityType>
   <EntityType Name="EmployeePayroll">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Организация_Key" Type="Edm.Guid" />
    <Property Name="Сотрудник_Key" Type="Edm.Guid" />
    <Property Name="ФИО" Type="Edm.String" />
    <Property Name="Оклад" Type="Edm.String" />
    <Property Name="Начислено" Type="Edm.String" />
    <Property Name="Удержано" Type="Edm.String" />
    <Property Name="НДФЛ" Type="Edm.String" />
    <Property Name="ПаспортСерияНомер" Type="Edm.String" />
    <Property Name="СНИЛС" Type="Edm.String" />
    <Property Name="BankAccountNumber" Type="Edm.String" />
    <Property Name="CertificatePrivateKey" Type="Edm.String" />
    <Property Name="ЛичныйТелефон" Type="Edm.String" />
    <Property Name="DateOfBirth" Type="Edm.DateTime" />
    <Property Name="Адрес" Type="Edm.String" />
    <Property Name="Почта" Type="Edm.String" />
    <Property Name="ЭлектроннаяПочта" Type="Edm.String" />
    <Property Name="Телефон" Type="Edm.String" />
    <Property Name="МобильныйТелефон" Type="Edm.String" />
    <Property Name="БанковскиеРеквизиты" Type="Edm.String" />
    <Property Name="РасчетныйСчет" Type="Edm.String" />
    <Property Name="РасчётныйСчёт" Type="Edm.String" />
    <Property Name="ЛицевойСчет" Type="Edm.String" />
    <Property Name="НомерКарты" Type="Edm.String" />
    <Property Name="IBAN" Type="Edm.String" />
    <Property Name="КонтактнаяИнформация" Type="StandardODATA.PrivateContact" />
    <Property Name="КоллекцияКонтактов" Type="Collection(Edm.String)" />
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
        query = parse_qs(parts.query)
        selected = unquote(query["$select"][0]).split(",")
        assert "Организация_Key" in selected
        expression = unquote(query["$filter"][0])
        assert "Организация_Key eq guid'" + ORG + "'" in expression
        if entity == "Document_РасходнаяНакладная":
            assert "Number eq 'РТ-000001'" in expression
            return FakeResponse({"value": [{
                "Number": "РТ-000001",
                "СчетФактураВыставлен": True,
                "Комментарий": "ok",
                "Организация_Key": ORG,
            }]})
        if entity == "InformationRegister_НачисленияСотрудников":
            assert "Ref_Key eq guid'" + DOC + "'" in expression
            return FakeResponse({"value": [{
                "Ref_Key": DOC,
                "Сотрудник_Key": DOC,
                "ФИО": "Тестовый сотрудник",
                "Оклад": "80000",
                "Начислено": "85000",
                "Удержано": "11050",
                "НДФЛ": "11050",
                "Организация_Key": ORG,
            }]})
        raise AssertionError(entity)


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

    def test_payroll_entity_is_readable_and_salary_fields_are_not_sensitive(self):
        result = diagnostic.get_entity_schema(
            self.config(),
            "InformationRegister_НачисленияСотрудников",
            metadata_raw=METADATA,
        )
        self.assertTrue(result["row_access_allowed"])
        sensitivity = {item["name"]: item["sensitive"] for item in result["fields"]}
        for field in ("Сотрудник_Key", "ФИО", "Оклад", "Начислено", "Удержано", "НДФЛ"):
            with self.subTest(field=field):
                self.assertFalse(sensitivity[field])

    def test_schema_marks_private_personnel_fields(self):
        sale = diagnostic.get_entity_schema(
            self.config(), "Document_РасходнаяНакладная", metadata_raw=METADATA
        )
        sale_sensitivity = {item["name"]: item["sensitive"] for item in sale["fields"]}
        self.assertTrue(sale_sensitivity["SecretToken"])
        self.assertFalse(sale_sensitivity["Number"])

        payroll = diagnostic.get_entity_schema(
            self.config(),
            "InformationRegister_НачисленияСотрудников",
            metadata_raw=METADATA,
        )
        sensitivity = {item["name"]: item["sensitive"] for item in payroll["fields"]}
        for field in (
            "ПаспортСерияНомер",
            "СНИЛС",
            "BankAccountNumber",
            "CertificatePrivateKey",
            "ЛичныйТелефон",
            "DateOfBirth",
            "Адрес",
            "Почта",
            "ЭлектроннаяПочта",
            "Телефон",
            "МобильныйТелефон",
            "БанковскиеРеквизиты",
            "РасчетныйСчет",
            "РасчётныйСчёт",
            "ЛицевойСчет",
            "НомерКарты",
            "IBAN",
        ):
            with self.subTest(field=field):
                self.assertTrue(sensitivity[field])

    def test_schema_marks_complex_and_collection_fields_non_scalar(self):
        payroll = diagnostic.get_entity_schema(
            self.config(),
            "InformationRegister_НачисленияСотрудников",
            metadata_raw=METADATA,
        )
        readable = {item["name"]: item["readable_scalar"] for item in payroll["fields"]}
        self.assertFalse(readable["КонтактнаяИнформация"])
        self.assertFalse(readable["КоллекцияКонтактов"])
        self.assertTrue(readable["ФИО"])

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
        self.assertTrue(result["sensitive_personal_security_fields_denied"])
        self.assertTrue(result["non_primitive_fields_denied"])
        self.assertEqual(result["page_byte_limit"], diagnostic.MAX_ROW_PAGE_BYTES)
        self.assertTrue(all(request.method == "GET" for request in opener.requests))

    def test_read_allows_payroll_and_personnel_business_data(self):
        opener = FakeOpener()
        result = diagnostic.read_entity_rows(
            self.config(),
            "InformationRegister_НачисленияСотрудников",
            fields=["Сотрудник_Key", "ФИО", "Оклад", "Начислено", "Удержано", "НДФЛ"],
            filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
            top=5,
            opener=opener,
            metadata_raw=METADATA,
        )
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["ФИО"], "Тестовый сотрудник")
        self.assertEqual(result["rows"][0]["Оклад"], "80000")
        self.assertEqual(result["rows"][0]["Начислено"], "85000")

    def test_read_rejects_entity_without_organization_scope(self):
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "ORGANIZATION_SCOPE_UNAVAILABLE"):
            diagnostic.read_entity_rows(
                self.config(),
                "Catalog_Номенклатура",
                fields=["Description"],
                filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
                metadata_raw=METADATA,
            )

    def test_read_rejects_secret_and_private_personnel_fields_in_select(self):
        cases = [
            ("Document_РасходнаяНакладная", "SecretToken", "Number", "РТ-000001"),
            ("InformationRegister_НачисленияСотрудников", "ПаспортСерияНомер", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "СНИЛС", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "BankAccountNumber", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "CertificatePrivateKey", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "ЛичныйТелефон", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "DateOfBirth", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "Адрес", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "Почта", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "БанковскиеРеквизиты", "Ref_Key", DOC),
            ("InformationRegister_НачисленияСотрудников", "РасчетныйСчет", "Ref_Key", DOC),
        ]
        for entity, field, filter_field, value in cases:
            with self.subTest(entity=entity, field=field):
                with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "SENSITIVE_FIELD_DENIED"):
                    diagnostic.read_entity_rows(
                        self.config(),
                        entity,
                        fields=[field],
                        filters=[{"field": filter_field, "op": "eq", "value": value}],
                        metadata_raw=METADATA,
                    )

    def test_read_rejects_private_personnel_fields_in_filters(self):
        for field in ("Адрес", "Почта", "БанковскиеРеквизиты", "РасчетныйСчет", "IBAN"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "SENSITIVE_FIELD_DENIED"):
                    diagnostic.read_entity_rows(
                        self.config(),
                        "InformationRegister_НачисленияСотрудников",
                        fields=["ФИО"],
                        filters=[{"field": field, "op": "eq", "value": "private"}],
                        metadata_raw=METADATA,
                    )

    def test_read_rejects_complex_and_collection_fields_before_request(self):
        class NoNetworkOpener:
            def open(self, request, timeout=None):
                raise AssertionError("network request must not be constructed")

        for field in ("КонтактнаяИнформация", "КоллекцияКонтактов"):
            with self.subTest(select=field):
                with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "NON_PRIMITIVE_FIELD_DENIED"):
                    diagnostic.read_entity_rows(
                        self.config(),
                        "InformationRegister_НачисленияСотрудников",
                        fields=[field],
                        filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
                        opener=NoNetworkOpener(),
                        metadata_raw=METADATA,
                    )
            with self.subTest(filter=field):
                with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "NON_PRIMITIVE_FIELD_DENIED"):
                    diagnostic.read_entity_rows(
                        self.config(),
                        "InformationRegister_НачисленияСотрудников",
                        fields=["ФИО"],
                        filters=[{"field": field, "op": "eq", "value": "probe"}],
                        opener=NoNetworkOpener(),
                        metadata_raw=METADATA,
                    )

    def test_nested_runtime_value_cannot_escape_scalar_contract(self):
        class NestedValueOpener(FakeOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                return FakeResponse({"value": [{
                    "Комментарий": {"Паспорт": "secret"},
                    "Организация_Key": ORG,
                }]})

        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "NON_PRIMITIVE_FIELD_DENIED"):
            diagnostic.read_entity_rows(
                self.config(),
                "Document_РасходнаяНакладная",
                fields=["Комментарий"],
                filters=[{"field": "Number", "op": "eq", "value": "РТ-000001"}],
                opener=NestedValueOpener(),
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

    def test_diagnostic_access_uses_target_management_roles_and_superuser(self):
        target = Organization.objects.create(name="Diagnostic Target Org")
        other = Organization.objects.create(name="Diagnostic Other Org")
        owner = User.objects.create_user(username="diag-owner")
        accountant = User.objects.create_user(username="diag-accountant")
        admin = User.objects.create_user(username="diag-admin")
        manager = User.objects.create_user(username="diag-manager")
        service = User.objects.create_user(username="diag-service")
        installer = User.objects.create_user(username="diag-installer")
        other_owner = User.objects.create_user(username="diag-other-owner")
        outsider = User.objects.create_user(username="diag-outsider")
        superuser = User.objects.create_superuser(username="diag-super", password="pass")
        OrganizationAccess.objects.create(user=owner, organization=target, role="owner")
        OrganizationAccess.objects.create(user=accountant, organization=target, role="accountant")
        OrganizationAccess.objects.create(user=admin, organization=target, role="admin")
        OrganizationAccess.objects.create(user=manager, organization=target, role="manager")
        OrganizationAccess.objects.create(user=service, organization=target, role="service")
        OrganizationAccess.objects.create(user=installer, organization=target, role="installer")
        OrganizationAccess.objects.create(user=other_owner, organization=other, role="owner")

        with self.settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=target.pk):
            self.assertTrue(diagnostic.can_access_diagnostic_mcp(owner, target))
            self.assertTrue(diagnostic.can_access_diagnostic_mcp(accountant, target))
            self.assertTrue(diagnostic.can_access_diagnostic_mcp(admin, target))
            self.assertTrue(diagnostic.can_access_diagnostic_mcp(superuser, target))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(manager, target))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(service, target))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(installer, target))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(outsider, target))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(other_owner, other))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(owner, other))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(superuser, other))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(AnonymousUser(), target))
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(owner, None))

    def test_diagnostic_access_fails_closed_without_valid_target_setting(self):
        organization = Organization.objects.create(name="Diagnostic Fail Closed Org")
        owner = User.objects.create_user(username="diag-fail-owner")
        OrganizationAccess.objects.create(user=owner, organization=organization, role="owner")

        for value in (None, "", "bad", 0, -1, True):
            with self.subTest(value=value):
                with self.settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=value):
                    self.assertFalse(diagnostic.can_access_diagnostic_mcp(owner, organization))
