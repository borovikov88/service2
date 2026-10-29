from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from pool_service.services.crm_document_allocations import Allocation, preview_allocations
from pool_service.tests.test_onec_service_document_preview import DocumentPreviewTests


class DocumentAllocationTests(SimpleTestCase):
    # Reuse the actual adapter fixture, not a ledger mock.
    setUp = DocumentPreviewTests.setUp
    reader = DocumentPreviewTests.reader
    read = DocumentPreviewTests.read
    def proposal(self, snapshot=None, items=None, **kwargs):
        snapshot = snapshot or self.read()
        items = items or [Allocation(1, snapshot.lines[0].content_hash, date(2026,1,1), D('0.5'), D('60'))]
        return preview_allocations(snapshot, expected_snapshot_hash=kwargs.get('expected', snapshot.snapshot_hash), allocations=items)

    def test_partial_quantity_is_not_month_completion_or_payment(self):
        result = self.proposal()
        self.assertFalse(result.binding_allowed)
        self.assertIsNone(result.realized_amount); self.assertIsNone(result.paid_amount)
        self.assertEqual(result.confirmed_months, ())
        self.assertIn('period_coverage_unverified', result.blockers)

    def test_two_units_require_two_explicit_months_and_no_inferred_coverage(self):
        snapshot = self.read(self.reader(lines=[{**self.line, 'Количество':2,'Всего':240}]))
        items = [Allocation(1,snapshot.lines[0].content_hash,date(2026,m,1),D(1),D(120)) for m in (1,2)]
        result = self.proposal(snapshot,items)
        self.assertIn('future_period',result.blockers)
        self.assertEqual(result.confirmed_months,())
        self.assertEqual(result.proposal_hash,self.proposal(snapshot,list(reversed(items))).proposal_hash)

    def test_changed_snapshot_or_position_requires_reconcile(self):
        with self.assertRaises(ValidationError): self.proposal(expected='stale')
        snapshot=self.read(); item=self.proposal(snapshot).allocations[0]
        for changed in (replace(item,line_number=2),replace(item,content_hash='changed')):
            with self.assertRaises(ValidationError): self.proposal(snapshot,[changed])

    def test_duplicate_overallocation_invalid_month_and_decimal_rejected(self):
        snapshot=self.read(); item=self.proposal(snapshot).allocations[0]
        for items in ([item,item],[replace(item,quantity=D(2))],[replace(item,amount=D(121))],
                [replace(item,month=date(2026,1,2))],[replace(item,amount=D('NaN'))],
                [replace(item,quantity=True)],[replace(item,amount=D(-1))]):
            with self.subTest(items=repr(items)),self.assertRaises(ValidationError): self.proposal(snapshot,items)

    def test_future_deleted_unposted_document_never_credited(self):
        snapshot=self.read(self.reader(header={**self.header,'Date':'2026-02-01T00:00:00','Posted':False,'DeletionMark':True}))
        result=self.proposal(snapshot)
        self.assertTrue({'future_document','deleted_document','unposted_document'} <= set(result.blockers))
        self.assertFalse(result.binding_allowed)

    def test_equivalent_decimal_scale_replay_is_only_proposal_digest(self):
        snapshot=self.read(); item=self.proposal(snapshot).allocations[0]
        self.assertEqual(self.proposal(snapshot).proposal_hash,
            self.proposal(snapshot,[replace(item,quantity=D('0.50'),amount=D('60.00'))]).proposal_hash)
        self.assertIn('durable_consumption_unavailable',self.proposal(snapshot).blockers)

    def test_decimal_context_cannot_round_away_overallocation(self):
        from decimal import localcontext
        snapshot=self.read(); item=self.proposal(snapshot).allocations[0]
        for precision in (2, 28, 50):
            with localcontext() as ctx:
                ctx.prec=precision
                with self.assertRaises(ValidationError):
                    self.proposal(snapshot,[replace(item,amount=D('120.000000000000001'))])
                with self.assertRaises(ValidationError):
                    self.proposal(snapshot,[replace(item,amount=D('60.000000000000001')),
                        replace(item,month=date(2026,2,1),amount=D('60'))])
                a=self.proposal(snapshot,[replace(item,amount=D('60.000000000000001'))])
                b=self.proposal(snapshot,[replace(item,amount=D('60'))])
                self.assertNotEqual(a.proposal_hash,b.proposal_hash)
        with self.assertRaises(ValidationError):
            self.proposal(snapshot,[replace(item,amount=D('120.00000000000000000000000001'))])
