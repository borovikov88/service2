#!/usr/bin/env python3
"""GET-only monthly accrual-register preview. No import and no FOT classification.

The live schema establishes field names, not the semantics of catalog Тип values.
Run this file directly on the hosting to print aggregate diagnostics only.
"""
import argparse
import base64
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, unquote, parse_qsl
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

REGISTER = 'AccumulationRegister_НачисленияИУдержания_RecordType'
SETTLEMENTS = 'AccumulationRegister_РасчетыСПерсоналом_RecordType'
CATALOG = 'Catalog_ВидыНачисленийИУдержаний'
FIELDS = ('Recorder', 'Recorder_Type', 'LineNumber', 'Active', 'Period',
          'ПериодРегистрации', 'Организация_Key', 'Сотрудник_Key', 'Валюта_Key',
          'ВидНачисленияУдержания_Key', 'Сумма', 'СуммаВал')
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50000
MAX_PAGES = 100
MAX_OUTPUT_BYTES = 256 * 1024
SOCKET_TIMEOUT = 15
TOTAL_TIMEOUT = 60
IDENTIFIER = re.compile(r'[^\W\d]\w*(?:\.[^\W\d]\w*)*\Z', re.UNICODE)
CODES = {'INVALID_CONFIG', 'INVALID_MONTH', 'INVALID_GUID', 'INVALID_ROW',
         'INVALID_AMOUNT', 'INVALID_DATE', 'INVALID_TYPE', 'ORGANIZATION_MISMATCH',
         'MONTH_MISMATCH', 'DUPLICATE_ROW', 'CATALOG_INCOMPLETE', 'CATALOG_INVALID',
         'INVALID_RESPONSE', 'UNSAFE_PAGINATION', 'RESPONSE_LIMIT', 'TIMEOUT',
         'HTTP_ERROR', 'CHECK_FAILED', 'OUTPUT_LIMIT'}
STAGES = {'config', 'tls', 'open', 'read', 'parse', 'register', 'catalog',
          'aggregate', 'dependencies', 'output', 'runtime'}


class PayrollError(Exception):
    def __init__(self, code, stage='parse', reason='VALIDATION', verify_code=None, http_status=None):
        super().__init__(code)
        self.code, self.stage, self.reason = code, stage, reason
        self.verify_code = verify_code
        self.http_status = http_status


def diagnostic(exc, stage='runtime'):
    """Never return exception text, URL, credentials, IDs or response bodies."""
    if isinstance(exc, PayrollError):
        result = {'error': exc.code if isinstance(exc.code, str) and exc.code in CODES else 'CHECK_FAILED',
                  'stage': exc.stage if isinstance(exc.stage, str) and exc.stage in STAGES else 'runtime',
                  'reason': exc.reason if isinstance(exc.reason, str) and exc.reason in {'VALIDATION', 'TIMEOUT', 'TLS_CERTIFICATE', 'TLS', 'DNS', 'NETWORK', 'DEPENDENCY', 'IO', 'UNEXPECTED'} else 'UNEXPECTED'}
        code = exc.verify_code
    else:
        for _ in range(3):
            if not isinstance(exc, URLError) or not isinstance(exc.reason, BaseException):
                break
            exc = exc.reason
        reason = 'UNEXPECTED'
        if isinstance(exc, ssl.SSLCertVerificationError):
            reason = 'TLS_CERTIFICATE'
        elif isinstance(exc, ssl.SSLError):
            reason = 'TLS'
        elif isinstance(exc, socket.gaierror):
            reason = 'DNS'
        elif isinstance(exc, TimeoutError):
            reason = 'TIMEOUT'
        elif isinstance(exc, (URLError, ConnectionError)):
            reason = 'NETWORK'
        elif isinstance(exc, ImportError):
            reason = 'DEPENDENCY'
        elif isinstance(exc, OSError):
            reason = 'IO'
        result = {'error': 'CHECK_FAILED', 'stage': stage if isinstance(stage, str) and stage in STAGES else 'runtime', 'reason': reason}
        code = getattr(exc, 'verify_code', None)
    if result['reason'] == 'TLS_CERTIFICATE' and type(code) is int and 0 <= code <= 2147483647:
        result['verify_code'] = code
    http_status = getattr(exc, 'http_status', None)
    if type(http_status) is int and 100 <= http_status <= 599:
        result['http_status'] = http_status
    return result


