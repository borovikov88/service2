from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from pool_service.tests.test_pool_estimates import INPUTS, block
from pool_service.tests.test_estimate_catalog import feed, offer

PRIMARY = {**INPUTS, 'heating':'exchanger', 'heating_power':'20', 'power':'230',
           'primary_pump_connection':'g1.5', 'primary_valve_connection':'g1',
           'primary_temperature':'80', 'primary_delta':'20', 'primary_medium':'water', 'primary_head':'2'}


def fixture():
    products = [
        ('91','32528','Насос Aquaviva LX GPD25-4S-180','100'),
        ('92','35398','Циркуляционный насос Aquaviva LX GPS25-5-180 с частотным регулированием','200'),
        ('93','19818','Клапан соленоидный Aquaviva 2W31 DN25-G1','300'),
        ('94','54203','Клапан соленоидный Aquaviva 2W31 DN50-G2','1'),
        ('95','19817','Клапан соленоидный Aquaviva 2W31 DN20','2'),
        ('96','', 'Теплообменник Test 20 кВт','500'),
    ]
    return catalog.parse_catalog(feed(*(offer(pid, name, price=price, url=f'https://shop.aqualine22.ru/tsirkulyatsionnye-nasosy/test-{pid}.html').replace('</offer>',f'<vendorCode>{sku}</vendorCode></offer>') for pid,sku,name,price in products)))


