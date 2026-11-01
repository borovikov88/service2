"""Bounded board prefixes; no production fixtures or writes outside test DB."""
import json
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.crm_commercial_models import CrmDeal, CrmPipeline
from pool_service.models import Organization


class ColumnLoadingTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['plan_type', 'paid_until'])
        self.client.force_login(self.user)
        self.stage = self.pipeline.stages.get(code='need')
        self.target = self.pipeline.stages.get(code='technical')
        self.rows = self.seed(self.stage, 107)
        self.other = self.seed(self.target, 3)

    def seed(self, stage, count):
        return [CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=stage, manager=self.user, client=self.client_record, title=f'Synthetic board {stage.pk}-{n}',
            amount=Decimal('10.01'), amount_basis='agreed', currency='RUB') for n in range(count)]

    def data(self, **params):
        return self.client.get(reverse('crm_live_data'), {'org_id':self.org.pk,
            'pipeline':self.pipeline.pk, 'layout':'board', **params})

    def test_columns_are_independent_bounded_and_totals_cover_all_rows(self):
        first = self.data().json()
        self.assertEqual(len(first['deals']), 53)
        self.assertEqual(len({row['id'] for row in first['deals']}), 53)
        totals = next(row for row in first['stage_totals'] if row['stage_id']==self.stage.pk)
        self.assertEqual((totals['count'],totals['amount']), (107,'1071.07'))
        second = self.data(column_limits=json.dumps({str(self.stage.pk):100})).json()
        self.assertEqual(len(second['deals']), 103)
        self.assertTrue(set(row['id'] for row in first['deals']) <= set(row['id'] for row in second['deals']))
        final = self.data(column_limits=json.dumps({str(self.stage.pk):150})).json()
        self.assertEqual({row['id'] for row in final['deals']}, {r.pk for r in self.rows+self.other})
        self.assertFalse(next(w for w in final['column_windows'] if w['stage_id']==self.stage.pk)['has_more'])
        self.assertEqual(final, self.data(column_limits=json.dumps({str(self.stage.pk):150})).json())

    def test_mutation_between_reads_replaces_prefix_without_gaps_or_duplicates(self):
        before = self.data().json()
        top = before['deals'][0]['id']
        row=CrmDeal.objects.get(pk=top); row.stage=self.target; row.revision=1; row.save()
        row=CrmDeal.objects.get(pk=self.rows[-2].pk); row.is_archived=True; row.revision=1; row.save()
        self.seed(self.stage, 1)
        data = self.data(column_limits=json.dumps({str(self.stage.pk):150})).json()
        expected = set(CrmDeal.objects.filter(organization=self.org,is_archived=False).values_list('pk',flat=True))
        self.assertEqual({r['id'] for r in data['deals']}, expected)
        self.assertEqual(len(data['deals']), len(expected))
        self.assertEqual(next(r for r in data['deals'] if r['id']==top)['stage_id'], self.target.pk)

    def test_filter_counts_and_table_pagination_remain_scoped(self):
        filtered = self.data(q=f'Synthetic board {self.target.pk}-', owner='me').json()
        self.assertEqual(len(filtered['deals']), 3)
        self.assertEqual(sum(r['count'] for r in filtered['stage_totals']), 3)
        first=self.data(layout='page').json(); second=self.data(layout='page',page=2).json()
        self.assertEqual(len(first['deals']),50); self.assertEqual(len(second['deals']),50)
        self.assertFalse({r['id'] for r in first['deals']} & {r['id'] for r in second['deals']})
        self.assertTrue(second['has_previous']); self.assertEqual(second['column_windows'],[])

    def test_invalid_foreign_stage_and_excessive_limits_rejected(self):
        other=CrmPipeline.objects.get(organization=self.org,code='service').stages.first()
        for value in [{str(other.pk):100},{str(self.stage.pk):201},{str(self.stage.pk):True},[],{str(self.stage.pk):'100'},'{']:
            with self.subTest(value=value):
                response=self.data(column_limits=value if isinstance(value,str) else json.dumps(value))
                self.assertEqual(response.status_code,400)
        self.assertEqual(self.data(org_id=Organization.objects.create(name='Other synthetic').pk).status_code,403)
        self.client.logout();self.assertEqual(self.data().status_code,401)

    def test_cap_is_explicit_and_html_bootstraps_board_but_table_does_not(self):
        self.seed(self.stage, 100)
        data=self.data(column_limits=json.dumps({str(self.stage.pk):200})).json()
        window=next(w for w in data['column_windows'] if w['stage_id']==self.stage.pk)
        self.assertEqual((window['loaded'],window['has_more'],window['can_expand']),(200,True,False))
        for view,layout in [('kanban','board'),('table','page')]:
            response=self.client.get(reverse('crm_live_workspace'),{'org_id':self.org.pk,'pipeline':self.pipeline.pk,'view':view})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.context['crm_bootstrap']['layout'],layout)
