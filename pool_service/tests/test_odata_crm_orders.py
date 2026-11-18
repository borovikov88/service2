"""Synthetic 1C order evidence; no business source values or network calls."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from pool_service.finance_imports.odata_crm_orders import (
    FISCAL_CHECK, ORDER, INVOICE, SETTLEMENTS, CrmOrdersReadError, read_crm_documents, read_crm_orders, source_identity,
)
from pool_service.finance_imports.odata_profit import ODataConfig, ODataPreviewError, ZERO_GUID

ORG = "11111111-1111-1111-1111-111111111111"
CUSTOMER = "22222222-2222-2222-2222-222222222222"
AGREEMENT = "33333333-3333-3333-3333-333333333333"
CURRENCY = "44444444-4444-4444-4444-444444444444"
ORDER_ID = "55555555-5555-5555-5555-555555555555"
INVOICE_ID = "66666666-6666-6666-6666-666666666666"
PAYMENT_ID = "77777777-7777-7777-7777-777777777777"
OTHER = "88888888-8888-8888-8888-888888888888"
PAYMENT = "Document_ПоступлениеНаСчет"
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def config(**overrides):
    values = dict(base_url="https://example.test/odata/standard.odata/", organization_guids=(ORG,),
        max_pages=100, max_rows=10000)
    values.update(overrides)
    return ODataConfig(**values)


def header(entity, *, ref=None, amount="100.00", **changes):
    value = dict(Ref_Key=ref or {ORDER: ORDER_ID, INVOICE: INVOICE_ID, PAYMENT: PAYMENT_ID}[entity],
        Date="2026-10-01T09:00:00", Posted=True, DeletionMark=False, Number="Synthetic document",
        DataVersion="AAAA", Организация_Key=ORG, Контрагент_Key=CUSTOMER, СуммаДокумента=amount)
    if entity == PAYMENT:
        value.update(ВалютаДенежныхСредств_Key=CURRENCY, ВидОперации="ОтПокупателя")
    else:
        value.update(Договор_Key=AGREEMENT, ВалютаДокумента_Key=CURRENCY)
    if entity == ORDER:
        value["ПричинаОтмены_Key"] = ZERO_GUID
    if entity == INVOICE:
        value.update(Заказ=ORDER_ID, Заказ_Type="StandardODATA." + ORDER, ВидОперации="ПродажаПокупателю")
    value.update(changes)
    return value


def movement(*, payment=False, amount="100.00", **changes):
    value = dict(Recorder=PAYMENT_ID if payment else INVOICE_ID,
        Recorder_Type="StandardODATA." + (PAYMENT if payment else INVOICE),
        LineNumber="1", Period="2026-10-02T09:00:00", Active=True,
        RecordType="Expense" if payment else "Receipt", Организация_Key=ORG,
        Контрагент_Key=CUSTOMER, Договор_Key=AGREEMENT, Заказ=ORDER_ID,
        Заказ_Type="StandardODATA." + ORDER, ТипРасчетов="Долг", Документ=INVOICE_ID,
        Документ_Type="StandardODATA." + INVOICE, ДокументОплаты=PAYMENT_ID if payment else ZERO_GUID,
        ДокументОплаты_Type="StandardODATA." + PAYMENT if payment else "",
        СуммаВал=amount, СуммаПлатежа=amount if payment else "0.00", ЭтоОплатаЗаказа=True)
    value.update(changes)
    return value


class Response:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.data).encode()


class Source:
    def __init__(self, *, orders=None, invoices=None, payments=None, movements=None, mutate=None):
        self.rows = {ORDER: orders if orders is not None else [header(ORDER)],
            INVOICE: invoices if invoices is not None else [header(INVOICE)],
            PAYMENT: payments if payments is not None else [header(PAYMENT)],
            SETTLEMENTS: movements if movements is not None else [movement(), movement(payment=True)]}
        self.calls, self.mutate = [], mutate
    def open(self, request, timeout):
        entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
        query = unquote(urlsplit(request.full_url).query)
        self.calls.append((entity, request.method, query))
        rows = deepcopy(self.rows.get(entity, []))
        if self.mutate:
            rows = self.mutate(entity, rows, self.calls)
        return Response({"value": rows})


class ExactNumberSource(Source):
    """Emulate exact provider filters, and fail on an unscoped order read."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.urls = []
    def open(self, request, timeout):
        self.urls.append(request.full_url)
        entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
        if entity != ORDER:
            return super().open(request, timeout)
        query = parse_qs(urlsplit(request.full_url).query)["$filter"][0]
        self.calls.append((entity, request.method, query))
        rows = deepcopy(self.rows[ORDER])
        number = re.search(r"Number eq '((?:''|[^'])*)'", query)
        if number:
            number = number.group(1).replace("''", "'")
            customers = set(re.findall(r"Контрагент_Key eq guid'([^']+)'", query))
            if not customers:
                raise AssertionError("Number search must remain customer-scoped")
            rows = [row for row in rows if row["Number"] == number and row["Контрагент_Key"] in customers]
        else:
            refs = set(re.findall(r"Ref_Key eq guid'([^']+)'", query))
            if not refs:
                raise AssertionError("Exact lookup must not fall back to all orders")
            rows = [row for row in rows if row["Ref_Key"] in refs]
        if self.mutate:
            rows = self.mutate(entity, rows, self.calls)
        return Response({"value": rows})