class PrimaryCircuitTests(SimpleTestCase):
    def test_real_catalogue_prices_and_separate_preliminary_status(self):
        base = generate(PRIMARY)
        result, count = catalog.auto_select(base, fixture())
        self.assertEqual(count,9)
        for tier, pid, delta in (('economy','91',900),('standard','92',1000),('premium','92',1000)):
            rows = block(result,variant=tier)['rows']
            pump = next(r for r in rows if r['kind']=='circulation_pump')
            valve = next(r for r in rows if r['kind']=='solenoid_valve')
            self.assertEqual(pump['catalog']['id'],pid)
            self.assertEqual(valve['catalog']['id'],'93')  # DN50 is cheaper but incompatible
            self.assertEqual(pump['selection_status'],'preliminary_primary')
            self.assertEqual(Decimal(result['variants'][tier]['total'])-Decimal(base['variants'][tier]['total']),delta)
            self.assertTrue(any('НЕ подтверждает совместимость' in w for w in result['variants'][tier]['warnings']))
        twice,_=catalog.auto_select(result,fixture())
        self.assertEqual([v['total'] for v in twice['variants'].values()],[v['total'] for v in result['variants'].values()])

    def test_known_mismatches_never_select(self):
        for change,kind in (({'primary_flow':'2.8'},'circulation_pump'),
                            ({'primary_head':'5'},'circulation_pump'),
                            ({'primary_temperature':'121'},'solenoid_valve')):
            s,_=catalog.auto_select(generate({**PRIMARY,**change}),fixture())
            self.assertIsNone(next(r for r in block(s)['rows'] if r['kind']==kind)['price'])
        # Heat duty is checked through water delta-T, not filtration flow.
        s,_=catalog.auto_select(generate({**PRIMARY,'primary_delta':'5'}),fixture())
        self.assertIsNone(next(r for r in block(s)['rows'] if r['kind']=='circulation_pump')['price'])

    def test_manual_prices_quantities_and_only_variant_preserved(self):
        s,_=catalog.auto_select(generate(PRIMARY),fixture())
        row=next(r for r in block(s)['rows'] if r['kind']=='circulation_pump')
        row.update(price='77',auto_quantity=False,quantity='3',selection_locked=True)
        standard=deepcopy(s['variants']['standard'])
        result,_=catalog.auto_select(recalculate(s,s['inputs']),fixture(),only_variant='economy')
        row=next(r for r in block(result)['rows'] if r['kind']=='circulation_pump')
        self.assertEqual((row['price'],row['quantity']),('77.00','3.000'))
        self.assertEqual(result['variants']['standard'],standard)

    def test_legacy_unlinked_pairs_can_be_assigned_without_duplicate_costs(self):
        s=generate(PRIMARY)
        rows=block(s)['rows']
        first=next(r for r in rows if r['kind']=='heater')
        second=deepcopy(first);second['id']='second';rows.append(second)
        old=[r for r in rows if r['kind'] in ('circulation_pump','solenoid_valve')]
        for row in old:
            row.update(heating_parent='',auto_quantity=False,quantity='1',price='50')
        unresolved=recalculate(s,PRIMARY)
        self.assertEqual(len([r for r in block(unresolved)['rows'] if r['kind']=='circulation_pump']),1)
        self.assertTrue(any('Есть непривязанные' in w for w in unresolved['variants']['economy']['warnings']))
        for row in block(unresolved)['rows']:
            if row['kind'] in ('circulation_pump','solenoid_valve'): row['heating_parent']=first['id']
        resolved=recalculate(unresolved,PRIMARY)
        self.assertEqual(len([r for r in block(resolved)['rows'] if r['kind']=='circulation_pump']),2)
        existing=[r for r in block(resolved)['rows'] if r.get('heating_parent')==first['id']]
        self.assertTrue(all(r['price']=='50.00' and r['quantity']=='1.000' for r in existing))

    def test_identity_requires_brand_and_complete_model(self):
        from pool_service.services.estimate_primary import facts
        product = fixture()['products']['91']
        self.assertIsNotNone(facts(product))
        self.assertIsNone(facts({**product, 'brand':'Other'}))
        self.assertIsNone(facts({**product, 'name':product['name']+'X'}))
        valve = fixture()['products']['93']
        self.assertIsNone(facts({**valve, 'name':'Aquaviva 2W31 DN250'}))

    def test_explicit_unbound_survives_recalculate_while_legacy_pair_still_binds(self):
        s=generate(PRIMARY)
        for tier in s['variants']:
            rows=block(s,variant=tier)['rows']
            parent=next(r for r in rows if r['kind']=='heater')
            parent['quantity']='2'
            valve=next(r for r in rows if r['kind']=='solenoid_valve')
            valve.update(heating_parent='',heating_unbound=True,quantity='2',price='100')
        result=evaluate(recalculate(s,s['inputs']))
        for tier in result['variants']:
            valves=[r for r in block(result,variant=tier)['rows'] if r['kind']=='solenoid_valve']
            self.assertEqual(len(valves),1)
            self.assertEqual(valves[0]['heating_parent'],'')
            self.assertTrue(valves[0]['heating_unbound'])
            self.assertIsNone(valves[0]['quantity'])
            self.assertIsNone(valves[0]['amount'])
            self.assertTrue(any('снята пользователем' in w for w in result['variants'][tier]['warnings']))
            # Parent assignment is authoritative even when a stale unbound flag is submitted.
            valves[0]['heating_parent']=next(r['id'] for r in block(result,variant=tier)['rows'] if r['kind']=='heater')
        rebound=recalculate(result,result['inputs'])
        for tier in rebound['variants']:
            valve=next(r for r in block(rebound,variant=tier)['rows'] if r['kind']=='solenoid_valve')
            self.assertFalse(valve['heating_unbound'])
            self.assertEqual(valve['quantity'],'2.000')
            self.assertEqual(valve['amount'],'200.00')
        legacy=generate(PRIMARY)
        for tier in legacy['variants']:
            for r in block(legacy,variant=tier)['rows']:
                if r['kind']=='solenoid_valve':
                    r['heating_parent']=''
                    r.pop('heating_unbound',None)
        legacy=recalculate(legacy,legacy['inputs'])
        self.assertTrue(all(next(r for r in block(legacy,variant=t)['rows'] if r['kind']=='solenoid_valve')['heating_parent'] for t in legacy['variants']))

    def test_missing_connections_propose_without_inventing_object_parameters(self):
        import json
        base=generate({**PRIMARY,'primary_pump_connection':'','primary_valve_connection':''})
        result,count=catalog.auto_select(base,fixture())
        self.assertEqual(count,9)
        for tier,pid,vid,delta in [('economy','91','94','601'),('standard','92','93','1000'),('premium','92','93','1000')]:
            rows=block(result,variant=tier)['rows']
            pump=next(r for r in rows if r['kind']=='circulation_pump')
            valve=next(r for r in rows if r['kind']=='solenoid_valve')
            self.assertEqual((pump['catalog']['id'],valve['catalog']['id']),(pid,vid))
            for row in (pump,valve):
                self.assertIn('Проверить:',row['selection_note'])
                self.assertIn('присоединение на объекте',row['selection_note'])
                self.assertIn('Источник характеристик:',row['selection_note'])
                self.assertIn('совместимость не подтверждена',row['selection_note'])
            self.assertEqual(Decimal(result['variants'][tier]['total'])-Decimal(base['variants'][tier]['total']),Decimal(delta))
        self.assertEqual(result['inputs']['primary_pump_connection'],'')
        self.assertEqual(result['inputs']['primary_valve_connection'],'')
        loaded=evaluate(json.loads(json.dumps(result)))
        again,filled=catalog.auto_select(loaded,fixture())
        self.assertEqual(filled,0)
        self.assertEqual(again['variants'],result['variants'])
        # Once actual connection is supplied, a cheap incompatible valve cannot remain auto-selected.
        changed,_=catalog.auto_select(recalculate(result,{**result['inputs'],'primary_valve_connection':'g1'}),fixture())
        self.assertEqual(next(r for r in block(changed)['rows'] if r['kind']=='solenoid_valve')['catalog']['id'],'93')

    def test_conflicts_unknown_identity_and_manual_data_are_protected(self):
        from pool_service.services.estimate_primary import rejection
        c=fixture()
        for field,value in [('specification_conflict',True),('level_data_issue','conflict')]:
            p={**c['products']['91'],field:value,'specification_note':'Тестовый конфликт'}
            self.assertIn('Противоречивые',rejection(p,PRIMARY))
        self.assertIsNotNone(rejection({**c['products']['91'],'sku':'999'},PRIMARY))
        for update in [dict(auto_quantity=False,quantity='2'),dict(unit_price_manual=True),dict(excluded=True),dict(selection_locked=True)]:
            s=generate(PRIMARY)
            row=next(r for r in block(s)['rows'] if r['kind']=='circulation_pump')
            row.update(update)
            result,_=catalog.auto_select(s,c)
            after=next(r for r in block(result)['rows'] if r['kind']=='circulation_pump')
            self.assertIsNone(after['catalog'])
            self.assertIsNone(after['price'])
        c['products']['91']['specification_conflict']=True
        result,_=catalog.auto_select(generate(PRIMARY),c)
        self.assertEqual(next(r for r in block(result)['rows'] if r['kind']=='circulation_pump')['catalog']['id'],'92')

    def test_changed_known_mismatch_with_no_alternative_withdraws_only_auto(self):
        for change,kind in [({'primary_pump_connection':'g1'},'circulation_pump'),
                            ({'primary_flow':'3'},'circulation_pump'),
                            ({'primary_head':'6'},'circulation_pump'),
                            ({'primary_temperature':'130'},'solenoid_valve')]:
            s,_=catalog.auto_select(generate(PRIMARY),fixture())
            old=next(r for r in block(s)['rows'] if r['kind']==kind)
            amount=Decimal(old['amount'])
            changed=recalculate(s,{**s['inputs'],**change})
            result,_=catalog.auto_select(changed,fixture(),only_variant='economy')
            target=next(r for r in block(result)['rows'] if r['kind']==kind)
            self.assertIsNone(target['catalog'])
            self.assertIsNone(target['price'])
            self.assertIn('отклонён',target['selection_note'])
            self.assertIsNone(target['amount'])
            # Other rows may obtain valid alternatives; verify the entire total.
            self.assertEqual(Decimal(result['variants']['economy']['total']), sum(
                (Decimal(r['amount'] or '0') for b in result['variants']['economy']['blocks'] if not b['excluded']
                 for r in b['rows'] if not r['excluded'] and not r['existing']), Decimal(0)))
            old['catalog']['selection']='manual'
            manual,_=catalog.auto_select(recalculate(s,{**s['inputs'],**change}),fixture(),only_variant='economy')
            kept=next(r for r in block(manual)['rows'] if r['kind']==kind)
            self.assertEqual(kept['catalog']['id'],old['catalog']['id'])
            self.assertEqual(kept['price'],old['price'])

    def test_new_live_catalog_conflict_withdraws_untouched_auto(self):
        s,_=catalog.auto_select(generate(PRIMARY),fixture())
        c=fixture()
        for p in c['products'].values():
            if p['kind']=='circulation_pump':
                p.update(specification_conflict=True,specification_note='Явный конфликт обновлённого каталога')
        result,_=catalog.auto_select(s,c)
        row=next(r for r in block(result)['rows'] if r['kind']=='circulation_pump')
        self.assertIsNone(row['catalog'])
        self.assertIsNone(row['amount'])
        self.assertIn('Явный конфликт обновлённого каталога',row['selection_note'])
