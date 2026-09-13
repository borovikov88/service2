from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import unquote

from django.test import SimpleTestCase

from pool_service.finance_imports.odata_finance_position import (
    CATALOG_COUNTERPARTIES,
    CATALOG_KKM,
    CUSTOMER_REGISTER,
    DOCUMENT_CASH_WITHDRAWAL,
    FinancePositionReadError,
    IN_TRANSIT_REGISTER,
    KKM_REGISTER,
    ODataConfig,
    ZERO_GUID,
    read_finance_position,
)

ORG = "11111111-1111-1111-1111-111111111111"
REF = "22222222-2222-2222-2222-222222222222"


def config():
    return ODataConfig(
        "https://example.test/odata/standard.odata/",
        "u", "p", (ORG,), 5, 20, 100,
    )


def read_with(pages):
    with patch(
        "pool_service.finance_imports.odata_finance_position.read_odata_pages",
        side_effect=pages,
    ):
        return read_finance_position(
            config(),
            now=datetime(2026, 9, 13, 1, 30, tzinfo=timezone.utc),
            opener=object(),
        )


class DeletedReferencePolicyTests(SimpleTestCase):
    def test_deleted_kkm_is_excluded_from_cash(self):
        def pages(_config, url, opener=None):
            decoded = unquote(url)
            if KKM_REGISTER in decoded and "Balance(" in decoded:
                yield ([{
                    "Организация_Key": ORG,
                    "КассаККМ_Key": REF,
                    "СуммаBalance": "40700",
                    "СуммаВалBalance": "40700",
                }], 1)
            elif CATALOG_KKM in decoded and "Balance(" not in decoded:
                yield ([{
                    "Ref_Key": REF,
                    "Description": "Касса № 1",
                    "DeletionMark": True,
                }], 1)
            else:
                yield ([], 1)

        result = read_with(pages)

        self.assertEqual(result.cash_rows, ())
        deleted = result.diagnostics["deleted_reference_exclusions"]
        self.assertEqual(deleted["object_count"], 1)
        self.assertEqual(deleted["row_count"], 1)
        self.assertEqual(deleted["cash_row_count"], 1)
        self.assertEqual(deleted["cash_net_amount"], "40700.00")
        self.assertEqual(
            deleted["by_reference_type"][CATALOG_KKM]["absolute_amount"],
            "40700.00",
        )

    def test_deleted_counterparty_is_excluded_from_settlements(self):
        def pages(_config, url, opener=None):
            decoded = unquote(url)
            if CUSTOMER_REGISTER in decoded and "Balance(" in decoded:
                yield ([{
                    "Организация_Key": ORG,
                    "ТипРасчетов": "Долг",
                    "Контрагент_Key": REF,
                    "Договор_Key": ZERO_GUID,
                    "Документ": ZERO_GUID,
                    "Документ_Type": "",
                    "Заказ": ZERO_GUID,
                    "Заказ_Type": "",
                    "СуммаBalance": "100",
                    "СуммаВалBalance": "0",
                    "СуммаРегBalance": "0",
                }], 1)
            elif CATALOG_COUNTERPARTIES in decoded:
                yield ([{
                    "Ref_Key": REF,
                    "Description": "Удалённый контрагент",
                    "DeletionMark": True,
                }], 1)
            else:
                yield ([], 1)

        result = read_with(pages)

        self.assertEqual(result.settlement_rows, ())
        deleted = result.diagnostics["deleted_reference_exclusions"]
        self.assertEqual(deleted["settlement_row_count"], 1)
        self.assertEqual(deleted["settlement_absolute_amount"], "100.00")

    def test_deleted_transfer_document_excludes_in_transit_row(self):
        def pages(_config, url, opener=None):
            decoded = unquote(url)
            if IN_TRANSIT_REGISTER in decoded and "Balance(" in decoded:
                yield ([{
                    "Организация_Key": ORG,
                    "Касса": ZERO_GUID,
                    "Касса_Type": f"StandardODATA.{CATALOG_KKM}",
                    "ДокументПередачи": REF,
                    "ДокументПередачи_Type":
                        f"StandardODATA.{DOCUMENT_CASH_WITHDRAWAL}",
                    "СуммаBalance": "6626",
                    "СуммаВалBalance": "6626",
                }], 1)
            elif DOCUMENT_CASH_WITHDRAWAL in decoded and "Balance(" not in decoded:
                yield ([{
                    "Ref_Key": REF,
                    "Number": "123",
                    "Date": "2026-09-13T01:00:00",
                    "DeletionMark": True,
                }], 1)
            else:
                yield ([], 1)

        result = read_with(pages)

        self.assertEqual(result.cash_rows, ())
        deleted = result.diagnostics["deleted_reference_exclusions"]
        self.assertEqual(deleted["cash_absolute_amount"], "6626.00")
        self.assertIn(
            DOCUMENT_CASH_WITHDRAWAL,
            deleted["by_reference_type"],
        )

    def test_non_boolean_deletion_mark_still_fails_closed(self):
        def pages(_config, url, opener=None):
            decoded = unquote(url)
            if KKM_REGISTER in decoded and "Balance(" in decoded:
                yield ([{
                    "Организация_Key": ORG,
                    "КассаККМ_Key": REF,
                    "СуммаBalance": "1",
                    "СуммаВалBalance": "1",
                }], 1)
            elif CATALOG_KKM in decoded and "Balance(" not in decoded:
                yield ([{
                    "Ref_Key": REF,
                    "Description": "Касса",
                    "DeletionMark": "false",
                }], 1)
            else:
                yield ([], 1)

        with self.assertRaisesRegex(
            FinancePositionReadError,
            "deletion mark",
        ):
            read_with(pages)
