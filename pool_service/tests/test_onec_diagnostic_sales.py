import json
from urllib.parse import unquote

from django.test import SimpleTestCase

from pool_service import onec_diagnostic as diagnostic
from pool_service.finance_imports.odata_profit import ODataConfig


ORG = "10000000-0000-4000-8000-000000000001"
SAND = "20000000-0000-4000-8000-000000000002"
SAND_2 = "30000000-0000-4000-8000-000000000003"

METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices>
<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="S">
<EntityType Name="N"><Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Code" Type="Edm.String"/><Property Name="Description" Type="Edm.String"/><Property Name="DeletionMark" Type="Edm.Boolean"/></EntityType>
<EntityType Name="P"><Property Name="Period" Type="Edm.DateTime"/><Property Name="Active" Type="Edm.Boolean"/><Property Name="Номенклатура_Key" Type="Edm.Guid"/><Property Name="Организация_Key" Type="Edm.Guid"/><Property Name="Количество" Type="Edm.Decimal"/><Property Name="Сумма" Type="Edm.Decimal"/></EntityType>
<EntityContainer Name="C"><EntitySet Name="Catalog_Номенклатура" EntityType="S.N"/><EntitySet Name="AccumulationRegister_Продажи_RecordType" EntityType="S.P"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>""".encode()


class Response:
    status = 200
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return json.dumps(self.payload).encode()


class Opener:
    def __init__(self, payloads): self.payloads = list(payloads); self.urls = []
    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        return Response(self.payloads.pop(0))


def catalog(*items, next_link=None):
    value = [{"Ref_Key": ref, "Code": code, "Description": description, "DeletionMark": deleted}
             for ref, code, description, deleted in items]
    payload = {"value": value}
    if next_link: payload["@odata.nextLink"] = next_link
    return payload


def sale(ref, period, quantity, amount, organization=ORG, active=True):
    return {"Period": period, "Active": active, "Номенклатура_Key": ref,
            "Организация_Key": organization, "Количество": quantity, "Сумма": amount}


class OneCDiagnosticSalesTests(SimpleTestCase):
    def config(self):
        return ODataConfig(base_url="https://one.test/odata/standard.odata/", username="u", password="p",
                           organization_guids=(ORG,), max_pages=5, max_rows=50)

    def call(self, opener, query="sand"):
        return diagnostic.get_nomenclature_sales(
            self.config(), query=query, start_date="2025-08-01", end_date="2025-08-31",
            opener=opener, metadata_raw=METADATA,
        )

    def test_escaping_boundaries_negative_movements_and_pagination(self):
        next_url = "https://one.test/odata/standard.odata/sales?page=2"
        opener = Opener([
            catalog((SAND, "1", "Builder's sand", False)),
            {"value": [sale(SAND, "2025-08-01T00:00:00", "10", "100")], "@odata.nextLink": next_url},
            {"value": [sale(SAND, "2025-08-31T23:59:59", "-2", "-20")]},
        ])
        result = self.call(opener, "Builder's")
        self.assertTrue(result["complete"])
        self.assertEqual(result["quantity_net"], "8")
        self.assertEqual(result["amount_net"], "80")
        self.assertEqual(result["row_count"], 2)
        self.assertIn("Builder''s", unquote(opener.urls[0]))
        sales_url = unquote(opener.urls[1])
        self.assertIn("Active", sales_url.split("&")[0])
        self.assertIn("Active eq true", sales_url)
        self.assertIn("Period lt datetime'2025-09-01T00:00:00'", sales_url)

    def test_inactive_sales_movement_fails_closed(self):
        opener = Opener([
            catalog((SAND, "1", "Sand", False)),
            {"value": [sale(SAND, "2025-08-10T00:00:00", "100", "1000", active=False)]},
        ])
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "INACTIVE_SALES_MOVEMENT"):
            self.call(opener)

    def test_deleted_items_are_excluded_and_multiple_matches_have_no_total(self):
        opener = Opener([
            catalog((SAND, "1", "Sand", False), (SAND_2, "2", "Fine sand", False),
                    ("40000000-0000-4000-8000-000000000004", "3", "Deleted", True)),
            {"value": [sale(SAND, "2025-08-10T00:00:00", "1", "10"),
                       sale(SAND_2, "2025-08-11T00:00:00", "2", "20")]},
        ])
        result = self.call(opener)
        self.assertEqual(len(result["matches"]), 2)
        self.assertNotIn("quantity_net", result)

    def test_returned_org_and_dates_are_revalidated(self):
        for bad_row, error in (
            (sale(SAND, "2025-08-02T00:00:00", "1", "1", SAND_2), "ORGANIZATION_SCOPE_VIOLATION"),
            (sale(SAND, "2025-09-01T00:00:00", "1", "1"), "SALES_DATE_SCOPE_VIOLATION"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(diagnostic.OneCDiagnosticError, error):
                self.call(Opener([catalog((SAND, "1", "Sand", False)), {"value": [bad_row]}]))

    def test_unprovable_pagination_fails_closed(self):
        opener = Opener([catalog((SAND, "1", "Sand", False))] + [
            {"value": [], "@odata.nextLink": f"https://one.test/odata/standard.odata/page/{number + 1}"}
            for number in range(diagnostic.MAX_SALES_PAGES)
        ])
        with self.assertRaisesRegex(diagnostic.OneCDiagnosticError, "ODATA_PAGINATION_LIMIT"):
            self.call(opener)