def guard(stage, call):
    try:
        return call()
    except PayrollError:
        raise
    except HTTPError as exc:
        raise PayrollError('HTTP_ERROR', stage, http_status=exc.code) from None
    except Exception as exc:
        safe = diagnostic(exc, stage)
        raise PayrollError(safe['error'], safe['stage'], safe['reason'], safe.get('verify_code')) from None


def guid(value, *, zero=False):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
        raise PayrollError('INVALID_GUID')
    parsed = UUID(value)
    if not parsed.int and not zero:
        raise PayrollError('INVALID_GUID')
    return str(parsed)


def month_bounds(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', value):
        raise PayrollError('INVALID_MONTH', 'config')
    try:
        first = date.fromisoformat(value + '-01')
        last = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    except ValueError:
        raise PayrollError('INVALID_MONTH', 'config') from None
    return first, last


def calendar_date(value):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,7})?(?:Z|[+-][0-9]{2}:[0-9]{2})?', value):
        raise PayrollError('INVALID_DATE')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        raise PayrollError('INVALID_DATE') from None


def amount(value):
    if type(value) not in (int, Decimal, str) or isinstance(value, str) and (len(value) > 64 or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value)):
        raise PayrollError('INVALID_AMOUNT')
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PayrollError('INVALID_AMOUNT') from None
    if not number.is_finite() or len(number.as_tuple().digits) > 28 or not -12 <= number.as_tuple().exponent <= 12 or number.copy_abs() > Decimal('1e18'):
        raise PayrollError('INVALID_AMOUNT')
    return number


def line_number(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value):
        value = int(value)
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise PayrollError('INVALID_ROW', 'register')
    return value


def identifier(value):
    if not isinstance(value, str) or len(value) > 128 or not IDENTIFIER.fullmatch(value):
        raise PayrollError('INVALID_TYPE')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_url(base, entity, options):
    return base + quote(entity, safe='') + '?' + '&'.join(k + '=' + quote(v, safe='') for k, v in options.items())


def no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise PayrollError('INVALID_RESPONSE')
        obj[key] = value
    return obj


