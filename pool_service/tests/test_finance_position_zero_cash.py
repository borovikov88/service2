from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import unquote

from django.test import SimpleTestCase

from pool_service.finance_imports.odata_finance_position import (
    CATALOG_KKM,
    DOCUMENT_CASH_WITHDRAWAL,
    IN_TRANSIT_REGISTER,
    ODataConfig,
    ZERO_GUID,
    read_finance_position,
)

ORG = "11111111-1111-1111-1111-111111111111"
DOC = "22222222-2222-2222-2222-222222222222"


class FinancePositionZeroCashReferenceTests(SimpleTestCase):
    def test_zero_cash_in_transit_uses_transfer_document(self):
        config = ODataConfig(
            "https://example.test/odata/standard.odata/",
            "u", "p", (ORG,), 5, 20, 100,
        )
        requested_urls = []

        def pages(_config, url, opener=None):
            decoded = unquote(url)
            requested_urls.append(decoded)

            if IN_TRANSIT_REGISTER in decoded and "Balance(" in decoded:
                yield ([{
                    "Организация_Key": ORG,
                    "Касса": ZERO_GUID,
                    "Касса_Type": f"StandardODATA.{CATALOG_KKM}",
                    "ДокументПередачи": DOC,
                    "ДокументПередачи_Type":
                        f"StandardODATA.{DOCUMENT_CASH_WITHDRAWAL}",
                    "СуммаBalance": "6626",
                    "СуммаВалBalance": "6626",
                }], 1)

            elif (
                DOCUMENT_CASH_WITHDRAWAL in decoded
                and "Balance(" not in decoded
            ):
                yield ([{
                    "Ref_Key": DOC,
                    "Number": "123",
                    "Date": "2026-09-12T06:00:00",
                    "DeletionMark": False,
                }], 1)

            else:
                yield ([], 1)

        with patch(
            "pool_service.finance_imports.odata_finance_position.read_odata_pages",
            side_effect=pages,
        ):
            result = read_finance_position(
                config,
                now=datetime(2026, 9, 12, 23, 0, tzinfo=timezone.utc),
                opener=object(),
            )

        self.assertEqual(len(result.cash_rows), 1)
        row = result.cash_rows[0]

        self.assertEqual(row.source_kind, "in_transit")
        self.assertEqual(row.account_guid, ZERO_GUID)
        self.assertEqual(row.amount, Decimal("6626.00"))
        self.assertEqual(
            row.display_name,
            "Выемка №123 от 2026-09-12",
        )
        self.assertEqual(
            row.transfer_document_display,
            "Выемка №123 от 2026-09-12",
        )

        # Нулевой GUID не должен запрашиваться как реальная ККМ.
        self.assertFalse(any(
            CATALOG_KKM in url and "Balance(" not in url
            for url in requested_urls
        ))
