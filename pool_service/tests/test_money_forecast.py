from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone as dj_timezone

from pool_service.finance_imports.money_forecast import (
    _bucket_forecast,
    _deduplicated_forecast,
    _match_order_receivables,
    _service_plan_items,
)
from pool_service.finance_imports.odata_money_forecast import (
    COUNTERPARTIES,
    CUSTOMER_ORDER,
    CUSTOMER_PREPAYMENT,
    CUSTOMER_SCHEDULE,
    CUSTOMER_STATES,
    REALIZATION,
    AGREEMENTS,
    SUPPLIER_ORDER,
    SUPPLIER_SCHEDULE,
    MoneyForecastSourceRow,
    MoneyForecastSourceSnapshot,
    read_money_forecast,
)
from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service.finance_imports.finance_position import _persist_snapshot
from pool_service.finance_imports.odata_finance_position import (
    FinancePositionSourceSnapshot,
    SettlementPositionSourceRow,
)
from pool_service.money_models import (
    ManagementMoneyPlan,
    OneCMoneyForecastRow,
    OneCMoneyForecastSnapshot,
)
from pool_service.models import Organization


ORG_GUID = "11111111-1111-1111-1111-111111111111"
ORDER_GUID = "fa80360c-7124-11f1-89a3-fa163e1420a5"
PARTY_GUID = "22222222-2222-2222-2222-222222222222"
AGREEMENT_GUID = "33333333-3333-3333-3333-333333333333"
REALIZATION_GUID = "44444444-4444-4444-4444-444444444444"
STATE_WORK = "b0987b32-c07f-11ef-9fc3-fa163e1420a5"
STATE_INVOICE = "b09a10e6-c07f-11ef-9fc3-fa163e1420a5"


def _config():
    return ODataConfig(
        "https://example.test/odata/standard.odata/",
        "user",
        "password",
        (ORG_GUID,),
        5,
        100,
        10000,
    )


def _customer_order(*, state=STATE_WORK, due="0001-01-01T00:00:00", cancelled=False):
    return {
        "Ref_Key": ORDER_GUID,
        "Date": "2026-07-01T13:05:01",
        "DeletionMark": False,
        "Posted": True,
        "Number": "НФНФ-000094",
        "Организация_Key": ORG_GUID,
        "Контрагент_Key": PARTY_GUID,
        "Договор_Key": "00000000-0000-0000-0000-000000000000",
        "Ответственный_Key": "00000000-0000-0000-0000-000000000000",
        "СостояниеЗаказа": state,
        "СостояниеЗаказа_Type": "StandardODATA.Catalog_СостоянияЗаказовПокупателей",
        "ДатаИзменения": "2026-09-30T10:00:00",
        "ДатаОтгрузки": "2026-10-31T00:00:00",
        "ЗапланироватьОплату": False,
        "ОплатаДо": due,
        "СуммаДокумента": "243695.00",
        "ПричинаОтмены_Key": (
            "44444444-4444-4444-4444-444444444444"
            if cancelled else "00000000-0000-0000-0000-000000000000"
        ),
    }


def _reader_side_effect(
    *, schedule=None, prepayment=None, state=STATE_WORK, cancelled=False,
    realizations=None, agreements=None,
):
    schedule = list(schedule or [])
    prepayment = list(prepayment or [])
    realizations = list(realizations or [])
    agreements = list(agreements or [])

    def read(_config_value, entity, _fields, **_kwargs):
        if entity == CUSTOMER_ORDER:
            return [_customer_order(state=state, cancelled=cancelled)]
        if entity == SUPPLIER_ORDER:
            return []
        if entity == CUSTOMER_SCHEDULE:
            return schedule
        if entity == CUSTOMER_PREPAYMENT:
            return prepayment
        if entity == SUPPLIER_SCHEDULE:
            return []
        if entity == REALIZATION:
            return realizations
        if entity == AGREEMENTS:
            return agreements
        return []

    return read


def _lookups(_config_value, entity, guids, _opener):
    if entity == COUNTERPARTIES:
        return {PARTY_GUID: "СОШ №134 МАОУ"}
    if entity == CUSTOMER_STATES:
        return {
            STATE_WORK: "В работе",
            STATE_INVOICE: "Выставлен счет",
        }
    return {}


