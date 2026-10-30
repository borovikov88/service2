from copy import deepcopy
from datetime import date
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service import onec_service_document_preview as adapter
from pool_service.onec_diagnostic import OneCDiagnosticError
from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service.tests.test_onec_diagnostic_universal import SequencedOpener

REF, ORG, CLIENT, CONTRACT, ITEM, CURRENCY = [f'10000000-0000-4000-8000-{n:012d}' for n in range(1,7)]

def metadata():
    types=[]; sets=[]
    for index,(entity, fields) in enumerate(((adapter.HEADER,adapter.HEADER_FIELDS),(adapter.LINES,adapter.LINE_FIELDS))):
        types.append(f'<EntityType Name="T{index}">'+''.join(f'<Property Name="{key}" Type="{kind}" />' for key,kind in fields.items())+'</EntityType>')
        sets.append(f'<EntitySet Name="{entity}" EntityType="Test.T{index}" />')
    return ('<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices><Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="Test">'+''.join(types)+'<EntityContainer Name="C">'+''.join(sets)+'</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>').encode()

class DocumentPreviewTests(SimpleTestCase):
    def setUp(self):
        self.config=ODataConfig(base_url='https://1c.example.test/odata/standard.odata/', username='synthetic',password='synthetic',organization_guids=(ORG,),timeout_seconds=5,max_pages=5,max_rows=500)
        self.header={'Ref_Key':REF,'Организация_Key':ORG,'Контрагент_Key':CLIENT,'Договор_Key':CONTRACT,'ВалютаДокумента_Key':CURRENCY,'Date':'2026-01-15T00:00:00','Posted':True,'DeletionMark':False,'DataVersion':'constant'}
        self.line={'Ref_Key':REF,'LineNumber':1,'ИдентификаторСтроки':'','КлючСвязи':1,'Номенклатура_Key':ITEM,'Количество':1,'Сумма':'120.00','Всего':'120.00','СуммаНДС':0}
    def reader(self, lines=None, repeated=None, header=None):
        header=header or self.header
        lines=lines if lines is not None else [self.line]
        repeated=lines if repeated is None else repeated
        return SequencedOpener([{'value':[header]},{'value':[header]},{'value':lines},{'value':[header]},{'value':repeated},{'value':[header]}])
    def read(self, opener=None, **kwargs):
        return adapter.read_service_document_snapshot(self.config,document_ref=REF,organization_ref=ORG,client_ref=CLIENT,contract_ref=CONTRACT,as_of_date=date(2026,1,31),metadata_raw=kwargs.pop('metadata_raw',metadata()),opener=opener or self.reader(),**kwargs)
    def test_snapshot_no_accounting_claim(self):
        opener=self.reader(); result=self.read(opener)
        self.assertEqual(len(opener.urls),6)
        self.assertEqual(result.lines[0].amount,Decimal('120'))
        self.assertFalse(result.binding_allowed)
        self.assertFalse(result.lines[0].stable_identity_proven)
        self.assertIsNone(result.realized_amount); self.assertIsNone(result.paid_amount)
        self.assertEqual(result.confirmed_months,())
    def test_schema_rejected_before_transport(self):
        opener=self.reader()
        with self.assertRaises(OneCDiagnosticError): self.read(opener,metadata_raw=metadata().replace(b'Edm.Double',b'Edm.String'))
        self.assertEqual(opener.urls,[])
    def test_wrong_contract_client_and_org_rejected(self):
        for key in ('Договор_Key','Контрагент_Key','Организация_Key'):
            with self.subTest(key=key), self.assertRaises(OneCDiagnosticError):
                self.read(self.reader(header={**self.header,key:ITEM}))
    def test_child_scope_and_duplicate_position_rejected(self):
        for lines in ([{**self.line,'Ref_Key':ITEM}],[self.line,self.line]):
            with self.subTest(lines=len(lines)),self.assertRaises(OneCDiagnosticError): self.read(self.reader(lines=lines))
    def test_constant_version_does_not_hide_change(self):
        with self.assertRaisesMessage(OneCDiagnosticError,'DOCUMENT_CHANGED_DURING_READ'):
            self.read(self.reader(repeated=[{**self.line,'Сумма':121}]))
    def test_renumber_requires_review_not_new_credit(self):
        previous=self.read()
        result=self.read(self.reader(lines=[{**self.line,'LineNumber':2}]),previous=previous)
        self.assertIn('possible_renumbering_requires_review',result.warnings)
        self.assertFalse(result.binding_allowed)
    def test_changed_amount_and_unchanged_snapshot(self):
        previous=self.read()
        self.assertIn('unchanged_preview',self.read(previous=previous).warnings)
        result=self.read(self.reader(lines=[{**self.line,'Сумма':121}]),previous=previous)
        self.assertIn('source_changed_requires_review',result.warnings)
        self.assertNotIn('possible_renumbering_requires_review',result.warnings)
    def test_future_deleted_unposted_and_correction_are_explicit(self):
        header={**self.header,'Date':'2026-02-01T00:00:00','Posted':False,'DeletionMark':True}
        result=self.read(self.reader(header=header,lines=[{**self.line,'Количество':-1}]))
        self.assertTrue({'future_document','deleted_document','unposted_document','correction_requires_review'} <= set(result.warnings))
    def test_invalid_scalar_values_rejected(self):
        for key,value in [('Сумма','NaN'),('Количество',True),('ИдентификаторСтроки','x'*257),('LineNumber',True)]:
            with self.subTest(key=key),self.assertRaises(OneCDiagnosticError): self.read(self.reader(lines=[{**self.line,key:value}]))
    def test_equal_lines_are_ambiguous(self):
        result=self.read(self.reader(lines=[self.line,{**self.line,'LineNumber':2}]))
        self.assertIn('ambiguous_equal_lines',result.warnings)
    def test_empty_document_has_no_zero_claim(self):
        result=self.read(self.reader(lines=[]))
        self.assertIn('empty_document',result.warnings); self.assertIsNone(result.realized_amount)
    def test_wrong_previous_scope_rejected(self):
        from dataclasses import replace
        previous=replace(self.read(),contract_ref=ITEM)
        with self.assertRaisesMessage(OneCDiagnosticError,'PREVIOUS_DOCUMENT_SCOPE_MISMATCH'): self.read(previous=previous)
    def test_parent_scope_is_rechecked_for_tabular_part(self):
        opener=self.reader()
        opener.payloads[1]={'value':[{**self.header,'Организация_Key':ITEM}]}
        with self.assertRaises(OneCDiagnosticError): self.read(opener)
        self.assertEqual(len(opener.urls),2)
    def test_truncated_document_rejected(self):
        lines=[{**self.line,'LineNumber':n} for n in range(1,202)]
        with self.assertRaisesMessage(OneCDiagnosticError,'DOCUMENT_SNAPSHOT_INCOMPLETE'):
            self.read(self.reader(lines=lines))
    def test_missing_transport_rejected_without_network(self):
        with self.assertRaisesMessage(OneCDiagnosticError,'DOCUMENT_PREVIEW_INPUT_INVALID'):
            adapter.read_service_document_snapshot(self.config,document_ref=REF,organization_ref=ORG,client_ref=CLIENT,contract_ref=CONTRACT,as_of_date=date(2026,1,31),metadata_raw=metadata(),opener=None)
    def test_int64_string_wire_representation(self):
        result=self.read(self.reader(lines=[{**self.line,'LineNumber':'1','КлючСвязи':'1'}]))
        self.assertEqual(result.lines[0].number,1)
        with self.assertRaises(OneCDiagnosticError):
            self.read(self.reader(lines=[self.line,{**self.line,'LineNumber':'1'}]))
    def test_invalid_int64_wire_values(self):
        for value in ('1.0','1e0',True,2**63,'-9223372036854775809'):
            with self.subTest(value=value),self.assertRaises(OneCDiagnosticError):
                self.read(self.reader(lines=[{**self.line,'КлючСвязи':value}]))
    def test_incomplete_second_read_rejected(self):
        from unittest.mock import patch
        from pool_service.onec_diagnostic_universal import query_1c_rows
        for field in ('complete','truncated'):
            calls=[]
            def wrapped(*args,**kwargs):
                result=query_1c_rows(*args,**kwargs)
                calls.append(args[1])
                if len(calls)==3:
                    result[field]=(field=='truncated')
                return result
            with self.subTest(field=field),patch.object(adapter,'query_1c_rows',side_effect=wrapped):
                with self.assertRaisesMessage(OneCDiagnosticError,'DOCUMENT_SNAPSHOT_INCOMPLETE'): self.read()
