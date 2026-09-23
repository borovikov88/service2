import json
from urllib.parse import unquote, urlsplit

from django.test import TestCase

from pool_service import onec_diagnostic as base
from pool_service import onec_diagnostic_universal as universal
from pool_service.finance_imports.odata_profit import ODataConfig


ORG = "10000000-0000-4000-8000-000000000001"
OTHER_ORG = "20000000-0000-4000-8000-000000000002"
ITEM = "30000000-0000-4000-8000-000000000003"

METADATA = '''<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices>
    <Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="Test">
      <EntityType Name="CatalogItem">
        <Property Name="Ref_Key" Type="Edm.Guid" />
        <Property Name="Description" Type="Edm.String" />
        <Property Name="DeletionMark" Type="Edm.Boolean" />
      </EntityType>
      <EntityType Name="RegisterRow">
        <Property Name="Period" Type="Edm.DateTime" />
        <Property Name="Active" Type="Edm.Boolean" />
        <Property Name="Nomenclature_Key" Type="Edm.Guid" />
        <Property Name="Organisation_Key" Type="Edm.Guid" />
        <Property Name="Amount" Type="Edm.Double" />
      </EntityType>
      <EntityType Name="ScopedRegisterRow">
        <Property Name="Period" Type="Edm.DateTime" />
        <Property Name="Active" Type="Edm.Boolean" />
        <Property Name="Nomenclature_Key" Type="Edm.Guid" />
        <Property Name="Организация_Key" Type="Edm.Guid" />
        <Property Name="Amount" Type="Edm.Double" />
      </EntityType>
      <EntityContainer Name="Container">
        <EntitySet Name="Catalog_Test" EntityType="Test.CatalogItem" />
        <EntitySet Name="Document_Test_Items" EntityType="Test.RegisterRow" />
        <EntitySet Name="AccumulationRegister_Test" EntityType="Test.ScopedRegisterRow" />
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>'''.encode('utf-8')


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _limit):
        return self.payload


class RecordingOpener:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        return FakeResponse(self.payload)


