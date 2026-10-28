"""Bounded document evidence adapter over the existing read-only 1C gateway.

No route, credentials, import, ledger, CRM write or service/payment approval.
Caller must already be authorized by the existing Diagnostic/CRM boundary.
Metadata and transport are explicit: this adapter never creates a connection.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID

from django.utils import timezone
from . import onec_diagnostic as base
from .onec_diagnostic_universal import query_1c_rows

HEADER = 'Document_РасходнаяНакладная'
LINES = HEADER + '_Запасы'
HEADER_FIELDS = {'Ref_Key':'Edm.Guid', 'Организация_Key':'Edm.Guid', 'Контрагент_Key':'Edm.Guid',
    'Договор_Key':'Edm.Guid', 'ВалютаДокумента_Key':'Edm.Guid', 'Date':'Edm.DateTime',
    'Posted':'Edm.Boolean', 'DeletionMark':'Edm.Boolean', 'DataVersion':'Edm.String'}
LINE_FIELDS = {'Ref_Key':'Edm.Guid', 'LineNumber':'Edm.Int64', 'ИдентификаторСтроки':'Edm.String',
    'КлючСвязи':'Edm.Int64', 'Номенклатура_Key':'Edm.Guid', 'Количество':'Edm.Double',
    'Сумма':'Edm.Double', 'Всего':'Edm.Double', 'СуммаНДС':'Edm.Double'}
MAX_LINES = 200


@dataclass(frozen=True)
class DocumentLine:
    number: int
    identifier_hint: str
    link_key_hint: int
    nomenclature: str
    quantity: Decimal
    amount: Decimal
    vat: Decimal
    total: Decimal
    content_hash: str
    # Neither LineNumber nor the observed optional hints prove persistence.
    stable_identity_proven: bool = False


@dataclass(frozen=True)
class DocumentSnapshot:
    source_id: str
    metadata_hash: str
    observed_at: str
    as_of_date: date
    document_ref: str
    organization_ref: str
    client_ref: str
    contract_ref: str
    currency_ref: str | None
    document_date: datetime
    posted: bool
    deleted: bool
    data_version: str
    snapshot_hash: str
    lines: tuple[DocumentLine, ...]
    warnings: tuple[str, ...]
    binding_allowed: bool = False
    realized_amount: None = None
    paid_amount: None = None
    confirmed_months: tuple = ()


def _error(code):
    raise base.OneCDiagnosticError(code)


def _guid(value, *, optional=False):
    try:
        result=UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _error('DOCUMENT_GUID_INVALID')
    if not result.int:
        if optional: return None
        _error('DOCUMENT_GUID_MISSING')
    return str(result)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _text(value):
    if type(value) is not str or len(value.encode()) > 256:
        _error('DOCUMENT_TEXT_INVALID')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 128:
        _error('DOCUMENT_NUMBER_INVALID')
    try: result=Decimal(str(value))
    except InvalidOperation: _error('DOCUMENT_NUMBER_INVALID')
    if not result.is_finite() or result and not -324 <= result.adjusted() <= 308:
        _error('DOCUMENT_NUMBER_INVALID')
    return result


def _canonical(number):
    if number == 0: return '0'
    value=format(number, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _header(raw, expected):
    if {key:_guid(raw.get(key)) for key in expected} != expected:
        _error('DOCUMENT_SCOPE_MISMATCH')
    if type(raw.get('Posted')) is not bool or type(raw.get('DeletionMark')) is not bool:
        _error('DOCUMENT_STATUS_UNAVAILABLE')
    value=raw.get('Date')
    if type(value) is not str or len(value) > 40:
        _error('DOCUMENT_DATE_INVALID')
    try: parsed=datetime.fromisoformat(value)
    except ValueError: _error('DOCUMENT_DATE_INVALID')
    return (parsed, raw['Posted'], raw['DeletionMark'], _text(raw.get('DataVersion')),
            _guid(raw.get('ВалютаДокумента_Key'), optional=True))


def _int64(value):
    if type(value) is str and re.fullmatch(r"-?[0-9]{1,19}", value):
        value = int(value)
    if type(value) is not int or not -(2**63) <= value < 2**63:
        _error("DOCUMENT_LINE_POSITION_INVALID")
    return value


def _lines(rows, ref):
    result=[]; seen=set()
    for row in rows:
        if _guid(row.get('Ref_Key')) != ref: _error('DOCUMENT_LINE_SCOPE_MISMATCH')
        number, link=_int64(row.get('LineNumber')), _int64(row.get('КлючСвязи'))
        if type(number) is not int or number <= 0 or number in seen or type(link) is not int:
            _error('DOCUMENT_LINE_POSITION_INVALID')
        seen.add(number)
        hint=_text(row.get('ИдентификаторСтроки'))
        item=_guid(row.get('Номенклатура_Key'))
        quantity, amount, total, vat=(_number(row.get(key)) for key in ('Количество','Сумма','Всего','СуммаНДС'))
        fingerprint=_hash([item,*map(_canonical,(quantity,amount,total,vat))])
        result.append(DocumentLine(number,hint,link,item,quantity,amount,vat,total,fingerprint))
    return tuple(sorted(result,key=lambda row:row.number))


def read_service_document_snapshot(config, *, document_ref, organization_ref, client_ref,
                                   contract_ref, as_of_date, metadata_raw, opener, previous=None):
    """Explicit transport only, no automatic fetch/import or durable snapshot.

    Two equal bounded reads detect observed changes, but are not a transactional
    1C snapshot. No financial/service conclusion or approval is ever returned.
    """
    if type(as_of_date) is not date or not isinstance(metadata_raw, bytes) or opener is None:
        _error('DOCUMENT_PREVIEW_INPUT_INVALID')
    config=base._validated_config(config)
    ref, org, client, contract=map(_guid,(document_ref,organization_ref,client_ref,contract_ref))
    if org not in {_guid(value) for value in config.organization_guids}:
        _error('ORGANIZATION_SCOPE_VIOLATION')
    index=base.parse_metadata(metadata_raw)
    for entity, expected_fields in ((HEADER,HEADER_FIELDS),(LINES,LINE_FIELDS)):
        schema=index.get(entity)
        if not schema or any(schema.field_types.get(key)!=kind for key,kind in expected_fields.items()):
            _error('DOCUMENT_SCHEMA_UNCONFIRMED')
    source_id=_hash(config.base_url)
    if previous is not None and (type(previous) is not DocumentSnapshot or
        (previous.source_id,previous.document_ref,previous.organization_ref,previous.client_ref,previous.contract_ref)
        != (source_id,ref,org,client,contract)):
        _error('PREVIOUS_DOCUMENT_SCOPE_MISMATCH')
    expected={'Ref_Key':ref,'Организация_Key':org,'Контрагент_Key':client,'Договор_Key':contract}
    def read(entity, fields, limit):
        value=query_1c_rows(config,entity,fields=list(fields),filters=[{'field':'Ref_Key','op':'eq','value':ref}],
            limit=limit,include_deleted=True,metadata_raw=metadata_raw,opener=opener)
        if not value['complete'] or value['truncated']:
            _error('DOCUMENT_SNAPSHOT_INCOMPLETE')
        return value['rows']
    headers=read(HEADER,HEADER_FIELDS,1)
    if len(headers)!=1: _error('DOCUMENT_HEADER_UNAVAILABLE')
    header=_header(headers[0],expected)
    lines=_lines(read(LINES,LINE_FIELDS,MAX_LINES),ref)
    repeated_lines=_lines(read(LINES,LINE_FIELDS,MAX_LINES),ref)
    repeated_headers=read(HEADER,HEADER_FIELDS,1)
    if len(repeated_headers)!=1 or _header(repeated_headers[0],expected)!=header or repeated_lines!=lines:
        _error('DOCUMENT_CHANGED_DURING_READ')
    document_date, posted, deleted, version, currency=header
    digest=_hash([header,[tuple(vars(line).values()) for line in lines]])
    warnings=['service_scope_unconfirmed','service_periods_unconfirmed','payment_unverified',
              'line_identity_requires_reconciliation','non_atomic_read']
    if not posted: warnings.append('unposted_document')
    if deleted: warnings.append('deleted_document')
    if document_date.date()>as_of_date: warnings.append('future_document')
    if document_date.tzinfo is None: warnings.append('source_timezone_unspecified')
    if currency is None: warnings.append('currency_unavailable')
    if not lines: warnings.append('empty_document')
    if any(line.quantity<0 or line.amount<0 or line.total<0 for line in lines): warnings.append('correction_requires_review')
    if any(count>1 for count in Counter(line.content_hash for line in lines).values()): warnings.append('ambiguous_equal_lines')
    if any(count>1 for count in Counter(line.identifier_hint for line in lines if line.identifier_hint).values()): warnings.append('duplicate_identifier_hints')
    if previous is not None:
        if previous.snapshot_hash!=digest or previous.metadata_hash!=hashlib.sha256(metadata_raw).hexdigest():
            warnings.append('source_changed_requires_review')
            if (Counter(line.content_hash for line in previous.lines)==Counter(line.content_hash for line in lines)
                and [(line.number,line.identifier_hint,line.link_key_hint) for line in previous.lines]
                != [(line.number,line.identifier_hint,line.link_key_hint) for line in lines]):
                warnings.append('possible_renumbering_requires_review')
        else: warnings.append('unchanged_preview')
    return DocumentSnapshot(source_id,hashlib.sha256(metadata_raw).hexdigest(),timezone.now().isoformat(),as_of_date,
        ref,org,client,contract,currency,document_date,posted,deleted,version,digest,lines,tuple(warnings))
