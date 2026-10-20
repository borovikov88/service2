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

    def test_missing_connections_and_known_mismatches_never_guess_diameter(self):
        for change,kind in (({'primary_pump_connection':''},'circulation_pump'),
                            ({'primary_valve_connection':''},'solenoid_valve'),
                            ({'primary_flow':'2.8'},'circulation_pump'),
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
