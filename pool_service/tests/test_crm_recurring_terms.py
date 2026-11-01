from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal, localcontext
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from pool_service.services.crm_recurring_terms import ServicePeriodDraft as P, preview_terms


class RecurringTermsPreviewTests(SimpleTestCase):
    def preview(self, **changes):
        args=dict(start=date(2030,1,15),end=date(2030,12,20),complete_period_list=True,
                  renewal_review_on=date(2030,11,1),periods=[P(date(2030,1,1),Decimal('0.5'),'мес',Decimal('100.25'))])
        args.update(changes)
        return preview_terms(**args)

    def test_partial_and_multiple_quantity_do_not_create_months(self):
        p=self.preview(periods=[P(date(2030,1,1),Decimal('0.5'),'мес',Decimal('100.25')),
                                P(date(2030,3,1),Decimal('2'),'шт',Decimal('200.75'))])
        self.assertEqual([r.month.month for r in p.periods],[1,3])
        self.assertEqual(p.contract_total,Decimal('301.00'))
        self.assertFalse(p.can_activate);self.assertIsNone(p.paid_amount);self.assertIsNone(p.realized_amount)

    def test_unknown_not_zero_and_indefinite_not_annualized(self):
        p=self.preview(end=None,complete_period_list=False)
        self.assertEqual(p.listed_amount,Decimal('100.25'));self.assertIsNone(p.contract_total)
        self.assertIsNone(self.preview(complete_period_list=False).contract_total)
        p=self.preview(periods=[P(date(2030,1,1),Decimal(1),'мес',None)])
        self.assertIsNone(p.listed_amount);self.assertIsNone(p.contract_total)
        self.assertEqual(self.preview(periods=[P(date(2030,1,1),Decimal(1),'мес',Decimal(0))]).contract_total,0)

    def test_hash_order_scale_and_decimal_context(self):
        rows=[P(date(2030,1,1),Decimal('1.0'),' мес ',Decimal('12345678.90')),
              P(date(2030,2,1),Decimal('2'),'шт',Decimal('0.01'))]
        expected=self.preview(periods=rows)
        with localcontext() as ctx:
            ctx.prec=2
            actual=self.preview(periods=list(reversed(rows)))
        self.assertEqual(actual.proposal_hash,expected.proposal_hash)
        self.assertEqual(actual.contract_total,Decimal('12345678.91'))
        rows[0]=P(date(2030,1,1),Decimal('1.00'),'мес',Decimal('12345678.900'))
        self.assertEqual(self.preview(periods=rows).proposal_hash,expected.proposal_hash)
        with self.assertRaises(FrozenInstanceError):actual.periods[0].unit='changed'

    def test_duplicate_outside_and_missing_explicit_month_rejected(self):
        row=P(date(2030,1,1),Decimal(1),'мес',Decimal(1))
        for rows in ([row,row],[P(date(2029,12,1),Decimal(1),'мес',Decimal(1))],
                     [P(date(2031,1,1),Decimal(1),'мес',Decimal(1))],
                     [P(date(2030,1,2),Decimal(1),'мес',Decimal(1))],[]):
            with self.subTest(rows=rows),self.assertRaises(ValidationError):self.preview(periods=rows)

    def test_invalid_dates_and_full_indefinite_rejected(self):
        for changes in ({'end':date(2029,1,1)},{'start':datetime(2030,1,1)},
                        {'end':None},{'complete_period_list':1},{'renewal_review_on':None}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):self.preview(**changes)

    def test_bad_decimals_units_and_subkopecks_rejected(self):
        for quantity in (Decimal(0),Decimal('-1'),Decimal('NaN'),Decimal('Infinity'),1.0,Decimal('1e30')):
            with self.subTest(quantity=quantity),self.assertRaises(ValidationError):
                self.preview(periods=[P(date(2030,1,1),quantity,'мес',Decimal(1))])
        for amount in (Decimal('-1'),Decimal('NaN'),Decimal('0.001'),1.0):
            with self.subTest(amount=amount),self.assertRaises(ValidationError):
                self.preview(periods=[P(date(2030,1,1),Decimal(1),'мес',amount)])
        with self.assertRaises(ValidationError):self.preview(periods=[P(date(2030,1,1),Decimal(1),' ',None)])

    def test_material_changes_change_proposal_and_never_authorize(self):
        original=self.preview()
        for changes in ({'renewal_review_on':date(2030,10,1)}, {'complete_period_list':False},
                        {'periods':[P(date(2030,1,1),Decimal(1),'мес',Decimal('100.25'))]}):
            p=self.preview(**changes)
            self.assertNotEqual(p.proposal_hash,original.proposal_hash)
            self.assertIn('contract_scope_unverified',p.blockers)
            self.assertFalse(p.can_activate)
