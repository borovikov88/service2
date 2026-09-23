from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlsplit

from django.test import SimpleTestCase

from pool_service.finance_imports.odata_direct_order_costs import (
    RECORDER_TYPE_ODATA,
    read_direct_order_expense_rows,
)
from pool_service.finance_imports.odata_profit import ODataPreviewError
from pool_service.tests.test_onec_odata_profit_preview import (
    FakeOpener,
    ORG_A,
    ORG_B,
    BASE_URL,
    config,
)


RECEIPT = "77777777-7777-4777-8777-777777777777"
ORDER = "88888888-8888-4888-8888-888888888888"
ACCOUNT = "99999999-9999-4999-8999-999999999999"
OPERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def expense_row(
    line=1,
    *,
    amount="25000.00",
    organization=ORG_A,
    active=True,
    order=ORDER,
    recorder_type=RECORDER_TYPE_ODATA,
):
    return {
        "Recorder": RECEIPT,
        "Recorder_Type": recorder_type,
        "LineNumber": line,
        "Period": "2026-05-15T10:00:00Z",
        "Active": active,
        "Организация_Key": organization,
        "ЗаказПокупателя_Key": order,
        "СодержаниеПроводки": "Прочие расходы",
        "СуммаРасходов": amount,
        "СчетУчета_Key": ACCOUNT,
        "ХозяйственнаяОперация_Key": OPERATION,
    }


class DirectOrderExpenseReaderTests(SimpleTestCase):
    def read(self, rows, *, cfg=None):
        opener = FakeOpener({"value": rows})
        result = read_direct_order_expense_rows(
            cfg or config(), "2026-05", "2026-05", opener=opener
        )
        return result, opener

    def test_reads_only_bounded_direct_receipt_costs_and_preserves_negative_sign(self):
        (rows, pages), opener = self.read([
            expense_row(1, amount="25000.00"),
            expense_row(2, amount="-5000.00"),
            expense_row(3, active=False),
            expense_row(4, amount="0"),
            expense_row(5, order=ZERO_GUID),
        ])

        self.assertEqual(pages, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].amount, Decimal("25000.00"))
        self.assertEqual(rows[1].amount, Decimal("-5000.00"))
        self.assertEqual(rows[0].order_guid, ORDER)

        request = opener.requests[0][0]
        self.assertEqual(request.get_method(), "GET")
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertIn("ЗаказПокупателя_Key", query["$select"][0])
        self.assertIn("СуммаРасходов", query["$select"][0])
        filter_value = query["$filter"][0]
        self.assertIn("Active eq true", filter_value)
        self.assertIn("ЗаказПокупателя_Key ne guid", filter_value)
        self.assertIn("СуммаРасходов ne 0", filter_value)
        self.assertIn(RECORDER_TYPE_ODATA, filter_value)
        self.assertIn(
            "AccumulationRegister_ДоходыИРасходы_RecordType",
            unquote(request.full_url),
        )

    def test_zero_order_is_ignored_even_if_1c_returns_it(self):
        (rows, _), _ = self.read([expense_row(order=ZERO_GUID)])
        self.assertEqual(rows, [])

    def test_wrong_recorder_type_fails_closed(self):
        with self.assertRaises(ODataPreviewError):
            self.read([
                expense_row(
                    recorder_type="StandardODATA.Document_РасходнаяНакладная"
                )
            ])

    def test_foreign_organization_fails_closed(self):
        with self.assertRaises(ODataPreviewError):
            self.read([expense_row(organization=ORG_B)])

    def test_duplicate_identity_fails_closed(self):
        with self.assertRaises(ODataPreviewError):
            self.read([expense_row(1), expense_row(1)])
