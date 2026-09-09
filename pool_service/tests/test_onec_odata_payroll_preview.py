"""Synthetic, DB-free tests for the unclassified 1C payroll preview."""
import importlib.util
import json
from pathlib import Path
import ssl
import unittest
from unittest.mock import patch
from urllib.error import URLError, HTTPError
from urllib.parse import unquote, urlsplit
from decimal import Decimal, localcontext

SPEC = importlib.util.spec_from_file_location('payroll_preview_under_test', Path(__file__).resolve().parents[1] / 'finance_imports' / 'odata_payroll.py')
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)
ORG = '11111111-1111-1111-1111-111111111111'
EMP = '22222222-2222-2222-2222-222222222222'
TYPE = '33333333-3333-3333-3333-333333333333'
CURRENCY = '44444444-4444-4444-4444-444444444444'
DOC = '55555555-5555-5555-5555-555555555555'
CONFIG = {'ONEC_ODATA_BASE_URL': 'https://example.test/test/odata/standard.odata/',
          'ONEC_ODATA_USERNAME': 'test-user', 'ONEC_ODATA_PASSWORD': 'secret-not-output',
          'ONEC_ODATA_ORGANIZATION_GUIDS': ORG}


def row(**changes):
    data = {'Recorder': DOC, 'Recorder_Type': 'StandardODATA.Document_НачислениеЗарплатыУНФ', 'LineNumber': 1,
            'Active': True, 'Period': '2026-09-01T00:00:00', 'ПериодРегистрации': '2026-08-01T00:00:00',
            'Организация_Key': ORG, 'Сотрудник_Key': EMP, 'Валюта_Key': CURRENCY,
            'ВидНачисленияУдержания_Key': TYPE, 'Сумма': '100.10', 'СуммаВал': '100.10'}
    data.update(changes)
    return data


def catalog(**changes):
    data = {'Ref_Key': TYPE, 'Тип': 'Начисление', 'IsFolder': False, 'DeletionMark': False}
    data.update(changes)
    return data


class Response:
    status = 200
    def __init__(self, data):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.raw[:limit]


class Opener:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []
    def open(self, request, timeout):
        self.requests.append(request)
        assert request.method == 'GET'
        assert 0 < timeout <= p.SOCKET_TIMEOUT
        return Response(self.pages.pop(0))


def run_preview(rows=None, types=None, settlements=None, **kwargs):
    rows = [row()] if rows is None else rows
    pages = [{'value': rows}]
    if any(r.get('Active') for r in rows):
        pages.append({'value': [catalog()] if types is None else types})
    pages.append({'value': [] if settlements is None else settlements})
    return p.read_monthly_preview(CONFIG, '2026-08', opener=Opener(pages), **kwargs)