class Reader:
    def __init__(self, config, opener=None):
        self.base = config.get('ONEC_ODATA_BASE_URL') or ''
        parts = guard('config', lambda: urlsplit(self.base))
        port = guard('config', lambda: parts.port)
        if parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment or not parts.path.endswith('/odata/standard.odata/') or any(c in self.base for c in '\r\n\t\\'):
            raise PayrollError('INVALID_CONFIG', 'config')
        self.origin = (parts.scheme, parts.hostname, port or 443)
        username, password = config.get('ONEC_ODATA_USERNAME') or '', config.get('ONEC_ODATA_PASSWORD') or ''
        if not username or not password or ':' in username:
            raise PayrollError('INVALID_CONFIG', 'config')
        self.auth = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode('ascii')
        if opener is None:
            context = guard('tls', lambda: ssl.create_default_context(cafile=config.get('SSL_CERT_FILE') or None, capath=config.get('SSL_CERT_DIR') or None))
            opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        self.opener = opener
        self.bytes = self.pages = self.rows = 0
        self.deadline = time.monotonic() + TOTAL_TIMEOUT

    def check_time(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PayrollError('TIMEOUT', 'read', 'TIMEOUT')
        return min(SOCKET_TIMEOUT, remaining)

    def pages_for(self, entity, options):
        url = make_url(self.base, entity, options)
        expected_path = unquote(urlsplit(url).path)
        seen = set()
        while url:
            self.check_time()
            parts = guard('parse', lambda: urlsplit(url))
            if (parts.scheme, parts.hostname, guard('parse', lambda: parts.port) or 443) != self.origin or parts.username is not None or parts.password is not None or parts.fragment or unquote(parts.path) != expected_path or any(c in url for c in '\r\n\t\\') :
                raise PayrollError('UNSAFE_PAGINATION')
            # A nextLink may page this same selected dataset; it must never
            # widen selection, expand employee references or change its filter.
            pairs = guard('parse', lambda: parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True))
            query = dict(pairs)
            if len(query) != len(pairs):
                raise PayrollError('UNSAFE_PAGINATION')
            for key, value in pairs:
                if key in options:
                    if value != options[key]:
                        raise PayrollError('UNSAFE_PAGINATION')
                elif key in ('$skip', '$top'):
                    if not re.fullmatch(r'[0-9]{1,8}', value) or int(value) > MAX_ROWS:
                        raise PayrollError('UNSAFE_PAGINATION')
                elif key == '$skiptoken':
                    if not value or len(value) > 8192 or any(ord(char) < 32 for char in value):
                        raise PayrollError('UNSAFE_PAGINATION')
                else:
                    raise PayrollError('UNSAFE_PAGINATION')
            paging = {key: str(int(value)) if key in ('$skip', '$top') else value
                      for key, value in sorted(query.items()) if key not in options}
            url = make_url(self.base, entity, {**options, **paging})
            if url in seen:
                raise PayrollError('UNSAFE_PAGINATION')
            seen.add(url)
            self.pages += 1
            if self.pages > MAX_PAGES:
                raise PayrollError('RESPONSE_LIMIT')
            request = Request(url, headers={'Accept': 'application/json', 'Authorization': self.auth}, method='GET')
            with guard('open', lambda: self.opener.open(request, timeout=self.check_time())) as response:
                if response.status != 200:
                    raise PayrollError('HTTP_ERROR', 'open', http_status=response.status)
                raw = guard('read', lambda: response.read(MAX_BYTES - self.bytes + 1))
            self.bytes += len(raw)
            if self.bytes > MAX_BYTES:
                raise PayrollError('RESPONSE_LIMIT')
            self.check_time()
            try:
                data = json.loads(raw.decode('utf-8-sig'), parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(PayrollError('INVALID_RESPONSE')), object_pairs_hook=no_duplicate_keys)
            except (ValueError, UnicodeError, RecursionError):
                raise PayrollError('INVALID_RESPONSE') from None
            if not isinstance(data, dict):
                raise PayrollError('INVALID_RESPONSE')
            if 'd' in data and 'value' not in data:
                data = data['d']
                if not isinstance(data, dict) or 'results' not in data:
                    raise PayrollError('INVALID_RESPONSE')
                rows, next_url = data['results'], data.get('__next')
            elif 'value' in data and 'd' not in data:
                rows = data['value']
                links = [data[k] for k in ('odata.nextLink', '@odata.nextLink') if k in data]
                if len(links) > 1:
                    raise PayrollError('INVALID_RESPONSE')
                next_url = links[0] if links else None
            else:
                raise PayrollError('INVALID_RESPONSE')
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise PayrollError('INVALID_RESPONSE')
            self.rows += len(rows)
            if self.rows > MAX_ROWS:
                raise PayrollError('RESPONSE_LIMIT')
            yield rows
            if next_url is not None and (not isinstance(next_url, str) or not next_url or len(next_url) > 16384):
                raise PayrollError('UNSAFE_PAGINATION')
            url = urljoin(url, next_url) if next_url else None


