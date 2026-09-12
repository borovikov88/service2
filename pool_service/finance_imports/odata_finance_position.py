"""GET-only 1C OData Balance reader for the point-in-time finance position."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib, json, os
from urllib.parse import quote
from urllib.request import build_opener
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.conf import settings
from .odata_profit import NoRedirectHandler, ODataConfig, ODataPreviewError, ZERO_GUID, normalize_guid, read_odata_pages, validate_config

CALENDAR_TIMEZONE_DEFAULT = "Asia/Barnaul"
REGULAR_REGISTER = "AccumulationRegister_ДенежныеСредства"
KKM_REGISTER = "AccumulationRegister_ДенежныеСредстваВКассахККМ"
IN_TRANSIT_REGISTER = "AccumulationRegister_ДенежныеСредстваКПоступлению"
CUSTOMER_REGISTER = "AccumulationRegister_РасчетыСПокупателями"
SUPPLIER_REGISTER = "AccumulationRegister_РасчетыСПоставщиками"
REGISTER_SPECS = {
 "regular": {"entity": REGULAR_REGISTER, "fields": ("Организация_Key","ТипДенежныхСредств","БанковскийСчетКасса","БанковскийСчетКасса_Type","Валюта_Key","ДоговорКонтрагента_Key","СуммаBalance","СуммаВалBalance")},
 "kkm": {"entity": KKM_REGISTER, "fields": ("Организация_Key","КассаККМ_Key","СуммаBalance","СуммаВалBalance")},
 "in_transit": {"entity": IN_TRANSIT_REGISTER, "fields": ("Организация_Key","Касса","Касса_Type","ДокументПередачи","ДокументПередачи_Type","СуммаBalance","СуммаВалBalance")},
 "customer": {"entity": CUSTOMER_REGISTER, "fields": ("Организация_Key","ТипРасчетов","Контрагент_Key","Договор_Key","Документ","Документ_Type","Заказ","Заказ_Type","СуммаBalance","СуммаВалBalance","СуммаРегBalance")},
 "supplier": {"entity": SUPPLIER_REGISTER, "fields": ("Организация_Key","ТипРасчетов","Контрагент_Key","Договор_Key","Документ","Документ_Type","Заказ_Key","СуммаBalance","СуммаВалBalance","СуммаРегBalance")},
}
CATALOG_COUNTERPARTIES="Catalog_Контрагенты"; CATALOG_BANK_ACCOUNTS="Catalog_БанковскиеСчета"; CATALOG_CASHES="Catalog_Кассы"; CATALOG_KKM="Catalog_КассыККМ"; DOCUMENT_CASH_WITHDRAWAL="Document_ВыемкаНаличных"
MONEY_REFERENCE_TYPES=frozenset({CATALOG_BANK_ACCOUNTS,CATALOG_CASHES,CATALOG_KKM}); DOCUMENT_REFERENCE_TYPES=frozenset({DOCUMENT_CASH_WITHDRAWAL}); REFERENCE_ALLOWLIST=frozenset({CATALOG_COUNTERPARTIES,*MONEY_REFERENCE_TYPES,*DOCUMENT_REFERENCE_TYPES})

@dataclass(frozen=True)
class CashPositionSourceRow:
 source_kind:str; organization_guid:str; account_guid:str; reference_type:str; display_name:str; currency_guid:str|None; agreement_guid:str|None; amount:Decimal; amount_currency:Decimal|None; transfer_document_guid:str|None=None; transfer_document_type:str=""; transfer_document_display:str=""; source_identity:str=""
@dataclass(frozen=True)
class SettlementPositionSourceRow:
 side:str; settlement_type_raw:str; management_classification:str; organization_guid:str; counterparty_guid:str; counterparty_name:str; agreement_guid:str|None; document_guid:str|None; document_type:str; order_guid:str|None; order_type:str; amount:Decimal; amount_currency:Decimal|None; amount_reg:Decimal|None; is_sign_anomaly:bool; source_identity:str
@dataclass(frozen=True)
class FinancePositionSourceSnapshot:
 snapshot_at:datetime; source_timezone:str; fetched_at:datetime; cash_rows:tuple; settlement_rows:tuple; diagnostics:dict
class FinancePositionReadError(ODataPreviewError): pass

def calendar_timezone():
 raw=(os.getenv("ONEC_ODATA_CALENDAR_TIMEZONE") or getattr(settings,"ONEC_ODATA_CALENDAR_TIMEZONE","") or CALENDAR_TIMEZONE_DEFAULT).strip()
 try: return ZoneInfo(raw)
 except (ZoneInfoNotFoundError, ValueError): raise FinancePositionReadError("1C calendar timezone is invalid") from None

def snapshot_calendar_time(now=None):
 current=now or datetime.now(timezone.utc)
 if current.tzinfo is None: raise FinancePositionReadError("Snapshot clock must be timezone-aware")
 return current.astimezone(calendar_timezone()).replace(microsecond=0)

def config_from_settings():
 return ODataConfig(settings.ONEC_ODATA_BASE_URL,settings.ONEC_ODATA_USERNAME,settings.ONEC_ODATA_PASSWORD,tuple(settings.ONEC_ODATA_ORGANIZATION_GUIDS),settings.ONEC_ODATA_TIMEOUT_SECONDS,settings.ONEC_ODATA_MAX_PAGES,settings.ONEC_ODATA_MAX_ROWS)
def validate_finance_position_configuration(config=None):
 result=validate_config(config or config_from_settings()); calendar_timezone(); return result

def classify_settlement(side, settlement_type, amount):
 if amount == 0: return "zero"
 table={('customer','Долг',1):'receivable',('customer','Аванс',-1):'customer_advance',('supplier','Долг',1):'payable',('supplier','Аванс',-1):'supplier_advance'}
 return table.get((side,settlement_type,1 if amount>0 else -1),"sign_anomaly")

def _decimal(value, field, optional=False):
 if optional and value in (None,""): return None
 if isinstance(value,(float,bool)) or value is None: raise FinancePositionReadError(f"{field} must be decimal")
 try: result=Decimal(str(value))
 except (InvalidOperation,ValueError): raise FinancePositionReadError(f"{field} must be decimal") from None
 if not result.is_finite(): raise FinancePositionReadError(f"{field} must be finite")
 return result.quantize(Decimal("0.01"))
def _optional_guid(value, field): return None if value in (None,"",ZERO_GUID) else normalize_guid(value,field=field)
def _string(value, field, limit=160):
 if not isinstance(value,str) or not value.strip() or len(value.strip())>limit: raise FinancePositionReadError(f"{field} is invalid")
 return value.strip()
def _type(value, allowed, field):
 raw=_string(value,field,240); raw=raw[len("StandardODATA."):] if raw.startswith("StandardODATA.") else raw
 if raw not in allowed: raise FinancePositionReadError(f"{field} is not allowed")
 return raw
def _identity(kind, values): return hashlib.sha256(json.dumps([kind,*values],ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
def _org_filter(guids): return " or ".join(f"Организация_Key eq guid'{guid}'" for guid in guids)
def _balance_url(config, entity, fields, snapshot_at):
 if entity not in {s['entity'] for s in REGISTER_SPECS.values()}: raise FinancePositionReadError("1C balance register is not allowed")
 period=quote(f"Balance(Period=datetime'{snapshot_at.strftime('%Y-%m-%dT%H:%M:%S')}')",safe="()=,'")
 return f"{config.base_url}{quote(entity,safe='')}/{period}?$select={quote(','.join(fields))}&$filter={quote(_org_filter(config.organization_guids))}"
def _reference_url(config, entity, fields, guids):
 if entity not in REFERENCE_ALLOWLIST: raise FinancePositionReadError("1C reference type is not allowed")
 expr=" or ".join(f"Ref_Key eq guid'{g}'" for g in guids)
 return f"{config.base_url}{quote(entity,safe='')}?$select={quote(','.join(fields))}&$filter={quote(expr)}"
def _display(value):
 text=_string(value,"Description",500)
 try: UUID(text)
 except ValueError: return text[:300]
 raise FinancePositionReadError("1C reference description must not be a GUID")
def _read_catalog(config, entity, guids, opener, budget, allow_deleted=False):
 expected=set(guids); found={}
 for start in range(0,len(expected),40):
  chunk=sorted(expected)[start:start+40]
  for rows,_ in read_odata_pages(config,_reference_url(config,entity,("Ref_Key","Description","DeletionMark"),chunk),opener=opener):
   budget[0]+=1
   if budget[0]>config.max_pages: raise FinancePositionReadError("1C reference lookups exceeded page limit")
   for raw in rows:
    key=normalize_guid(raw.get("Ref_Key"),field="Ref_Key")
    if key not in chunk or key in found: raise FinancePositionReadError("Unexpected 1C reference identity")
    mark=raw.get("DeletionMark")
    if mark not in (True,False) or (mark and not allow_deleted): raise FinancePositionReadError("1C reference deletion mark is invalid")
    found[key]=_display(raw.get("Description"))
 if set(found)!=expected: raise FinancePositionReadError("1C reference is missing")
 return found
def _read_docs(config, guids, opener, budget):
 expected=set(guids); found={}
 for start in range(0,len(expected),40):
  chunk=sorted(expected)[start:start+40]
  for rows,_ in read_odata_pages(config,_reference_url(config,DOCUMENT_CASH_WITHDRAWAL,("Ref_Key","Number","Date","DeletionMark"),chunk),opener=opener):
   budget[0]+=1
   if budget[0]>config.max_pages: raise FinancePositionReadError("1C document lookups exceeded page limit")
   for raw in rows:
    key=normalize_guid(raw.get("Ref_Key"),field="Ref_Key")
    if key not in chunk or key in found or raw.get("DeletionMark") is not False: raise FinancePositionReadError("Invalid transfer document")
    number=_string(raw.get("Number"),"Number",100); date=_string(raw.get("Date"),"Date",80)[:10]
    found[key]=f"Выемка №{number} от {date}"
 if set(found)!=expected: raise FinancePositionReadError("1C transfer document is missing")
 return found

def _org(raw, allowed):
 if not isinstance(raw,dict): raise FinancePositionReadError("1C balance row must be an object")
 value=normalize_guid(raw.get("Организация_Key"),field="Организация_Key")
 if value not in allowed: raise FinancePositionReadError("1C returned organization outside allowlist")
 return value

def read_finance_position(config=None, *, now=None, opener=None):
 config=validate_finance_position_configuration(config); at=snapshot_calendar_time(now); client=opener or build_opener(NoRedirectHandler()); budget=[0]; total=0; raw={}
 for kind in ("regular","kkm","in_transit","customer","supplier"):
  spec=REGISTER_SPECS[kind]; collected=[]
  for rows,_ in read_odata_pages(config,_balance_url(config,spec['entity'],spec['fields'],at),opener=client):
   budget[0]+=1; total+=len(rows)
   if budget[0]>config.max_pages or total>config.max_rows: raise FinancePositionReadError("1C finance position exceeded configured limits")
   collected.extend(rows)
  raw[kind]=collected
 refs={CATALOG_BANK_ACCOUNTS:set(),CATALOG_CASHES:set(),CATALOG_KKM:set(),CATALOG_COUNTERPARTIES:set()}; docs=set(); cash=[]; settlements=[]
 for r in raw['regular']:
  org=_org(r,config.organization_guids); typ=_type(r.get('БанковскийСчетКасса_Type'),frozenset({CATALOG_BANK_ACCOUNTS,CATALOG_CASHES}),'БанковскийСчетКасса_Type'); account=normalize_guid(r.get('БанковскийСчетКасса'),field='БанковскийСчетКасса'); refs[typ].add(account); cash.append(dict(source_kind='regular',organization_guid=org,account_guid=account,reference_type=typ,currency_guid=_optional_guid(r.get('Валюта_Key'),'Валюта_Key'),agreement_guid=_optional_guid(r.get('ДоговорКонтрагента_Key'),'ДоговорКонтрагента_Key'),amount=_decimal(r.get('СуммаBalance'),'СуммаBalance'),amount_currency=_decimal(r.get('СуммаВалBalance'),'СуммаВалBalance',True),transfer_document_guid=None,transfer_document_type=''))
 for r in raw['kkm']:
  org=_org(r,config.organization_guids); account=normalize_guid(r.get('КассаККМ_Key'),field='КассаККМ_Key'); refs[CATALOG_KKM].add(account); cash.append(dict(source_kind='kkm',organization_guid=org,account_guid=account,reference_type=CATALOG_KKM,currency_guid=None,agreement_guid=None,amount=_decimal(r.get('СуммаBalance'),'СуммаBalance'),amount_currency=_decimal(r.get('СуммаВалBalance'),'СуммаВалBalance',True),transfer_document_guid=None,transfer_document_type=''))
 for r in raw['in_transit']:
  org=_org(r,config.organization_guids); typ=_type(r.get('Касса_Type'),MONEY_REFERENCE_TYPES,'Касса_Type'); account=normalize_guid(r.get('Касса'),field='Касса'); refs[typ].add(account); doc=_optional_guid(r.get('ДокументПередачи'),'ДокументПередачи'); dtype=''
  if doc: dtype=_type(r.get('ДокументПередачи_Type'),DOCUMENT_REFERENCE_TYPES,'ДокументПередачи_Type'); docs.add(doc)
  cash.append(dict(source_kind='in_transit',organization_guid=org,account_guid=account,reference_type=typ,currency_guid=None,agreement_guid=None,amount=_decimal(r.get('СуммаBalance'),'СуммаBalance'),amount_currency=_decimal(r.get('СуммаВалBalance'),'СуммаВалBalance',True),transfer_document_guid=doc,transfer_document_type=dtype))
 for side in ('customer','supplier'):
  for r in raw[side]:
   org=_org(r,config.organization_guids); party=normalize_guid(r.get('Контрагент_Key'),field='Контрагент_Key'); refs[CATALOG_COUNTERPARTIES].add(party); st=_string(r.get('ТипРасчетов'),'ТипРасчетов',120); amount=_decimal(r.get('СуммаBalance'),'СуммаBalance'); classification=classify_settlement(side,st,amount); order_field='Заказ' if side=='customer' else 'Заказ_Key'
   settlements.append(dict(side=side,settlement_type_raw=st,management_classification=classification,organization_guid=org,counterparty_guid=party,agreement_guid=_optional_guid(r.get('Договор_Key'),'Договор_Key'),document_guid=_optional_guid(r.get('Документ'),'Документ'),document_type=(r.get('Документ_Type') or '')[:120],order_guid=_optional_guid(r.get(order_field),order_field),order_type=((r.get('Заказ_Type') or '')[:120] if side=='customer' else ''),amount=amount,amount_currency=_decimal(r.get('СуммаВалBalance'),'СуммаВалBalance',True),amount_reg=_decimal(r.get('СуммаРегBalance'),'СуммаРегBalance',True),is_sign_anomaly=classification=='sign_anomaly'))
 names={e:_read_catalog(config,e,g,client,budget,allow_deleted=(e==CATALOG_COUNTERPARTIES)) for e,g in refs.items()}; doc_names=_read_docs(config,docs,client,budget)
 cash_rows=[]
 for r in cash:
  ident=_identity('cash',(r['source_kind'],r['organization_guid'],r['reference_type'],r['account_guid'],r['currency_guid'] or '',r['agreement_guid'] or '',r['transfer_document_guid'] or '',r['transfer_document_type']))
  cash_rows.append(CashPositionSourceRow(**r,display_name=names[r['reference_type']][r['account_guid']],transfer_document_display=doc_names.get(r['transfer_document_guid'],'') if r['transfer_document_guid'] else '',source_identity=ident))
 settlement_rows=[]
 for r in settlements:
  ident=_identity('settlement',(r['side'],r['organization_guid'],r['settlement_type_raw'],r['counterparty_guid'],r['agreement_guid'] or '',r['document_guid'] or '',r['document_type'],r['order_guid'] or '',r['order_type']))
  settlement_rows.append(SettlementPositionSourceRow(**r,counterparty_name=names[CATALOG_COUNTERPARTIES][r['counterparty_guid']],source_identity=ident))
 if len({r.source_identity for r in cash_rows})!=len(cash_rows) or len({r.source_identity for r in settlement_rows})!=len(settlement_rows): raise FinancePositionReadError("1C Balance contains duplicate dimensions")
 fetched=datetime.now(timezone.utc).replace(microsecond=0); diagnostics={'register_rows':{k:len(raw[k]) for k in REGISTER_SPECS},'total_rows':total,'page_count':budget[0],'sign_anomaly_count':sum(r.is_sign_anomaly for r in settlement_rows)}
 return FinancePositionSourceSnapshot(at,at.tzinfo.key,fetched,tuple(cash_rows),tuple(settlement_rows),diagnostics)
