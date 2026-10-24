"""Synthetic proposals only: no real shop facts or prices."""
from copy import deepcopy
from decimal import Decimal
import json
from django.test import SimpleTestCase
from django.template.loader import render_to_string
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_estimate_catalog import feed, offer
from .test_pool_estimates import INPUTS, block


def target(s, kind, tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind)


class PreliminaryCoreTests(SimpleTestCase):
    def test_unknown_flow_power_and_point_proposed_but_known_limits_block(self):
        s=generate({**INPUTS,'power':'230','required_head':'8'})
        c=catalog.parse_catalog(feed(
            offer(1,'Насос Unknown','100'),
            offer(2,'Насос TooSmall 2 м3/ч 220В','1'),
            offer(3,'Насос WrongVoltage 10 м3/ч 380В','2'),
            offer(4,'Насос WrongHead 10m3/h*4m 220V','3'),
            offer(5,'Насос SPA 20 м3/ч','4',description='Без префильтра, гидромассаж'),
            offer(6,'Фильтр Unknown','200')))
        result,_=catalog.auto_select(s,c)
        for kind,pid in [('pump','1'),('filter','6')]:
            row=target(result,kind)
            self.assertEqual(row['catalog']['id'],pid)
            self.assertEqual(row['selection_status'],'check_core')
            self.assertIn('Проверить:',row['compatibility']['message'])
            self.assertIn('производительность',row['selection_note'])
            self.assertIn(c['products'][pid]['url'],row['selection_note'])
        self.assertIsNone(target(result,'pump')['specs']['flow'])
        self.assertIsNone(target(result,'filter')['specs']['filter_flow'])
        self.assertEqual(Decimal(result['variants']['economy']['total'])-Decimal(s['variants']['economy']['total']),Decimal('300'))
        twice,count=catalog.auto_select(evaluate(json.loads(json.dumps(result))),c)
        self.assertEqual(count,0)
        self.assertEqual(twice['variants'],result['variants'])
        printed=render_to_string('pool_service/estimates/print.html',{'version':{'snapshot':result,'number':1}})
        self.assertIn('Проверить:',printed)
        self.assertIn('производительность модели',printed)

    def test_complete_evidence_beats_cheaper_incomplete_and_capacity_comparable(self):
        c=catalog.parse_catalog(feed(
            offer(1,'Насос Unknown','1'),
            offer(2,'Насос Good 8m3/h*8m 220V','100'),
            offer(3,'Насос Giant 80m3/h*8m 220V','2'),
            offer(4,'Фильтр Unknown','1'),offer(5,'Фильтр Good 8 м3/ч','200')))
        result,_=catalog.auto_select(generate({**INPUTS,'power':'230','required_head':'8'}),c)
        for tier in result['variants']:
            self.assertEqual(target(result,'pump',tier)['catalog']['id'],'2')
            self.assertEqual(target(result,'filter',tier)['catalog']['id'],'5')

    def test_full_power_for_each_heater_unknown_capacity_is_not_split(self):
        s=generate({**INPUTS,'heating':'electric','heating_types':['electric','exchanger','heatpump'],'heating_power':'20','power':'230'})
        c=catalog.parse_catalog(feed(
            offer(1,'Электронагреватель Unknown 220В','100'),
            offer(2,'Электронагреватель Small 10 кВт 220В','1'),
            offer(3,'Теплообменник Good 20 кВт','200'),
            offer(4,'Тепловой насос Good 20 кВт 220В','300')))
        result,_=catalog.auto_select(s,c)
        rows=[r for r in block(result)['rows'] if r['kind']=='heater']
        self.assertEqual([r['catalog']['id'] for r in rows],['1','3','4'])
        self.assertIn('мощность модели',rows[0]['selection_note'])
        self.assertEqual(Decimal(result['heating']['power']),Decimal('20'))
        before_work=[r for r in block(s)['rows'] if r['kind']=='work']
        self.assertEqual([r for r in block(result)['rows'] if r['kind']=='work'],before_work)
        self.assertEqual(Decimal(result['variants']['economy']['total'])-Decimal(s['variants']['economy']['total']),Decimal('600'))

    def test_conflicting_title_values_never_become_missing_fact_candidates(self):
        c=catalog.parse_catalog(feed(offer(1,'Насос Conflict 8 м3/ч 12 м3/ч 220В'),
            offer(2,'Электронагреватель Conflict 6 кВт 20 кВт 220В'),
            offer(3,'Насос ConflictVoltage 10 м3/ч 220В 380В')))
        self.assertTrue(all(p['specification_conflict'] for p in c['products'].values()))
        result,_=catalog.auto_select(generate({**INPUTS,'heating':'electric','heating_power':'20'}),c)
        self.assertIsNone(target(result,'pump')['catalog'])
        self.assertIsNone(target(result,'heater')['catalog'])

    def test_live_conflict_or_new_known_limit_withdraws_only_untouched_auto(self):
        c=catalog.parse_catalog(feed(offer(1,'Насос Good 8m3/h*8m 220V','100')))
        initial,_=catalog.auto_select(generate({**INPUTS,'power':'230','required_head':'8'}),c)
        for mode in ['flow','conflict']:
            s=deepcopy(initial);current=deepcopy(c)
            if mode=='flow':s=recalculate(s,{**s['inputs'],'length':'20'})
            else:current['products']['1'].update(specification_conflict=True,specification_note='Конфликт обновления')
            result,_=catalog.auto_select(s,current)
            self.assertIsNone(target(result,'pump')['catalog'])
            self.assertIsNone(target(result,'pump')['amount'])
            self.assertIn('отклонён',target(result,'pump')['selection_note'])
            self.assertEqual(Decimal(s['variants']['economy']['total'])-Decimal(result['variants']['economy']['total']),Decimal('100'))
            target(s,'pump')['catalog']['selection']='manual'
            manual,_=catalog.auto_select(s,current)
            self.assertEqual(target(manual,'pump')['catalog']['id'],'1')
            self.assertEqual(target(manual,'pump')['price'],'100.00')

    def test_replacement_recalculation_manual_values_and_frozen_price(self):
        c=catalog.parse_catalog(feed(offer(1,'Насос Unknown','100')))
        original,_=catalog.auto_select(generate({**INPUTS,'power':'230','required_head':'8'}),c)
        current=catalog.parse_catalog(feed(offer(1,'Насос Good 8m3/h*8m 220V','999')),checked_at='2026-10-21T00:00:00+00:00')
        updated,_=catalog.auto_select(original,current)
        row=target(updated,'pump')
        self.assertEqual((row['price'],row['catalog']['price']),('100.00','100.00'))
        self.assertEqual(row['catalog']['checked_at'],target(original,'pump')['catalog']['checked_at'])
        self.assertIn('характеристики насоса в карточке магазина', row['selection_note'])
        for edit in [dict(price='77.00'),dict(quantity='2',selection_locked=True),dict(excluded=True),dict(existing=True),dict(unit_price_manual=True)]:
            s=deepcopy(original);target(s,'pump').update(edit)
            preserved,_=catalog.auto_select(s,current,only_variant='economy')
            self.assertEqual(target(preserved,'pump')['catalog']['id'],'1')
            self.assertEqual(target(preserved,'pump')['name'],target(s,'pump')['name'])
            self.assertEqual(target(preserved,'pump')['price'],target(s,'pump')['price'])
            self.assertEqual(preserved['variants']['standard'],evaluate(s)['variants']['standard'])

    def test_protected_choice_warns_about_new_catalog_conflict_without_mutation(self):
        c=catalog.parse_catalog(feed(offer(1,'Насос Good 8m3/h*8m 220V','100')))
        initial,_=catalog.auto_select(generate({**INPUTS,'power':'230','required_head':'8'}),c)
        target(initial,'pump')['catalog']['selection']='manual'
        before=deepcopy(target(initial,'pump'))
        c['products']['1'].update(specification_conflict=True,specification_note='Новый конфликт источника')
        result,_=catalog.auto_select(initial,c)
        row=target(result,'pump')
        for key in ('catalog','price','quantity','specs','model','name'):
            self.assertEqual(row[key],before[key])
        self.assertIn('Новый конфликт источника',row['selection_note'])
        self.assertIn('Новый конфликт источника',target(evaluate(result),'pump')['selection_note'])

    def test_embedded_missing_material_is_provisional_and_known_conflict_is_withdrawn(self):
        from .test_estimate_catalog_params import item
        c=catalog.parse_catalog(feed(item(71,'Форсунка Test',params=[('Тип отделки','пленка')])))
        base=generate({**INPUTS,'finish':'film'})
        result,_=catalog.auto_select(base,c)
        row=target(result,'inlet')
        self.assertEqual(row['catalog']['id'],'71')
        self.assertEqual(row['catalog']['material'],'')
        self.assertIn('материал корпуса',row['selection_note'])
        current=catalog.parse_catalog(feed(item(71,'Форсунка Test',params=[('Материал корпуса','нержавеющая сталь'),('Тип отделки','пленка')])))
        changed,_=catalog.auto_select(result,current)
        self.assertIsNone(target(changed,'inlet')['catalog'])
        self.assertIsNone(target(changed,'inlet')['amount'])
        self.assertIn('отклонён',target(changed,'inlet')['selection_note'])
        manual=deepcopy(result);target(manual,'inlet').update(auto_quantity=False,quantity='7',selection_locked=True)
        kept,_=catalog.auto_select(manual,current)
        self.assertEqual(target(kept,'inlet')['catalog']['id'],'71')
        self.assertEqual(target(kept,'inlet')['quantity'],'7.000')
        self.assertIn('Текущая выгрузка',target(kept,'inlet')['selection_note'])

    def test_live_manual_filter_warning_uses_pump_duty_not_only_pool_flow(self):
        c=catalog.parse_catalog(feed(offer(1,'Насос Test 12m3/h*8m 220V','100'),offer(2,'Фильтр Test 20 м3/ч','200')))
        initial,_=catalog.auto_select(generate({**INPUTS,'power':'230','required_head':'8'}),c)
        row=target(initial,'filter');row['catalog']['selection']='manual'
        before=deepcopy(row)
        c['products']['2'].update(filter_flow='10',nominal_flow='10')
        result,_=catalog.auto_select(initial,c)
        row=target(result,'filter')
        self.assertIn('производительность ниже',row['selection_note'])
        self.assertIn('производительность ниже',row['compatibility']['message'])
        self.assertEqual(row['catalog'],before['catalog'])
        self.assertEqual(row['specs'],before['specs'])
        self.assertEqual(row['price'],before['price'])

    def test_explicit_other_steel_grade_is_not_unknown_316(self):
        for grade in ('201','430','304/201'):
            c=catalog.parse_catalog(feed(offer(1,'Форсунка корпус AISI '+grade+' под пленку')))
            self.assertTrue(c['products']['1']['specification_conflict'])
            result,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),c)
            self.assertIsNone(target(result,'inlet','premium')['catalog'])

    def test_legacy_catalog_without_new_evidence_fields_keeps_totals_on_load(self):
        c=catalog.parse_catalog(feed(offer(1,'Насос Good 8m3/h*8m 220V','100')))
        s,_=catalog.auto_select(generate(INPUTS),c)
        for variant in s['variants'].values():
            for b in variant['blocks']:
                for row in b['rows']:
                    row.pop('catalog_review_note',None)
                    if row.get('catalog'):
                        for key in ('filtration','body_family','lining_denied'): row['catalog'].pop(key,None)
        old_totals={k:v['total'] for k,v in s['variants'].items()}
        loaded=evaluate(json.loads(json.dumps(s)))
        self.assertEqual({k:v['total'] for k,v in loaded['variants'].items()},old_totals)
        self.assertEqual(target(loaded,'pump')['price'],'100.00')
