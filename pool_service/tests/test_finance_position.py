from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import unquote
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone
from pool_service.finance_imports.finance_position import _persist_snapshot, get_finance_position, sync_finance_position
from pool_service.finance_imports.odata_finance_position import CashPositionSourceRow, FinancePositionReadError, FinancePositionSourceSnapshot, ODataConfig, REGISTER_SPECS, SettlementPositionSourceRow, _balance_url, calendar_timezone, classify_settlement, snapshot_calendar_time
from pool_service.finance_position_models import OneCFinancePositionSnapshot
from pool_service.models import Organization

ORG="11111111-1111-1111-1111-111111111111"; ACCOUNT="22222222-2222-2222-2222-222222222222"; PARTY="33333333-3333-3333-3333-333333333333"
def cash(kind, amount, identity):
 return CashPositionSourceRow(kind,ORG,ACCOUNT,"Catalog_КассыККМ" if kind=="kkm" else "Catalog_Кассы",f"Synthetic {kind}",None,None,Decimal(amount),None,source_identity=identity)
def settle(side, raw_type, amount, identity, reg="0"):
 classification=classify_settlement(side,raw_type,Decimal(amount)); return SettlementPositionSourceRow(side,raw_type,classification,ORG,PARTY,"Synthetic party",None,None,"",None,"",Decimal(amount),None,Decimal(reg),classification=="sign_anomaly",identity)
def source(cash_rows=(), settlement_rows=(), minute=0):
 at=datetime(2026,9,12,13,minute,tzinfo=timezone.utc); return FinancePositionSourceSnapshot(at,"Asia/Barnaul",at,tuple(cash_rows),tuple(settlement_rows),{"sign_anomaly_count":sum(r.is_sign_anomaly for r in settlement_rows)})

class ClassificationTests(TestCase):
 def test_sign_rules(self):
  cases=[("customer","Долг","1","receivable"),("customer","Аванс","-1","customer_advance"),("supplier","Долг","1","payable"),("supplier","Аванс","-1","supplier_advance"),("customer","Долг","-1","sign_anomaly"),("customer","Аванс","1","sign_anomaly"),("supplier","Долг","-1","sign_anomaly"),("supplier","Аванс","1","sign_anomaly")]
  for side,raw,amount,expected in cases: self.assertEqual(classify_settlement(side,raw,Decimal(amount)),expected)
 def test_zero_is_not_anomaly(self): self.assertEqual(classify_settlement("customer","Долг",Decimal("0")),"zero")

class CalendarTests(TestCase):
 @override_settings(ONEC_ODATA_CALENDAR_TIMEZONE="Asia/Barnaul")
 def test_explicit_barnaul_calendar(self):
  local=snapshot_calendar_time(datetime(2026,9,12,13,36,tzinfo=timezone.utc)); self.assertEqual(local.tzinfo.key,"Asia/Barnaul"); self.assertEqual(local.strftime("%Y-%m-%d %H:%M:%S"),"2026-09-12 20:36:00"); self.assertEqual(calendar_timezone().key,"Asia/Barnaul")
 @override_settings(ONEC_ODATA_CALENDAR_TIMEZONE="Invalid/Zone")
 def test_invalid_zone_fails_closed(self):
  with self.assertRaises(FinancePositionReadError): calendar_timezone()
 def test_all_five_registers_use_encoded_balance_at_calendar_time(self):
  config=ODataConfig("https://example.test/odata/standard.odata/","u","p",(ORG,),5,10,100); at=datetime(2026,9,12,20,36,tzinfo=timezone.utc)
  self.assertEqual(len(REGISTER_SPECS),5)
  for spec in REGISTER_SPECS.values():
   decoded=unquote(_balance_url(config,spec["entity"],spec["fields"],at)); self.assertIn(spec["entity"],decoded); self.assertIn("Balance(Period=datetime'2026-09-12T20:36:00')",decoded); self.assertIn(ORG,decoded)

@override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID="1")
class PersistenceTests(TestCase):
 def setUp(self):
  self.org=Organization.objects.create(id=1,name="Synthetic",paid_until=dj_timezone.now()); self.user=User.objects.create_user("fp-owner")
 def test_totals_ignore_sum_reg_and_keep_advance_separate(self):
  snap=_persist_snapshot(self.org,self.user,source([cash("regular","100","r"),cash("kkm","20","k"),cash("in_transit","5","t")],[settle("customer","Долг","50","cd","9000000"),settle("customer","Аванс","-7","ca","8000000"),settle("supplier","Долг","11","sd","7000000"),settle("supplier","Аванс","-13","sa","6000000"),settle("customer","Долг","-3","x","5000000")]))
  result=get_finance_position(self.org,now=snap.fetched_at); self.assertEqual(result["cash_total"],Decimal("125.00")); self.assertEqual(result["receivables"],Decimal("50.00")); self.assertEqual(result["customer_advances"],Decimal("7.00")); self.assertEqual(result["payables"],Decimal("11.00")); self.assertEqual(result["supplier_advances"],Decimal("13.00")); self.assertEqual(result["sign_anomaly_count"],1); self.assertEqual(result["sign_anomaly_amount"],Decimal("3.00")); self.assertEqual(result["calculated_position"],Decimal("170.00")); self.assertLess(result["calculated_position"],Decimal("1000"))
 def test_atomic_activation_keeps_single_active_snapshot(self):
  first=_persist_snapshot(self.org,self.user,source([cash("regular","1","a")])); second=_persist_snapshot(self.org,self.user,source([cash("regular","2","b")],minute=1)); first.refresh_from_db(); self.assertFalse(first.is_active); self.assertTrue(second.is_active); self.assertEqual(OneCFinancePositionSnapshot.objects.filter(organization=self.org,is_active=True).count(),1)
 def test_failed_collection_preserves_old_active(self):
  old=_persist_snapshot(self.org,self.user,source([cash("regular","7","old")]))
  config=ODataConfig("https://example.test/odata/standard.odata/","u","p",(ORG,),5,10,100)
  with patch("pool_service.finance_imports.finance_position.read_finance_position",side_effect=FinancePositionReadError("synthetic")):
   with self.assertRaises(FinancePositionReadError): sync_finance_position(self.org,self.user,config=config)
  old.refresh_from_db(); self.assertTrue(old.is_active); self.assertEqual(OneCFinancePositionSnapshot.objects.count(),1)
 def test_same_counterparty_can_keep_debt_and_advance(self):
  snap=_persist_snapshot(self.org,self.user,source(settlement_rows=[settle("customer","Долг","10","d"),settle("customer","Аванс","-4","a")]))
  self.assertEqual(snap.settlement_rows.count(),2); result=get_finance_position(self.org,now=snap.fetched_at); self.assertEqual(result["receivables"],Decimal("10.00")); self.assertEqual(result["customer_advances"],Decimal("4.00"))