class MoneyForecastReaderTests(TestCase):
    def test_realization_uses_contract_payment_term_without_inventing_payment(self):
        realization = {
            "Ref_Key": REALIZATION_GUID,
            "Date": "2026-09-28T15:03:50",
            "DeletionMark": False,
            "Posted": True,
            "Number": "НФНФ-000349",
            "Организация_Key": ORG_GUID,
            "Контрагент_Key": PARTY_GUID,
            "Договор_Key": AGREEMENT_GUID,
            "Заказ": "00000000-0000-0000-0000-000000000000",
            "Заказ_Type": "StandardODATA.Document_ЗаказПокупателя",
            "СуммаДокумента": "28086.00",
            "Ответственный_Key": "00000000-0000-0000-0000-000000000000",
        }
        agreement = {
            "Ref_Key": AGREEMENT_GUID,
            "Description": "Основной договор",
            "DeletionMark": False,
            "Недействителен": False,
            "СрокОплатыПокупателя": "30",
        }
        with (
            patch(
                "pool_service.finance_imports.odata_money_forecast._read",
                side_effect=_reader_side_effect(
                    realizations=[realization], agreements=[agreement]
                ),
            ),
            patch(
                "pool_service.finance_imports.odata_money_forecast._lookup_descriptions",
                side_effect=_lookups,
            ),
        ):
            result = read_money_forecast(
                datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc),
                config=_config(),
                opener=object(),
            )
        rows = [row for row in result.rows if row.item_kind == "realization_due"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.document_guid, REALIZATION_GUID)
        self.assertEqual(row.expected_amount, Decimal("28086.00"))
        self.assertEqual(row.expected_date, date(2026, 10, 28))
        self.assertIsNone(row.remaining_amount)
        self.assertEqual(row.confirmation_status, "review")
        self.assertEqual(row.payment_match_status, "receivable_match_required")

    def test_control_order_without_schedule_or_due_is_not_assumed_unpaid(self):
        with (
            patch(
                "pool_service.finance_imports.odata_money_forecast._read",
                side_effect=_reader_side_effect(),
            ),
            patch(
                "pool_service.finance_imports.odata_money_forecast._lookup_descriptions",
                side_effect=_lookups,
            ),
        ):
            result = read_money_forecast(
                datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
                config=_config(),
                opener=object(),
            )
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(row.order_number, "НФНФ-000094")
        self.assertEqual(row.order_state, "В работе")
        self.assertIsNone(row.expected_amount)
        self.assertIsNone(row.remaining_amount)
        self.assertIsNone(row.expected_date)
        self.assertEqual(row.confirmation_status, "review")
        self.assertEqual(row.source_payload["document_amount"], "243695.00")

    def test_single_schedule_subtracts_linked_prepayment_once(self):
        schedule = [{
            "Ref_Key": ORDER_GUID,
            "LineNumber": 1,
            "ДатаОплаты": "2026-10-31T00:00:00",
            "СуммаОплаты": "243695.00",
            "ПроцентОплаты": "100.00",
        }]
        prepayment = [{
            "Ref_Key": ORDER_GUID,
            "LineNumber": 1,
            "Документ": str(uuid.uuid4()),
            "Документ_Type": "StandardODATA.Document_ПоступлениеНаСчет",
            "СуммаПлатежа": "100000.00",
            "СуммаРасчетов": "100000.00",
            "ЭтоПредоплатаБезЗаказа": False,
        }]
        with (
            patch(
                "pool_service.finance_imports.odata_money_forecast._read",
                side_effect=_reader_side_effect(schedule=schedule, prepayment=prepayment),
            ),
            patch(
                "pool_service.finance_imports.odata_money_forecast._lookup_descriptions",
                side_effect=_lookups,
            ),
        ):
            result = read_money_forecast(
                datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
                config=_config(),
                opener=object(),
            )
        row = result.rows[0]
        self.assertEqual(row.expected_amount, Decimal("243695.00"))
        self.assertEqual(row.matched_paid_amount, Decimal("100000.00"))
        self.assertEqual(row.remaining_amount, Decimal("143695.00"))
        self.assertEqual(row.expected_date, date(2026, 10, 31))

    def test_multiple_schedule_rows_do_not_allocate_order_prepayment_arbitrarily(self):
        schedule = [
            {
                "Ref_Key": ORDER_GUID, "LineNumber": 1,
                "ДатаОплаты": "2026-10-10T00:00:00",
                "СуммаОплаты": "100000.00", "ПроцентОплаты": "40.00",
            },
            {
                "Ref_Key": ORDER_GUID, "LineNumber": 2,
                "ДатаОплаты": "2026-11-10T00:00:00",
                "СуммаОплаты": "143695.00", "ПроцентОплаты": "60.00",
            },
        ]
        prepayment = [{
            "Ref_Key": ORDER_GUID, "LineNumber": 1,
            "Документ": str(uuid.uuid4()),
            "Документ_Type": "StandardODATA.Document_ПоступлениеНаСчет",
            "СуммаПлатежа": "50000.00", "СуммаРасчетов": "50000.00",
            "ЭтоПредоплатаБезЗаказа": False,
        }]
        with (
            patch(
                "pool_service.finance_imports.odata_money_forecast._read",
                side_effect=_reader_side_effect(schedule=schedule, prepayment=prepayment),
            ),
            patch(
                "pool_service.finance_imports.odata_money_forecast._lookup_descriptions",
                side_effect=_lookups,
            ),
        ):
            result = read_money_forecast(
                datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
                config=_config(),
                opener=object(),
            )
        self.assertEqual(len(result.rows), 2)
        self.assertTrue(all(row.remaining_amount is None for row in result.rows))
        self.assertTrue(all(row.confirmation_status == "review" for row in result.rows))
        self.assertTrue(all(
            row.payment_match_status == "ambiguous_prepayment_allocation"
            for row in result.rows
        ))

    def test_cancelled_order_future_amount_is_excluded_from_main_forecast(self):
        schedule = [{
            "Ref_Key": ORDER_GUID,
            "LineNumber": 1,
            "ДатаОплаты": "2026-10-31T00:00:00",
            "СуммаОплаты": "243695.00",
            "ПроцентОплаты": "100.00",
        }]
        with (
            patch(
                "pool_service.finance_imports.odata_money_forecast._read",
                side_effect=_reader_side_effect(schedule=schedule, cancelled=True),
            ),
            patch(
                "pool_service.finance_imports.odata_money_forecast._lookup_descriptions",
                side_effect=_lookups,
            ),
        ):
            result = read_money_forecast(
                datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
                config=_config(),
                opener=object(),
            )
        row = result.rows[0]
        self.assertEqual(row.confirmation_status, "excluded")
        self.assertIn("отмен", row.basis)

    def test_invoice_only_order_is_possible_sale_not_main_forecast(self):
        with (
            patch(
                "pool_service.finance_imports.odata_money_forecast._read",
                side_effect=_reader_side_effect(state=STATE_INVOICE),
            ),
            patch(
                "pool_service.finance_imports.odata_money_forecast._lookup_descriptions",
                side_effect=_lookups,
            ),
        ):
            result = read_money_forecast(
                datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
                config=_config(),
                opener=object(),
            )
        self.assertEqual(result.rows[0].confirmation_status, "possible")
        self.assertIsNone(result.rows[0].remaining_amount)


class MoneyForecastReceivableMatchingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Money receivable test", paid_until=dj_timezone.now()
        )
        self.user = User.objects.create_user("money-receivable-owner")

    def _position(self, amount=None):
        settlements = ()
        if amount is not None:
            settlements = (
                SettlementPositionSourceRow(
                    side="customer",
                    settlement_type_raw="Долг",
                    management_classification="receivable",
                    organization_guid=ORG_GUID,
                    counterparty_guid=PARTY_GUID,
                    counterparty_name="Журавлики",
                    agreement_guid=None,
                    document_guid=REALIZATION_GUID,
                    document_type="Document_РасходнаяНакладная",
                    order_guid=ORDER_GUID,
                    order_type="Document_ЗаказПокупателя",
                    amount=Decimal(amount),
                    amount_currency=Decimal(amount),
                    amount_reg=None,
                    is_sign_anomaly=False,
                    source_identity=f"debt-{amount}",
                ),
            )
        at = datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc)
        return _persist_snapshot(
            self.organization,
            self.user,
            FinancePositionSourceSnapshot(
                snapshot_at=at,
                source_timezone="Asia/Barnaul",
                fetched_at=at,
                cash_rows=(),
                settlement_rows=settlements,
                diagnostics={},
            ),
        )

    def _schedule_row(self, identity, due, amount="36047.58"):
        return MoneyForecastSourceRow(
            direction="receipt",
            item_kind="order_schedule",
            source_identity=identity,
            order_guid=ORDER_GUID,
            document_guid=None,
            document_type="",
            agreement_guid=None,
            counterparty_guid=PARTY_GUID,
            counterparty_name="Журавлики",
            order_number="НФНФ-000032",
            order_date=date(2026, 1, 13),
            order_state="В работе",
            expected_amount=Decimal(amount),
            matched_paid_amount=Decimal("0.00"),
            remaining_amount=Decimal(amount),
            payment_match_status="no_prepayment",
            contractual_due_date=due,
            expected_date=due,
            expected_month=due.replace(day=1),
            date_precision="exact",
            basis="График оплаты заказа в 1С",
            confirmation_status="confirmed",
            source_updated_at=None,
            source_payload={},
        )

    def _source(self):
        at = datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc)
        return MoneyForecastSourceSnapshot(
            source_at=at,
            fetched_at=at,
            rows=(
                self._schedule_row("feb", date(2026, 2, 13)),
                self._schedule_row("sep", date(2026, 9, 13)),
                self._schedule_row("oct", date(2026, 10, 13)),
            ),
            diagnostics={},
        )

    def test_current_debt_is_allocated_to_latest_due_instalment_only(self):
        self._position("36047.58")
        result = _match_order_receivables(self.organization, self._source())
        feb, sep, oct_row = result.rows

        self.assertEqual(feb.confirmation_status, "excluded")
        self.assertEqual(feb.remaining_amount, Decimal("0.00"))
        self.assertEqual(feb.payment_match_status, "order_receivable_settled")

        self.assertEqual(sep.confirmation_status, "confirmed")
        self.assertEqual(sep.remaining_amount, Decimal("36047.58"))
        self.assertEqual(sep.payment_match_status, "order_receivable_matched")

        self.assertEqual(oct_row.confirmation_status, "confirmed")
        self.assertEqual(oct_row.remaining_amount, Decimal("36047.58"))
        self.assertEqual(oct_row.payment_match_status, "no_prepayment")

    def test_no_current_receivable_closes_past_instalments_but_keeps_future(self):
        self._position()
        result = _match_order_receivables(self.organization, self._source())
        feb, sep, oct_row = result.rows

        self.assertEqual(feb.confirmation_status, "excluded")
        self.assertEqual(sep.confirmation_status, "excluded")
        self.assertEqual(feb.remaining_amount, Decimal("0.00"))
        self.assertEqual(sep.remaining_amount, Decimal("0.00"))

        self.assertEqual(oct_row.confirmation_status, "confirmed")
        self.assertEqual(oct_row.remaining_amount, Decimal("36047.58"))


class MoneyForecastPlanTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Money test", paid_until=dj_timezone.now()
        )
        self.user = User.objects.create_user("money-owner")

    def test_explicit_summer_calendar_does_not_create_winter_receipts(self):
        plan = ManagementMoneyPlan.objects.create(
            organization=self.organization,
            direction="receipt",
            source_type="service",
            counterparty_name="Летний объект",
            amount=Decimal("50000.00"),
            date_precision="month",
            basis="Действующий договор обслуживания",
            confirmation_status="confirmed",
            service_period_start=date(2026, 5, 1),
            service_period_end=date(2026, 10, 31),
            service_active_months=[5, 6, 7, 8, 9],
            service_payment_offset_months=1,
            created_by=self.user,
            updated_by=self.user,
        )
        rows = _service_plan_items(plan)
        self.assertEqual(
            [item["service_month"].month for item in rows],
            [5, 6, 7, 8, 9],
        )
        self.assertEqual(
            [item["expected_month"].month for item in rows],
            [6, 7, 8, 9, 10],
        )
        self.assertNotIn(11, [item["expected_month"].month for item in rows])

    def test_management_october_expectation_is_bucketed_without_changing_contract_due(self):
        item = {
            "source": "service2",
            "id": 1,
            "direction": "receipt",
            "source_type": "order",
            "order_guid": ORDER_GUID,
            "counterparty_name": "СОШ №134 МАОУ",
            "object_name": "",
            "order_reference": "НФНФ-000094",
            "amount": Decimal("120000.00"),
            "remaining_amount": Decimal("120000.00"),
            "contractual_due_date": None,
            "expected_date": None,
            "expected_month": date(2026, 10, 1),
            "date_precision": "month",
            "basis": "Ожидание собственника: конец октября",
            "confirmation_status": "confirmed",
            "responsible": "owner",
            "last_updated": dj_timezone.now(),
            "note": "",
        }
        result = _bucket_forecast(
            [item],
            today=date(2026, 9, 30),
            forecast_months=[date(2026, 9, 1), date(2026, 10, 1)],
        )
        october = result["months"][1]
        self.assertEqual(october["receipts"], Decimal("120000.00"))
        self.assertIsNone(october["items"][0]["contractual_due_date"])

    def test_overdue_expectation_stays_overdue_and_is_not_rolled_forward(self):
        item = {
            "direction": "receipt",
            "confirmation_status": "confirmed",
            "remaining_amount": Decimal("10.00"),
            "expected_date": date(2026, 9, 1),
            "expected_month": date(2026, 9, 1),
            "date_precision": "exact",
            "payment_match_status": "no_prepayment",
        }
        result = _bucket_forecast(
            [item],
            today=date(2026, 9, 30),
            forecast_months=[date(2026, 9, 1), date(2026, 10, 1)],
        )
        self.assertEqual(len(result["overdue"]), 1)
        self.assertEqual(result["months"][0]["receipts"], Decimal("10.00"))
        self.assertTrue(result["months"][0]["items"][0]["is_overdue"])
        self.assertEqual(result["months"][0]["items"][0]["original_expected_date"], date(2026, 9, 1))
        self.assertEqual(result["months"][1]["receipts"], Decimal("0.00"))

    def test_onec_schedule_has_priority_over_manual_duplicate(self):
        snapshot = OneCMoneyForecastSnapshot.objects.create(
            organization=self.organization,
            source_at=dj_timezone.now(),
            fetched_at=dj_timezone.now(),
            is_active=True,
        )
        OneCMoneyForecastRow.objects.create(
            snapshot=snapshot,
            direction="receipt",
            item_kind="order_schedule",
            source_identity="schedule-1",
            order_guid=ORDER_GUID,
            counterparty_name="СОШ №134 МАОУ",
            order_number="НФНФ-000094",
            expected_amount=Decimal("100.00"),
            remaining_amount=Decimal("100.00"),
            payment_match_status="no_prepayment",
            expected_date=date(2026, 10, 31),
            expected_month=date(2026, 10, 1),
            date_precision="exact",
            basis="График 1С",
            confirmation_status="confirmed",
        )
        ManagementMoneyPlan.objects.create(
            organization=self.organization,
            direction="receipt",
            source_type="order",
            linked_order_guid=ORDER_GUID,
            counterparty_name="СОШ №134 МАОУ",
            amount=Decimal("90.00"),
            expected_month=date(2026, 10, 1),
            date_precision="month",
            basis="Ручное ожидание",
            confirmation_status="confirmed",
            created_by=self.user,
            updated_by=self.user,
        )
        items = _deduplicated_forecast(self.organization, snapshot)
        manual = [item for item in items if item["source"] == "service2"][0]
        self.assertEqual(manual["confirmation_status"], "excluded")
        self.assertEqual(manual["duplicate_reason"], "onec_priority")
