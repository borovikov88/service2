from django.contrib.auth.models import User
from django.test import TestCase

from pool_service import onec_diagnostic as diagnostic
from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service.models import Organization, OrganizationAccess


ORG = "10000000-0000-4000-8000-000000000001"
DOC = "20000000-0000-4000-8000-000000000002"

METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
 <edmx:DataServices>
  <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="StandardODATA">
   <EntityType Name="EmployeePayroll">
    <Property Name="Ref_Key" Type="Edm.Guid" />
    <Property Name="Организация_Key" Type="Edm.Guid" />
    <Property Name="ФИО" Type="Edm.String" />
    <Property Name="Employee_SSN" Type="Edm.String" />
    <Property Name="SSN_Number" Type="Edm.String" />
    <Property Name="EmployeeSSN" Type="Edm.String" />
    <Property Name="Amount" Type="Edm.Decimal" />
   </EntityType>
   <EntityContainer Name="Container">
    <EntitySet Name="InformationRegister_НачисленияСотрудников" EntityType="StandardODATA.EmployeePayroll" />
   </EntityContainer>
  </Schema>
 </edmx:DataServices>
</edmx:Edmx>""".encode()


class OneCDiagnosticReviewFollowupTests(TestCase):
    def config(self, *, max_rows=50):
        return ODataConfig(
            base_url="https://example.test/odata/standard.odata/",
            username="reader",
            password="secret",
            organization_guids=(ORG,),
            timeout_seconds=5,
            max_pages=5,
            max_rows=max_rows,
        )

    def test_normalized_ssn_variants_are_sensitive(self):
        schema = diagnostic.get_entity_schema(
            self.config(),
            "InformationRegister_НачисленияСотрудников",
            metadata_raw=METADATA,
        )
        sensitivity = {item["name"]: item["sensitive"] for item in schema["fields"]}
        for field in ("Employee_SSN", "SSN_Number", "EmployeeSSN"):
            with self.subTest(field=field):
                self.assertTrue(sensitivity[field])
                with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "SENSITIVE_FIELD_DENIED"):
                    diagnostic.read_entity_rows(
                        self.config(),
                        "InformationRegister_НачисленияСотрудников",
                        fields=[field],
                        filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
                        metadata_raw=METADATA,
                    )

    def test_numeric_filter_rejects_extreme_exponent_before_request(self):
        class NoNetworkOpener:
            def open(self, request, timeout=None):
                raise AssertionError("network request must not be attempted")

        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "INVALID_FILTER_VALUE"):
            diagnostic.read_entity_rows(
                self.config(),
                "InformationRegister_НачисленияСотрудников",
                fields=["ФИО"],
                filters=[
                    {"field": "Ref_Key", "op": "eq", "value": DOC},
                    {"field": "Amount", "op": "gt", "value": "1e100000000"},
                ],
                opener=NoNetworkOpener(),
                metadata_raw=METADATA,
            )

    def test_configured_max_rows_is_authoritative(self):
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "ROW_LIMIT_OUT_OF_RANGE"):
            diagnostic.read_entity_rows(
                self.config(max_rows=3),
                "InformationRegister_НачисленияСотрудников",
                fields=["ФИО"],
                filters=[{"field": "Ref_Key", "op": "eq", "value": DOC}],
                top=4,
                metadata_raw=METADATA,
            )

    def test_inactive_owner_is_denied(self):
        organization = Organization.objects.create(name="Diagnostic Inactive Target")
        owner = User.objects.create_user(username="diag-inactive-owner", is_active=False)
        OrganizationAccess.objects.create(user=owner, organization=organization, role="owner")

        with self.settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=organization.pk,
            ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_USER_IDS=(owner.pk,),
        ):
            self.assertFalse(diagnostic.can_access_diagnostic_mcp(owner, organization))