class PayrollPreviewTests(unittest.TestCase):
    def test_signed_exact_sums_and_diagnostics_not_fot(self):
        result = run_preview([row(), row(LineNumber=2, Сумма='-0.001', СуммаВал='-0.001')])
        group = result['groups'][0]
        self.assertEqual(group['amount'], '100.099')
        self.assertEqual(group['negative_amount_rows'], 1)
        self.assertEqual(group['non_cent_rows'], 1)
        self.assertEqual(group['period_month_differs_rows'], 2)
        self.assertIsNone(result['fot_amount'])
        self.assertEqual(result['semantic_status'], 'unverified')
        serialized = json.dumps(result)
        self.assertEqual(result['kind_groups'][0]['kind_guid'], TYPE)
        for secret in (EMP, DOC, CONFIG['ONEC_ODATA_PASSWORD']):
            self.assertNotIn(secret, serialized)

    def test_reader_preserves_kind_and_signed_settlement_components(self):
        kind = '66666666-6666-6666-6666-666666666666'
        deduction = row(LineNumber=2, ВидНачисленияУдержания_Key=kind, Сумма='12.00', СуммаВал='12.00')
        result = run_preview([row(), deduction], [catalog(), catalog(Ref_Key=kind, Тип='Налог')],
            [row(RecordType='Receipt'), row(LineNumber=2, RecordType='Receipt', Сумма='-12.00', СуммаВал='-12.00')])
        self.assertEqual({g['kind_guid'] for g in result['kind_groups']}, {TYPE, kind})
        control = result['settlements']['groups'][0]
        self.assertEqual(control['positive_amount'], '100.10')
        self.assertEqual(control['negative_amount'], '-12.00')
        self.assertEqual(control['amount'], '88.10')

    def test_settlements_separate_without_fabricated_balances(self):
        payment = row(RecordType='Expense', Recorder_Type='StandardODATA.Document_РасходСоСчета', Сумма='-5')
        result = run_preview(settlements=[payment])
        settlement = result['settlements']
        self.assertEqual(settlement['groups'][0]['record_type'], 'Expense')
        self.assertEqual(settlement['groups'][0]['amount'], '-5')
        for field in ('opening_balance', 'closing_balance', 'accrued', 'paid'):
            self.assertIsNone(settlement[field])

    def test_empty_is_missing_and_zero_currency_is_not_rub(self):
        result = run_preview([])
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['organizations_without_rows'], [ORG])
        self.assertIsNone(result['fot_amount'])
        zero = '00000000-0000-0000-0000-000000000000'
        self.assertEqual(run_preview([row(Валюта_Key=zero)])['groups'][0]['currency_guid'], zero)

    def test_month_and_org_guards_on_every_register(self):
        for register in ('accrual', 'settlement'):
            for changes, expected in [({'Организация_Key': EMP}, 'ORGANIZATION_MISMATCH'),
                                      ({'ПериодРегистрации': '2026-07-31T23:59:59'}, 'MONTH_MISMATCH')]:
                with self.subTest(register=register, changes=changes):
                    bad = row(RecordType='Receipt', **changes)
                    with self.assertRaises(p.PayrollError) as caught:
                        run_preview([bad] if register == 'accrual' else None, settlements=[bad] if register == 'settlement' else None)
                    self.assertEqual(caught.exception.code, expected)

    def test_invalid_row_fields_and_duplicates(self):
        for changes in ({'Active': 1}, {'LineNumber': True}, {'LineNumber': -1}, {'Сотрудник_Key': '0'}, {'Recorder': 'bad'}, {'Recorder_Type': 'employee name'}):
            with self.subTest(changes=changes), self.assertRaises(p.PayrollError):
                run_preview([row(**changes)])
        for settlement in (False, True):
            with self.assertRaises(p.PayrollError) as caught:
                repeated = [row(RecordType='Receipt'), row(RecordType='Receipt')]
                run_preview(None if settlement else repeated, settlements=repeated if settlement else None)
            self.assertEqual(caught.exception.code, 'DUPLICATE_ROW')

    def test_inactive_rows_ignored_but_scope_checked(self):
        result = run_preview([row(Active=False)])
        self.assertEqual(result['inactive_rows_ignored'], 1)
        self.assertEqual(result['rows'], 0)
        with self.assertRaises(p.PayrollError):
            run_preview([row(Active=False, Организация_Key=EMP)])

    def test_catalog_must_be_complete_valid_and_unique(self):
        for types in ([], [catalog(DeletionMark=True)], [catalog(IsFolder=0)], [catalog(Тип='person name')], [catalog(Ref_Key=EMP)], [catalog(), catalog()]):
            with self.subTest(types=types), self.assertRaises(p.PayrollError):
                run_preview(types=types)

    def test_money_limits_reject_bool_float_nonfinite_and_long_decimal(self):
        for value in (True, 1.2, 'NaN', Decimal('Infinity'), '1e100', '0.00000000000001', '1' * 29, None):
            with self.subTest(value=value), self.assertRaises(p.PayrollError):
                p.amount(value)
        self.assertEqual(p.amount(Decimal('0.1')), Decimal('0.1'))
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(run_preview([row(Сумма='999999999999999999.123456789')])['groups'][0]['amount'], '999999999999999999.123456789')

    def test_scope_configuration_required_and_requested_subset(self):
        for config in ({**CONFIG, 'ONEC_ODATA_ORGANIZATION_GUIDS': ''}, {**CONFIG, 'ONEC_ODATA_BASE_URL': 'http://example.test/odata/standard.odata/'}):
            with self.assertRaises(p.PayrollError):
                p.read_monthly_preview(config, '2026-08', opener=Opener([]))
        with self.assertRaises(p.PayrollError):
            run_preview(organization_guids=[EMP])
        for month in ('2026-13', '26-08', '2026-8', '9999-12'):
            with self.assertRaises(p.PayrollError):
                p.month_bounds(month)

    def test_pagination_legacy_and_query_only_and_literal_dollar(self):
        pages = [{'d': {'results': [row()], '__next': '?$skiptoken=2'}}, {'d': {'results': [row(LineNumber=2)]}}, {'value': [catalog()]}, {'value': []}]
        opener = Opener(pages)
        result = p.read_monthly_preview(CONFIG, '2026-08', opener=opener)
        self.assertEqual(result['rows'], 2)
        url = opener.requests[0].full_url
        self.assertIn('$filter=', url)
        self.assertNotIn('%24filter', url)
        self.assertIn('$filter=', opener.requests[1].full_url)
        self.assertIn('$select=', opener.requests[1].full_url)
        self.assertIn('$skiptoken=2', opener.requests[1].full_url)
        self.assertIn('ПериодРегистрации ge', unquote(url))
        self.assertNotIn('Description', unquote(opener.requests[2].full_url))

    def test_unsafe_next_links_and_loops(self):
        initial = p.make_url(CONFIG['ONEC_ODATA_BASE_URL'], p.REGISTER, {'$format': 'json'})
        for link in ('https://evil.test/path', 'https://user:pass@example.test/test/odata/standard.odata/x', '#fragment', '/test/odata/standard.odata/Catalog_Secret', '?page=1'):
            with self.subTest(link=link):
                reader = p.Reader(CONFIG, Opener([{'value': [], 'odata.nextLink': link}] * 3))
                with self.assertRaises(p.PayrollError):
                    list(reader.pages_for(p.REGISTER, {'$format': 'json'}))

    def test_pagination_cannot_expand_or_change_dataset(self):
        for link in ('?$expand=Сотрудник&$select=*', '?$select=*', '?$filter=Active%20eq%20false',
                     '?$skiptoken=1&$skiptoken=2', '?%24expand=Сотрудник', '?$top=-1', '?$unknown=x'):
            opener = Opener([{'value': [], 'odata.nextLink': link}])
            with self.subTest(link=link), self.assertRaises(p.PayrollError):
                list(p.Reader(CONFIG, opener).pages_for(p.REGISTER, {'$select': 'Active', '$filter': 'Active eq true'}))
            self.assertEqual(len(opener.requests), 1)

    def test_response_limits_and_malformed_json(self):
        for payload in (b'{"value":[],"value":[]}', b'{"value":[{"x":NaN}]}', {'value': [], 'd': {}}, {'value': 'bad'}, {'value': [], '@odata.nextLink': 2}):
            with self.subTest(payload=payload), self.assertRaises(p.PayrollError):
                list(p.Reader(CONFIG, Opener([payload])).pages_for(p.REGISTER, {}))
        with patch.object(p, 'MAX_BYTES', 4), self.assertRaises(p.PayrollError):
            list(p.Reader(CONFIG, Opener([{'value': []}])).pages_for(p.REGISTER, {}))
        with patch.object(p, 'MAX_ROWS', 0), self.assertRaises(p.PayrollError):
            run_preview()

    def test_catalog_requests_batch_at_most_40_used_guids(self):
        ids = [str(p.UUID(int=i + 1)) for i in range(41)]
        rows = [row(LineNumber=i + 1, ВидНачисленияУдержания_Key=key) for i, key in enumerate(ids)]
        opener = Opener([{'value': rows}, {'value': [catalog(Ref_Key=key) for key in ids[:40]]},
                         {'value': [catalog(Ref_Key=ids[40])]}, {'value': []}])
        result = p.read_monthly_preview(CONFIG, '2026-08', opener=opener)
        self.assertEqual(result['rows'], 41)
        self.assertEqual(unquote(opener.requests[1].full_url).count('Ref_Key eq'), 40)
        self.assertEqual(unquote(opener.requests[2].full_url).count('Ref_Key eq'), 1)

    def test_limits_timeout_and_decimal_json_parsing(self):
        raw = json.dumps({'value': [row()]}).replace('"100.10"', '100.10').encode()
        result = p.read_monthly_preview(CONFIG, '2026-08', opener=Opener([raw, {'value': [catalog()]}, {'value': []}]))
        self.assertEqual(result['groups'][0]['amount'], '100.10')
        with patch.object(p, 'MAX_PAGES', 1), self.assertRaises(p.PayrollError):
            run_preview()
        reader = p.Reader(CONFIG, Opener([]))
        reader.deadline = -1
        with self.assertRaises(p.PayrollError) as caught:
            list(reader.pages_for(p.REGISTER, {}))
        self.assertEqual(caught.exception.code, 'TIMEOUT')

    def test_safe_diagnostics_reject_unhashable_and_preserve_http_status(self):
        self.assertEqual(p.diagnostic(p.PayrollError([], [], [])),
                         {'error': 'CHECK_FAILED', 'stage': 'runtime', 'reason': 'UNEXPECTED'})
        error = HTTPError('https://secret.example', 403, 'secret', {}, None)
        def fail():
            raise error
        with self.assertRaises(p.PayrollError) as caught:
            p.guard('open', fail)
        safe = p.diagnostic(caught.exception)
        self.assertEqual(safe['http_status'], 403)
        self.assertNotIn('secret', json.dumps(safe))
        circular = URLError('secret')
        circular.reason = circular
        self.assertEqual(p.diagnostic(circular)['reason'], 'NETWORK')

    def test_int64_strings_and_normalized_duplicate_identity(self):
        self.assertEqual(run_preview([row(LineNumber='1')])['rows'], 1)
        with self.assertRaises(p.PayrollError):
            run_preview([row(LineNumber='1'), row(LineNumber=1)])
        for value in ('1.0', '-1', '1e2', '9223372036854775808', True):
            with self.subTest(value=value), self.assertRaises(p.PayrollError):
                p.line_number(value)

    def test_exact_output_limit(self):
        result = run_preview()
        actual = len((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode())
        with patch.object(p, 'MAX_OUTPUT_BYTES', actual - 1), self.assertRaises(p.PayrollError):
            run_preview()

    def test_tls_and_secret_safe_failure(self):
        err = ssl.SSLCertVerificationError(1, 'secret-not-output https://password@example.test')
        err.verify_code = 20
        safe = p.diagnostic(URLError(err), 'open')
        self.assertEqual(safe['reason'], 'TLS_CERTIFICATE')
        self.assertEqual(safe['verify_code'], 20)
        self.assertNotIn('secret', json.dumps(safe))
        with patch.object(p.ssl, 'create_default_context') as context, patch.object(p, 'build_opener'):
            p.Reader({**CONFIG, 'SSL_CERT_FILE': '/ca/test.pem', 'SSL_CERT_DIR': '/ca/dir'})
        context.assert_called_once_with(cafile='/ca/test.pem', capath='/ca/dir')
        self.assertIsNone(p.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.test'))


if __name__ == '__main__':
    unittest.main()