def read_monthly_preview(config, month, *, organization_guids=(), opener=None):
    """Return unclassified aggregate preview; never return or persist employee rows."""
    first, last = month_bounds(month)
    raw_orgs = config.get('ONEC_ODATA_ORGANIZATION_GUIDS') or ''
    if not isinstance(raw_orgs, str):
        raise PayrollError('INVALID_CONFIG', 'config')
    allowed = {guid(v) for v in (part.strip() for part in raw_orgs.split(',')) if v}
    selected = {guid(v) for v in organization_guids} if organization_guids else allowed
    if not allowed or not selected or not selected.issubset(allowed) or len(allowed) > 40:
        raise PayrollError('INVALID_CONFIG', 'config')
    reader = Reader(config, opener)
    filters = "Active eq true and ПериодРегистрации ge datetime'%sT00:00:00' and ПериодРегистрации lt datetime'%sT00:00:00' and (%s)" % (first, last, ' or '.join("Организация_Key eq guid'%s'" % key for key in sorted(selected)))
    records, seen, types = [], set(), set()
    inactive = 0
    for rows in reader.pages_for(REGISTER, {'$format': 'json', '$select': ','.join(FIELDS), '$filter': filters}):
        for row in rows:
            if type(row.get('Active')) is not bool:
                raise PayrollError('INVALID_ROW', 'register')
            org = guid(row.get('Организация_Key'))
            if org not in selected:
                raise PayrollError('ORGANIZATION_MISMATCH', 'register')
            registration = calendar_date(row.get('ПериодРегистрации'))
            if not first <= registration < last:
                raise PayrollError('MONTH_MISMATCH', 'register')
            if not row['Active']:
                inactive += 1
                continue
            recorder = guid(row.get('Recorder'))
            recorder_type = identifier(row.get('Recorder_Type'))
            line = line_number(row.get('LineNumber'))
            key = (recorder_type, recorder, line)
            if key in seen:
                raise PayrollError('DUPLICATE_ROW', 'register')
            seen.add(key)
            guid(row.get('Сотрудник_Key'))
            currency = guid(row.get('Валюта_Key'), zero=True)
            type_id = guid(row.get('ВидНачисленияУдержания_Key'))
            value, value_currency = amount(row.get('Сумма')), amount(row.get('СуммаВал'))
            period = calendar_date(row.get('Period'))
            records.append((org, currency, type_id, value, value_currency, (period.year, period.month) != (registration.year, registration.month)))
            types.add(type_id)
    catalog = {}
    ordered = sorted(types)
    for start in range(0, len(ordered), 40):
        requested = set(ordered[start:start + 40])
        filter_value = ' or '.join("Ref_Key eq guid'%s'" % key for key in sorted(requested))
        for rows in reader.pages_for(CATALOG, {'$format': 'json', '$select': 'Ref_Key,Тип,IsFolder,DeletionMark', '$filter': '(' + filter_value + ')'}):
            for row in rows:
                key = guid(row.get('Ref_Key'))
                if key not in requested or key in catalog or row.get('IsFolder') is not False or row.get('DeletionMark') is not False:
                    raise PayrollError('CATALOG_INVALID', 'catalog')
                catalog[key] = identifier(row.get('Тип'))
        if not requested.issubset(catalog):
            raise PayrollError('CATALOG_INCOMPLETE', 'catalog')
    groups = defaultdict(lambda: {'rows': 0, 'amount': Decimal(0), 'amount_currency': Decimal(0), 'negative_amount_rows': 0, 'negative_currency_amount_rows': 0, 'non_cent_rows': 0, 'period_month_differs_rows': 0})
    kind_groups = defaultdict(lambda: {'rows': 0, 'amount': Decimal(0), 'amount_currency': Decimal(0), 'negative_amount_rows': 0, 'negative_currency_amount_rows': 0, 'non_cent_rows': 0, 'period_month_differs_rows': 0})
    with localcontext() as context:
        context.prec = 64
        for org, currency, type_id, value, value_currency, differs in records:
            detail = kind_groups[(org, currency, catalog[type_id], type_id)]
            detail['rows'] += 1
            detail['amount'] += value
            detail['amount_currency'] += value_currency
            detail['negative_amount_rows'] += value < 0
            detail['negative_currency_amount_rows'] += value_currency < 0
            detail['non_cent_rows'] += value != value.quantize(Decimal('.01')) or value_currency != value_currency.quantize(Decimal('.01'))
            detail['period_month_differs_rows'] += differs
            group = groups[(org, currency, catalog[type_id])]
            group['rows'] += 1
            group['amount'] += value
            group['amount_currency'] += value_currency
            group['negative_amount_rows'] += value < 0
            group['negative_currency_amount_rows'] += value_currency < 0
            group['non_cent_rows'] += value != value.quantize(Decimal('.01')) or value_currency != value_currency.quantize(Decimal('.01'))
            group['period_month_differs_rows'] += differs
    output = []
    for (org, currency, category), values in sorted(groups.items()):
        output.append({'organization_guid': org, 'currency_guid': currency, 'type_value': category,
                       **{k: format(v, 'f') if isinstance(v, Decimal) else v for k, v in values.items()}})
    result = {'kind': 'unclassified_monthly_payroll_preview', 'month': month,
              'status': 'data_present' if records else 'missing', 'fot_amount': None,
              'period_basis': 'ПериодРегистрации', 'semantic_status': 'unverified',
              'notice': 'Register amounts grouped by actual catalog type. Not verified FOT; counts do not prove completeness.',
              'selected_organizations': sorted(selected), 'organizations_without_rows': sorted(selected - {r[0] for r in records}),
              'rows': len(records), 'inactive_rows_ignored': inactive, 'groups': output}
    result['kind_groups'] = [dict(organization_guid=org, currency_guid=currency, type_value=category, kind_guid=kind, **{k: format(v, 'f') if isinstance(v, Decimal) else v for k, v in values.items()}) for (org, currency, category, kind), values in sorted(kind_groups.items())]
    # The existing Excel source is a settlements report, with opening/closing
    # balances and payments. Read these movements separately; their RecordType
    # is not yet mapped to accruals/payments and no opening balance is fabricated.
    settlement_groups = defaultdict(lambda: {'positive_amount': Decimal(0), 'negative_amount': Decimal(0), 'positive_amount_currency': Decimal(0), 'negative_amount_currency': Decimal(0), 'rows': 0, 'amount': Decimal(0), 'amount_currency': Decimal(0), 'negative_amount_rows': 0, 'negative_currency_amount_rows': 0, 'non_cent_rows': 0, 'period_month_differs_rows': 0})
    settlement_seen, settlement_orgs = set(), set()
    settlement_inactive = 0
    fields = tuple(field for field in FIELDS if field != 'ВидНачисленияУдержания_Key') + ('RecordType',)
    for rows in reader.pages_for(SETTLEMENTS, {'$format': 'json', '$select': ','.join(fields), '$filter': filters}):
        for row in rows:
            if type(row.get('Active')) is not bool:
                raise PayrollError('INVALID_ROW', 'register')
            org = guid(row.get('Организация_Key'))
            if org not in selected:
                raise PayrollError('ORGANIZATION_MISMATCH', 'register')
            registration = calendar_date(row.get('ПериодРегистрации'))
            if not first <= registration < last:
                raise PayrollError('MONTH_MISMATCH', 'register')
            if not row['Active']:
                settlement_inactive += 1
                continue
            recorder = guid(row.get('Recorder'))
            recorder_type = identifier(row.get('Recorder_Type'))
            record_type = identifier(row.get('RecordType'))
            line = line_number(row.get('LineNumber'))
            identity = (recorder_type, recorder, line)
            if identity in settlement_seen:
                raise PayrollError('DUPLICATE_ROW', 'register')
            settlement_seen.add(identity)
            guid(row.get('Сотрудник_Key'))
            currency = guid(row.get('Валюта_Key'), zero=True)
            value, value_currency = amount(row.get('Сумма')), amount(row.get('СуммаВал'))
            period = calendar_date(row.get('Period'))
            group = settlement_groups[(org, currency, record_type, recorder_type)]
            with localcontext() as context:
                context.prec = 64
                group['positive_amount'] += max(value, Decimal(0))
                group['negative_amount'] += min(value, Decimal(0))
                group['positive_amount_currency'] += max(value_currency, Decimal(0))
                group['negative_amount_currency'] += min(value_currency, Decimal(0))
                group['rows'] += 1
                group['amount'] += value
                group['amount_currency'] += value_currency
                group['negative_amount_rows'] += value < 0
                group['negative_currency_amount_rows'] += value_currency < 0
                group['non_cent_rows'] += value != value.quantize(Decimal('.01')) or value_currency != value_currency.quantize(Decimal('.01'))
                group['period_month_differs_rows'] += (period.year, period.month) != (registration.year, registration.month)
            settlement_orgs.add(org)
    result['settlements'] = {'status': 'data_present' if settlement_seen else 'missing',
        'opening_balance': None, 'closing_balance': None, 'accrued': None, 'paid': None,
        'rows': len(settlement_seen), 'inactive_rows_ignored': settlement_inactive,
        'organizations_without_rows': sorted(selected - settlement_orgs),
        'groups': [{'organization_guid': org, 'currency_guid': currency, 'record_type': record_type, 'recorder_type': recorder_type,
                    **{key: format(value, 'f') if isinstance(value, Decimal) else value for key, value in values.items()}}
                   for (org, currency, record_type, recorder_type), values in sorted(settlement_groups.items())]}
    reader.check_time()
    if len((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode()) > MAX_OUTPUT_BYTES:
        raise PayrollError('OUTPUT_LIMIT', 'output')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', required=True)
    parser.add_argument('--month', required=True)
    parser.add_argument('--organization-guid', action='append', default=[])
    args = parser.parse_args(argv)
    stage = 'runtime'
    def timeout(signum, frame):
        raise PayrollError('TIMEOUT', 'runtime', 'TIMEOUT')
    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(TOTAL_TIMEOUT)
        stage = 'dependencies'
        from dotenv import dotenv_values
        stage = 'config'
        config = {**dotenv_values(Path(args.app_dir) / '.env'), **os.environ}
        result = read_monthly_preview(config, args.month, organization_guids=args.organization_guid)
        stage = 'output'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps(diagnostic(exc, stage)))
        return 1
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    sys.exit(main())