class CrmOrderReaderTests(SimpleTestCase):
    def read(self, source=None, **kwargs):
        return read_crm_orders(config=config(), now=NOW, opener=source or Source(), **kwargs)

    def test_exact_fiscal_document_uses_scoped_header_and_claims_no_payment(self):
        source = Source()
        source.rows[FISCAL_CHECK] = [header(INVOICE, Number="SYN '100'", Заказ_Key=ORDER_ID)]
        rows = read_crm_documents(config=config(), kind="check", number="SYN '100'",
            customers=[CUSTOMER], now=NOW, opener=source)
        row = rows[ORG, INVOICE_ID]
        self.assertEqual((row["order_guid"], row["amount"], row["valid"]), (ORDER_ID, Decimal("100.00"), True))
        self.assertNotIn("paid_amount", row)
        entity, method, query = source.calls[0]
        self.assertEqual((entity, method), (FISCAL_CHECK, "GET"))
        self.assertIn("Number eq 'SYN ''100'''", query)
        self.assertIn(f"Контрагент_Key eq guid'{CUSTOMER}'", query)
        source.rows[FISCAL_CHECK][0]["Контрагент_Key"] = OTHER
        with self.assertRaises(CrmOrdersReadError):
            read_crm_documents(config=config(), kind="check", number="SYN '100'",
                customers=[CUSTOMER], now=NOW, opener=source)

    def test_realization_and_actual_payment_are_independent_verified_facts(self):
        source = Source()
        snapshot = self.read(source)
        row, = snapshot.rows
        self.assertEqual((row.amount, row.realized_amount, row.paid_amount), (Decimal("100"),) * 3)
        self.assertTrue(row.realization_complete and row.payment_complete and row.order_valid)
        self.assertEqual(row.blockers, ())
        self.assertEqual(snapshot.source_id, source_identity(config()))
        self.assertEqual(snapshot.diagnostics["read_method"], "complete_order_history_twice")
        self.assertFalse(snapshot.full_scope)
        self.assertTrue(snapshot.diagnostics["discovery_limited"])
        self.assertEqual(snapshot.requested_order_guids, (ORDER_ID,))
        self.assertTrue(all(method == "GET" for _, method, _ in source.calls))
        register_queries = [query for entity, _, query in source.calls if entity == SETTLEMENTS]
        self.assertTrue(all("Контрагент_Key eq guid'" in query and "Заказ eq" not in query for query in register_queries))
        self.assertFalse(any("Period ge" in query for query in register_queries))

    def test_invoice_order_payment_flag_is_not_payment(self):
        row, = self.read(Source(movements=[movement()])).rows
        self.assertEqual(row.realized_amount, Decimal("100"))
        self.assertEqual(row.paid_amount, Decimal("0"))
        self.assertTrue(row.payment_complete)

    def test_partial_order_allocation_uses_movement_not_whole_invoice(self):
        row, = self.read(Source(movements=[movement(amount="40.00"), movement(payment=True, amount="20.00")])).rows
        self.assertEqual(row.realized_amount, Decimal("40"))
        self.assertEqual(row.paid_amount, Decimal("20"))
        document, = [item for item in row.documents if item["kind"] == "realization"]
        self.assertEqual(Decimal(document["amount"]), Decimal("40"))

    def test_invoice_without_header_order_uses_explicit_register_order(self):
        row, = self.read(Source(invoices=[header(INVOICE, Заказ=ZERO_GUID, Заказ_Type="")])).rows
        self.assertEqual(row.realized_amount, Decimal("100"))

    def test_empty_ledger_is_known_zero_not_paid_from_missing_debt(self):
        row, = self.read(Source(movements=[])).rows
        self.assertEqual((row.realized_amount, row.paid_amount), (Decimal("0"), Decimal("0")))

    def test_unknown_advance_or_correction_never_becomes_payment(self):
        for change in ({"ТипРасчетов": "Аванс"}, {"RecordType": "Receipt"},
                {"СуммаВал": "-10.00"}, {"ДокументОплаты": ZERO_GUID},
                {"Recorder_Type": "StandardODATA.Document_КорректировкаДолга"}):
            with self.subTest(change=change):
                row, = self.read(Source(movements=[movement(), movement(payment=True, **change)])).rows
                self.assertIsNone(row.paid_amount)
                self.assertFalse(row.payment_complete)

    def test_payment_header_must_be_current_and_same_currency_party_and_operation(self):
        for change in ({"Posted": False}, {"DeletionMark": True}, {"Date": "2027-01-01T00:00:00"},
                {"Контрагент_Key": OTHER}, {"ВалютаДенежныхСредств_Key": OTHER},
                {"ВидОперации": "ПрочееПоступление"}):
            with self.subTest(change=change):
                row, = self.read(Source(payments=[header(PAYMENT, **change)])).rows
                self.assertIsNone(row.paid_amount)
                self.assertFalse(row.payment_complete)

    def test_deleted_unposted_or_mismatched_invoice_does_not_confirm_realization(self):
        for change in ({"Posted": False}, {"DeletionMark": True}, {"Заказ": OTHER},
                {"Договор_Key": OTHER}, {"ВидОперации": "Возврат"}, {"СуммаДокумента": "0.00"}):
            with self.subTest(change=change):
                row, = self.read(Source(invoices=[header(INVOICE, **change)])).rows
                self.assertIsNone(row.realized_amount)
                self.assertFalse(row.realization_complete)

    def test_missing_payment_header_is_unknown(self):
        row, = self.read(Source(payments=[])).rows
        self.assertIsNone(row.paid_amount)
        self.assertIn("payment_document_missing", row.blockers)

    def test_overallocated_invoice_and_payment_are_not_complete(self):
        rows = [movement(amount="110.00"), movement(payment=True, amount="110.00")]
        row, = self.read(Source(movements=rows)).rows
        self.assertIsNone(row.realized_amount)
        self.assertIsNone(row.paid_amount)

    def test_duplicate_movement_is_rejected(self):
        with self.assertRaises(CrmOrdersReadError):
            self.read(Source(movements=[movement(), movement()]))

    def test_outside_organization_is_rejected(self):
        with self.assertRaises(CrmOrdersReadError):
            self.read(Source(orders=[header(ORDER, Организация_Key=OTHER)]))

    def test_partial_scope_and_missing_order_are_explicit(self):
        snapshot = self.read(Source(orders=[]), order_guids=[ORDER_ID])
        self.assertFalse(snapshot.full_scope)
        self.assertEqual(snapshot.requested_order_guids, (ORDER_ID,))
        self.assertEqual(snapshot.diagnostics["missing_order_guids"], [ORDER_ID])
        self.assertEqual(snapshot.rows, ())

    def test_cancelled_order_is_retained_as_invalid_for_existing_links(self):
        row, = self.read(Source(orders=[header(ORDER, ПричинаОтмены_Key=OTHER)])).rows
        self.assertTrue(row.cancelled)
        self.assertFalse(row.order_valid)
        self.assertIsNone(row.realized_amount)
        self.assertIsNone(row.paid_amount)

    def test_unfinished_order_without_customer_is_retained_without_blocking_other_reads(self):
        row, = self.read(Source(orders=[header(ORDER, Posted=False, Контрагент_Key=ZERO_GUID)])).rows
        self.assertFalse(row.order_valid)
        self.assertEqual(row.customer_guid, "")
        self.assertIsNone(row.paid_amount)

    def test_shared_document_cannot_credit_multiple_orders_above_its_total(self):
        source = Source(invoices=[header(INVOICE, Заказ=ZERO_GUID, Заказ_Type="")], movements=[
            movement(amount="75.00"), movement(payment=True, amount="75.00"),
            movement(amount="75.00", Заказ=OTHER, LineNumber="2"),
            movement(payment=True, amount="75.00", Заказ=OTHER, LineNumber="2")])
        row, = self.read(source, order_guids=[ORDER_ID]).rows
        self.assertIsNone(row.realized_amount)
        self.assertIsNone(row.paid_amount)

    def test_unassigned_payment_allocation_still_consumes_document_capacity(self):
        source = Source(movements=[movement(), movement(payment=True),
            movement(payment=True, LineNumber="2", Заказ=ZERO_GUID, Заказ_Type="", amount="50.00")])
        row, = self.read(source).rows
        self.assertEqual(row.realized_amount, Decimal("100"))
        self.assertIsNone(row.paid_amount)
        self.assertFalse(row.payment_complete)
        self.assertIn("payment_allocation_exceeds_document", row.blockers)

    def test_invoice_correction_without_order_makes_net_evidence_unknown(self):
        source = Source(movements=[movement(), movement(payment=True),
            movement(Recorder=OTHER, Recorder_Type="StandardODATA.Document_КорректировкаДолга",
                LineNumber="2", Заказ=ZERO_GUID, Заказ_Type="", amount="-50.00")])
        row, = self.read(source).rows
        self.assertIsNone(row.realized_amount)
        self.assertIsNone(row.paid_amount)
        self.assertFalse(row.realization_complete or row.payment_complete)

    def test_correction_referencing_payment_without_order_is_not_lost(self):
        source = Source(movements=[movement(), movement(payment=True),
            movement(Recorder=OTHER, Recorder_Type="StandardODATA.Document_КорректировкаДолга",
                LineNumber="2", Заказ=ZERO_GUID, Заказ_Type="", amount="-50.00",
                Документ=PAYMENT_ID, Документ_Type="StandardODATA." + PAYMENT)])
        row, = self.read(source).rows
        self.assertIsNone(row.paid_amount)
        self.assertIn("related_document_requires_review", row.blockers)

    def test_unrelated_unassigned_movements_do_not_hide_valid_order_facts(self):
        source = Source(movements=[movement(), movement(payment=True),
            movement(Recorder=OTHER, Recorder_Type="StandardODATA.Document_КорректировкаДолга",
                LineNumber="2", Заказ=ZERO_GUID, Заказ_Type="", amount="-50.00", Документ=OTHER)])
        row, = self.read(source).rows
        self.assertEqual((row.realized_amount, row.paid_amount), (Decimal("100"), Decimal("100")))

    def test_shared_receipt_can_pay_another_explicit_contract_without_blocking_this_order(self):
        source = Source(orders=[header(ORDER, amount="60.00")], invoices=[header(INVOICE, amount="60.00")],
            movements=[movement(amount="60.00"), movement(payment=True, amount="60.00"),
                movement(payment=True, amount="40.00", LineNumber="2", Заказ=OTHER,
                    Договор_Key=OTHER, Документ=OTHER)])
        row, = self.read(source).rows
        self.assertEqual((row.realized_amount, row.paid_amount), (Decimal("60"), Decimal("60")))
        self.assertTrue(row.realization_complete and row.payment_complete)

    def test_mismatched_realization_is_not_exposed_in_public_document_list(self):
        for change in ({"Контрагент_Key": OTHER}, {"Договор_Key": OTHER},
                {"Заказ": OTHER}, {"ВалютаДокумента_Key": OTHER}):
            with self.subTest(change=change):
                row, = self.read(Source(invoices=[header(INVOICE, Number="Other scope private number", **change)])).rows
                self.assertIsNone(row.realized_amount)
                self.assertFalse(any(doc["kind"] == "realization" for doc in row.documents))
                self.assertNotIn("Other scope private number", json.dumps(row.documents))

    def test_bounded_discovery_refreshes_old_bound_orders_outside_latest_window(self):
        orders = [header(ORDER, ref=str(UUID(int=index + 100)),
            Date=(datetime(2020, 1, 1) + timedelta(days=index)).isoformat()) for index in range(600)]
        old_ref = orders[0]["Ref_Key"]
        orders[0]["DeletionMark"] = True
        class HistorySource(Source):
            def open(self, request, timeout):
                entity = unquote(urlsplit(request.full_url).path.rsplit("/", 1)[-1])
                query = unquote(urlsplit(request.full_url).query)
                self.calls.append((entity, request.method, query))
                if entity != ORDER:
                    return Response({"value": []})
                if "$top=200" in query:
                    return Response({"value": sorted(deepcopy(orders), key=lambda row: row["Date"], reverse=True)[:200]})
                refs = set(re.findall(r"Ref_Key eq guid'([^']+)'", query))
                return Response({"value": [deepcopy(row) for row in orders if row["Ref_Key"] in refs]})
        snapshot = self.read(HistorySource(), required_order_guids=[old_ref])
        self.assertEqual(len(snapshot.rows), 201)
        self.assertIn(old_ref, snapshot.requested_order_guids)
        self.assertFalse(snapshot.full_scope)
        self.assertTrue(snapshot.diagnostics["discovery_limited"])
        self.assertFalse(next(row for row in snapshot.rows if row.order_guid == old_ref).order_valid)

    def test_changed_discovery_window_is_not_claimed_as_stable(self):
        def mutate(entity, rows, calls):
            if entity == ORDER and sum(call[0] == ORDER for call in calls) == 4:
                rows[0]["Ref_Key"] = OTHER
            return rows
        with self.assertRaisesMessage(CrmOrdersReadError, "changed during"):
            self.read(Source(mutate=mutate))

    def test_number_lookup_finds_old_order_beyond_latest_600_and_excludes_other_customer(self):
        orders = [header(ORDER, ref=str(UUID(int=index + 100)), Number=f"SYN-{index}",
            Date=(datetime(2020, 1, 1) + timedelta(days=index)).isoformat()) for index in range(600)]
        old_ref = orders[0]["Ref_Key"]
        orders[-1].update(Number="SYN-0", Контрагент_Key=OTHER)
        source = ExactNumberSource(orders=orders, movements=[])
        snapshot = self.read(source, order_number="  SYN-0  ", customer_guids=[CUSTOMER])
        row, = snapshot.rows
        self.assertEqual(row.order_guid, old_ref)
        self.assertEqual(row.customer_guid, CUSTOMER)
        self.assertEqual(snapshot.requested_order_guids, (old_ref,))
        self.assertFalse(snapshot.full_scope)
        self.assertTrue(snapshot.diagnostics["discovery_limited"])
        self.assertEqual(snapshot.diagnostics["discovery_mode"], "number")
        self.assertFalse(any("$top" in url for url in source.urls))

    def test_number_lookup_empty_result_does_not_fall_back_to_all_orders(self):
        source = ExactNumberSource()
        snapshot = self.read(source, order_number="SYN-MISSING", customer_guids=[CUSTOMER])
        self.assertEqual(snapshot.rows, ())
        self.assertEqual(snapshot.requested_order_guids, ())
        self.assertFalse(snapshot.full_scope)
        self.assertTrue(snapshot.diagnostics["discovery_limited"])
        self.assertEqual(len(source.calls), 2)
        self.assertTrue(all("Number eq 'SYN-MISSING'" in query for _, _, query in source.calls))

    def test_number_lookup_quotes_the_string_literal_and_url_encodes_query_characters(self):
        number = "SYN'&+ /?"
        source = ExactNumberSource(orders=[header(ORDER, Number=number)], movements=[])
        row, = self.read(source, order_number=number, customer_guids=[CUSTOMER]).rows
        self.assertEqual(row.number, number)
        filters = [parse_qs(urlsplit(url).query)["$filter"][0] for url in source.urls]
        numbered = [value for value in filters if "Number eq" in value]
        self.assertEqual(len(numbered), 2)
        self.assertTrue(all("Number eq 'SYN''&+ /?'" in value for value in numbered))
        self.assertTrue(all("Организация_Key eq guid'" in value and "Контрагент_Key eq guid'" in value for value in numbered))
        self.assertTrue(any("%26%2B" in url for url in source.urls))

    def test_number_lookup_rejects_invalid_or_unscoped_arguments_before_any_request(self):
        cases = [dict(order_number=value, customer_guids=[CUSTOMER]) for value in ("", "   ", "x" * 81, True, "A\nB")]
        cases += [dict(order_number="SYN", customer_guids=value) for value in ((), None, CUSTOMER, [ZERO_GUID], ["invalid"])]
        cases += [dict(customer_guids=[CUSTOMER]),
            dict(order_number="SYN", customer_guids=[CUSTOMER], order_guids=[ORDER_ID])]
        for arguments in cases:
            source = Source()
            with self.subTest(arguments=arguments), self.assertRaises(ODataPreviewError):
                self.read(source, **arguments)
            self.assertEqual(source.calls, [])

    def test_number_lookup_rejects_a_provider_response_from_another_customer(self):
        source = Source(orders=[header(ORDER, Number="SYN-1", Контрагент_Key=OTHER)])
        with self.assertRaisesMessage(CrmOrdersReadError, "another customer"):
            self.read(source, order_number="SYN-1", customer_guids=[CUSTOMER])
        self.assertEqual(len(source.calls), 1)

    def test_number_lookup_rechecks_customer_before_reading_any_ledger(self):
        def mutate(entity, rows, calls):
            if entity == ORDER and len(calls) == 2:
                rows[0]["Контрагент_Key"] = OTHER
            return rows
        source = ExactNumberSource(orders=[header(ORDER, Number="SYN-1")], mutate=mutate)
        with self.assertRaisesMessage(CrmOrdersReadError, "scope changed"):
            self.read(source, order_number="SYN-1", customer_guids=[CUSTOMER])
        self.assertTrue(all(entity == ORDER for entity, _, _ in source.calls))

    def test_number_lookup_refreshes_required_renamed_order_and_reports_missing_required(self):
        source = ExactNumberSource(orders=[header(ORDER, Number="SYN-NEW")], movements=[])
        snapshot = self.read(source, order_number="SYN-OLD", customer_guids=[CUSTOMER],
            required_order_guids=[ORDER_ID, OTHER])
        row, = snapshot.rows
        self.assertEqual(row.number, "SYN-NEW")
        self.assertEqual(set(snapshot.requested_order_guids), {ORDER_ID, OTHER})
        self.assertEqual(snapshot.diagnostics["missing_order_guids"], [OTHER])

    def test_required_number_lookup_cannot_bypass_customer_scope(self):
        source = ExactNumberSource(orders=[header(ORDER, Number="SYN-NEW", Контрагент_Key=OTHER)], movements=[])
        with self.assertRaisesMessage(CrmOrdersReadError, "scope changed"):
            self.read(source, order_number="SYN-OLD", customer_guids=[CUSTOMER], required_order_guids=[ORDER_ID])
        self.assertTrue(all(entity == ORDER for entity, _, _ in source.calls))

    @override_settings(ONEC_ODATA_BASE_URL="https://example.test/odata/standard.odata/",
        ONEC_ODATA_ORGANIZATION_GUIDS=(ORG,), ONEC_ODATA_PAYROLL_CURRENCY_GUID=CURRENCY,
        ONEC_ODATA_PAYROLL_CURRENCY_CODE="RUB", ONEC_ODATA_PAYROLL_AUTO_COVERAGE_GUIDS=ORG,
        ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS="")
    def test_currency_label_requires_acknowledged_exact_source_binding(self):
        row, = self.read().rows
        self.assertEqual(row.currency_code, "RUB")
        for setting in ({"ONEC_ODATA_BASE_URL": "https://other.test/odata/standard.odata/"},
                {"ONEC_ODATA_PAYROLL_AUTO_COVERAGE_GUIDS": ""},
                {"ONEC_ODATA_PAYROLL_CURRENCY_GUID": OTHER}):
            with self.subTest(setting=setting), override_settings(**setting):
                row, = self.read().rows
                self.assertEqual(row.currency_code, "")

    def test_source_change_is_detected_even_with_constant_data_version(self):
        def mutate(entity, rows, calls):
            if entity == ORDER and sum(call[0] == ORDER for call in calls) == 2:
                rows[0]["СуммаДокумента"] = "200.00"
            return rows
        with self.assertRaisesMessage(CrmOrdersReadError, "changed during"):
            self.read(Source(mutate=mutate))

    def test_context_precision_cannot_round_money(self):
        with localcontext() as context:
            context.prec = 2
            row, = self.read(Source(orders=[header(ORDER, amount="123456.78")],
                invoices=[header(INVOICE, amount="123456.78")], payments=[header(PAYMENT, amount="123456.78")],
                movements=[movement(amount="123456.78"), movement(payment=True, amount="123456.78")])).rows
        self.assertEqual(row.paid_amount, Decimal("123456.78"))

    def test_global_page_and_row_budgets_apply_across_both_reads(self):
        for limits in ({"max_pages": 2}, {"max_rows": 2}):
            with self.subTest(limits=limits), self.assertRaises(ODataPreviewError):
                read_crm_orders(config=config(**limits), now=NOW, opener=Source())

    def test_transport_requires_https_and_aware_clock(self):
        for arguments in ({"config": config(base_url="http://example.test/odata/standard.odata/")},
                {"now": datetime(2026, 11, 15)}):
            values = dict(config=config(), now=NOW, opener=Source())
            values.update(arguments)
            with self.assertRaises(CrmOrdersReadError):
                read_crm_orders(**values)

    def test_nonfinite_or_unbounded_amounts_are_safe_read_errors(self):
        for amount in ("Infinity", "NaN", "1e1000000", "0.001", True):
            with self.subTest(amount=amount), self.assertRaises(CrmOrdersReadError):
                self.read(Source(orders=[header(ORDER, amount=amount)]))

    def test_foreign_pagination_is_rejected_by_shared_transport(self):
        class RedirectingSource:
            def open(self, request, timeout):
                return Response({"value": [], "@odata.nextLink": "https://foreign.test/leak"})
        with self.assertRaises(ODataPreviewError):
            self.read(RedirectingSource())