class UniversalDiagnosticPolicyTests(TestCase):
    def config(self):
        return ODataConfig(
            base_url="https://1c.example.test/odata/standard.odata/",
            username="reader",
            password="secret",
            organization_guids=(ORG,),
            timeout_seconds=5,
            max_pages=5,
            max_rows=50,
        )

    def test_universal_config_preserves_its_own_page_budget(self):
        with self.settings(
            ONEC_ODATA_BASE_URL="https://1c.example.test/odata/standard.odata/",
            ONEC_ODATA_USERNAME="reader",
            ONEC_ODATA_PASSWORD="secret",
            ONEC_ODATA_ORGANIZATION_GUIDS=(ORG,),
            ONEC_ODATA_TIMEOUT_SECONDS=5,
            ONEC_ODATA_MAX_PAGES=100,
            ONEC_ODATA_MAX_ROWS=50,
        ):
            legacy = base.config_from_settings()
            query = universal.config_from_settings()

        self.assertEqual(legacy.max_pages, base.MAX_READ_PAGES)
        self.assertEqual(query.max_pages, universal.MAX_QUERY_PAGES)

    def test_structured_contains_escapes_apostrophe_and_in_sets_key_anchor(self):
        fields = {"Description": "Edm.String", "Ref_Key": "Edm.Guid"}
        clauses, anchor = universal._normalize_filters(
            [
                {"field": "Description", "op": "contains", "value": "O'Reilly"},
                {"field": "Ref_Key", "op": "in", "value": [ITEM]},
            ],
            fields,
            entity_set="Catalog_Test",
        )
        self.assertIn("O''Reilly", clauses[0])
        self.assertTrue(anchor)

    def test_order_by_is_metadata_validated_and_bounded(self):
        fields = {"Period": "Edm.DateTime", "Amount": "Edm.Double"}
        self.assertEqual(
            universal._normalize_order_by(
                [
                    {"field": "Period", "direction": "desc"},
                    {"field": "Amount", "direction": "asc"},
                ],
                fields,
                entity_set="AccumulationRegister_Test",
            ),
            ["Period desc", "Amount asc"],
        )
        with self.assertRaises(base.OneCDiagnosticError) as error:
            universal._normalize_order_by(
                [{"field": "Period", "direction": "sideways"}],
                fields,
                entity_set="AccumulationRegister_Test",
            )
        self.assertEqual(error.exception.code, "INVALID_ORDER_DIRECTION")

    def test_unscoped_catalog_search_allows_contains_and_excludes_deleted(self):
        opener = RecordingOpener({
            "value": [
                {"Ref_Key": ITEM, "Description": "Test item", "DeletionMark": False}
            ]
        })
        result = universal.query_1c_rows(
            self.config(),
            "Catalog_Test",
            fields=["Ref_Key", "Description"],
            filters=[{"field": "Description", "op": "contains", "value": "Test"}],
            order_by=[{"field": "Description", "direction": "asc"}],
            opener=opener,
            metadata_raw=METADATA,
        )
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["organization_scope_enforced"])
        self.assertTrue(result["deleted_rows_excluded"])
        decoded = unquote(urlsplit(opener.urls[0]).query)
        self.assertIn("DeletionMark eq false", decoded)
        self.assertIn("$orderby=Description asc", decoded)

    def test_unscoped_non_catalog_requires_key_anchor(self):
        with self.assertRaises(base.OneCDiagnosticError) as error:
            universal.query_1c_rows(
                self.config(),
                "Document_Test_Items",
                fields=["Period"],
                filters=[{"field": "Period", "op": "ge", "value": "2026-01-01T00:00:00"}],
                metadata_raw=METADATA,
            )
        self.assertEqual(error.exception.code, "KEY_ANCHOR_REQUIRED")

    def test_scoped_register_injects_org_active_and_revalidates_rows(self):
        opener = RecordingOpener({
            "value": [{
                "Period": "2026-09-01T10:00:00",
                "Active": True,
                "Nomenclature_Key": ITEM,
                "Организация_Key": ORG,
                "Amount": 10.5,
            }]
        })
        result = universal.query_1c_rows(
            self.config(),
            "AccumulationRegister_Test",
            fields=["Period", "Amount"],
            filters=[{"field": "Nomenclature_Key", "op": "eq", "value": ITEM}],
            order_by=[{"field": "Period", "direction": "desc"}],
            opener=opener,
            metadata_raw=METADATA,
        )
        self.assertTrue(result["organization_scope_enforced"])
        self.assertTrue(result["inactive_rows_excluded"])
        self.assertEqual(result["rows"], [{"Period": "2026-09-01T10:00:00", "Amount": "10.5"}])
        decoded = unquote(urlsplit(opener.urls[0]).query)
        self.assertIn("Организация_Key eq guid", decoded)
        self.assertIn("Active eq true", decoded)

        violating = RecordingOpener({
            "value": [{
                "Period": "2026-09-01T10:00:00",
                "Active": True,
                "Nomenclature_Key": ITEM,
                "Организация_Key": OTHER_ORG,
                "Amount": 10.5,
            }]
        })
        with self.assertRaises(base.OneCDiagnosticError) as error:
            universal.query_1c_rows(
                self.config(),
                "AccumulationRegister_Test",
                fields=["Amount"],
                filters=[{"field": "Nomenclature_Key", "op": "eq", "value": ITEM}],
                opener=violating,
                metadata_raw=METADATA,
            )
        self.assertEqual(error.exception.code, "ORGANIZATION_SCOPE_VIOLATION")


    def test_limit_reports_truncation_and_fetches_only_one_extra_row(self):
        second = "40000000-0000-4000-8000-000000000004"
        opener = RecordingOpener({
            "value": [
                {"Ref_Key": ITEM, "Description": "First", "DeletionMark": False},
                {"Ref_Key": second, "Description": "Second", "DeletionMark": False},
            ]
        })
        result = universal.query_1c_rows(
            self.config(),
            "Catalog_Test",
            fields=["Ref_Key", "Description"],
            filters=[{"field": "Description", "op": "contains", "value": "i"}],
            limit=1,
            opener=opener,
            metadata_raw=METADATA,
        )
        self.assertEqual(result["row_count"], 1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        decoded = unquote(urlsplit(opener.urls[0]).query)
        self.assertIn("$top=2", decoded)

    def test_include_deleted_explicitly_disables_default_hygiene_filter(self):
        opener = RecordingOpener({
            "value": [
                {"Ref_Key": ITEM, "Description": "Deleted item", "DeletionMark": True}
            ]
        })
        result = universal.query_1c_rows(
            self.config(),
            "Catalog_Test",
            fields=["Ref_Key", "Description", "DeletionMark"],
            filters=[{"field": "Ref_Key", "op": "eq", "value": ITEM}],
            include_deleted=True,
            opener=opener,
            metadata_raw=METADATA,
        )
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["deleted_rows_excluded"])
        decoded = unquote(urlsplit(opener.urls[0]).query)
        self.assertNotIn("DeletionMark eq false", decoded)

    def test_in_filter_is_bounded_and_counts_as_key_anchor(self):
        fields = {"Ref_Key": "Edm.Guid"}
        clauses, anchor = universal._normalize_filters(
            [{"field": "Ref_Key", "op": "in", "value": [ITEM, OTHER_ORG]}],
            fields,
            entity_set="Document_Test_Items",
        )
        self.assertTrue(anchor)
        self.assertIn("Ref_Key eq guid", clauses[0])

        for value in ([], [ITEM] * (universal.MAX_IN_VALUES + 1)):
            with self.subTest(size=len(value)):
                with self.assertRaises(base.OneCDiagnosticError) as error:
                    universal._normalize_filters(
                        [{"field": "Ref_Key", "op": "in", "value": value}],
                        fields,
                        entity_set="Document_Test_Items",
                    )
                self.assertEqual(error.exception.code, "INVALID_FILTER_VALUE")

    def test_order_by_rejects_unknown_field(self):
        with self.assertRaises(base.OneCDiagnosticError) as error:
            universal._normalize_order_by(
                [{"field": "NotPublished", "direction": "asc"}],
                {"Period": "Edm.DateTime"},
                entity_set="AccumulationRegister_Test",
            )
        self.assertEqual(error.exception.code, "FIELD_NOT_PUBLISHED")
