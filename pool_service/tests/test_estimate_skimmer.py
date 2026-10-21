"""Synthetic regressions for skimmer take-off and per-quote material choices."""
from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog, estimate_embedded as embedded
from pool_service.services.pool_estimates import generate, recalculate, evaluate, EstimateInputError
from pool_service.tests.test_pool_estimates import INPUTS, block
from pool_service.tests.test_estimate_catalog import offer, feed
from pool_service.tests.test_estimate_heating_finish import HEATING


def fitting(snapshot, role, variant='economy', profile=None):
    profile = profile or snapshot['variants'][variant]['embedded_profile']
    return next(r for r in block(snapshot, 'embedded', variant)['rows'] if r.get('embedded_role') == role and r.get('embedded_profile') == profile)


def stock():
    return catalog.parse_catalog(feed(
        offer(101, 'Скиммер Aquaviva Test под лайнер', price='100', description='Материал ABS-пластик'),
        offer(102, 'Форсунка стеновая Aquaviva Test под лайнер', price='10', description='Материал ABS-пластик'),
        offer(103, 'Донный слив Aquaviva Test под лайнер', price='20', description='Материал ABS-пластик'),
        offer(104, 'Форсунка пылесосная Aquaviva Test под лайнер', price='5', description='Материал ABS-пластик'),
        offer(105, 'Скиммер Kripsol Test под лайнер', brand='Kripsol', price='300', description='Материал ABS-пластик'),
        offer(106, 'Скиммер Aquaviva Test под лайнер AISI 304', price='400'),
        offer(107, 'Скиммер Aquaviva Test под лайнер AISI 316L', price='600'),
        offer(108, 'Скиммер Aquaviva Cheap под лайнер AISI 304/316', price='1'),
        offer(109, 'Скиммер Aquaviva Wrong под бетон AISI 316L', price='1'),
        offer(110, 'Скиммер Aquaviva Gone под лайнер AISI 316L', price='1', available='false'),
    ))


class SkimmerEstimateTests(SimpleTestCase):
    def test_counts_and_per_variant_materials_from_real_specs_not_brand_guessing(self):
        s, _ = catalog.auto_select(generate({**INPUTS, 'finish':'film'}), stock())
        self.assertEqual(s['variants']['economy']['embedded_counts'], {'skimmer':'2','inlet':'3','drain':'2','vacuum':'1','embedded_work':'8'})
        self.assertEqual([fitting(s,'skimmer',v)['catalog']['id'] for v in s['variants']], ['101','105','107'])
        self.assertEqual(fitting(s,'inlet')['quantity'],'4.000')
        self.assertEqual(fitting(s,'inlet')['filtration_quantity'],'3.000')
        self.assertEqual(fitting(s,'inlet')['level_quantity'],'1.000')
        self.assertEqual(block(s,'embedded')['subtotal'],'32285.00')  # fittings285 + PVC materials32 ×1000
        c = stock(); del c['products']['107']
        missing, _ = catalog.auto_select(generate({**INPUTS,'finish':'film'}), c)
        self.assertIsNone(fitting(missing,'skimmer','premium')['price'])
        self.assertTrue(any('AISI 316' in w for w in missing['variants']['premium']['warnings']))

    def test_profile_change_only_current_quote_keeps_old_prices_and_manual_exclusions(self):
        s, _ = catalog.auto_select(generate({**INPUTS,'finish':'film'}), stock())
        fitting(s,'skimmer')['price']='123.45'
        fitting(s,'inlet').update(excluded=True,reason='Клиент')
        changed = embedded.change_profile(s,'economy','steel304')
        changed, _ = catalog.auto_select(changed,stock(),only_variant='economy')
        old=fitting(changed,'skimmer',profile='plastic_budget')
        self.assertEqual(old['price'],'123.45');self.assertTrue(old['excluded'])
        self.assertEqual(fitting(changed,'skimmer')['catalog']['id'],'106')
        self.assertEqual(changed['variants']['standard'],s['variants']['standard'])
        restored=embedded.change_profile(changed,'economy','plastic_budget')
        self.assertFalse(fitting(restored,'skimmer')['excluded'])
        self.assertEqual(fitting(restored,'skimmer')['price'],'123.45')
        self.assertTrue(fitting(restored,'inlet')['excluded'])
        self.assertFalse(fitting(s,'skimmer')['excluded'])

    def test_manually_restored_old_profile_is_not_replaced_by_current_profile(self):
        s, _ = catalog.auto_select(generate({**INPUTS,'finish':'film'}), stock())
        changed = embedded.change_profile(s,'economy','steel304')
        fitting(changed,'skimmer',profile='plastic_budget').update(excluded=False, reason='')
        selected, _ = catalog.auto_select(changed,stock())
        old = fitting(selected,'skimmer',profile='plastic_budget')
        self.assertEqual(old['catalog']['id'],'101')
        self.assertFalse(old['excluded'])
        self.assertEqual(fitting(selected,'skimmer')['catalog']['id'],'106')

    def test_finish_stock_ceiling_and_legacy_upgrade_preserve_manual_quantities(self):
        s=generate({**INPUTS,'length':'8.123','finish':'film'})
        g=s['geometry'];rows=block(s,'finish')['rows']
        self.assertEqual([r['quantity'] for r in rows[:3]],['80.000','69.000','80.000'])
        self.assertEqual(g['volume'],'48.738')  # physical geometry is not rounded to whole units
        rows[0].update(auto_quantity=False,quantity='88.5',price='10')
        changed=recalculate(s,{**INPUTS,'length':'9','finish':'film'})
        self.assertEqual(block(changed,'finish')['rows'][0]['quantity'],'88.500')
        self.assertEqual(block(changed,'finish')['rows'][1]['quantity'],'75.000')
        self.assertEqual(block(changed,'finish')['rows'][2]['quantity'],'87.000')
        tile=recalculate(changed,{**INPUTS,'finish':'tile'})
        active=[r for r in block(tile,'finish')['rows'] if not r['excluded'] and r.get('finish_type')]
        self.assertEqual(len(active),3);self.assertTrue(all(r['quantity']=='68.000' for r in active))
        legacy=generate({**INPUTS,'finish':'film'})
        for v in legacy['variants'].values():
            b=next(b for b in v['blocks'] if b['id']=='finish');b['rows']=b['rows'][:2]
            for r in b['rows']:r['basis']='finish_area';r['quantity']='68.000'
        upgraded=recalculate(legacy,{**INPUTS,'finish':'film'})
        self.assertEqual([next(r['quantity'] for r in block(upgraded,'finish')['rows'] if r['id'].startswith(prefix)) for prefix in ('finish_material','finish_work','finish_geotextile')],['79.000','68.000','79.000'])

    def test_outdoor_factor_applies_once_only_to_automatic_heating(self):
        inside=generate(HEATING);outside=generate({**HEATING,'location':'outdoor'})
        self.assertEqual(inside['heating']['power'],'2.784')
        self.assertEqual(outside['heating']['power'],'4.176')
        self.assertEqual(recalculate(outside,{**HEATING,'location':'outdoor'})['heating']['power'],'4.176')
        self.assertEqual(generate({**HEATING,'location':'outdoor','heating_power':'6'})['heating']['power'],'6.000')

    def test_counts_grow_with_selected_pump_and_protected_rows_remain(self):
        s,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),stock())
        block(s)['rows'][0]['specs']['flow']='20'
        fitting(s,'inlet').update(auto_quantity=False,quantity='9',selection_locked=True)
        fitting(s,'drain').update(existing=True,quantity='1')
        changed,_=catalog.auto_select(s,stock())
        self.assertEqual(fitting(changed,'skimmer')['quantity'],'4.000')
        self.assertEqual(fitting(changed,'inlet')['quantity'],'9.000')
        self.assertEqual(fitting(changed,'drain')['quantity'],'1.000')
        block(changed,'embedded')['excluded']=True
        frozen,_=catalog.auto_select(changed,stock())
        self.assertTrue(block(frozen,'embedded')['excluded'])

    def test_premium_prefers_expensive_suitable_model_without_giant_capacity(self):
        f=catalog.parse_catalog(feed(
            offer(1,'Насос Aquaviva Test (220V, 8m3/h*8m)',price='100'),
            offer(2,'Насос Hayward Test (220V, 10m3/h*8m)',brand='Hayward',price='500'),
            offer(3,'Насос Other Test (220V, 100m3/h*8m)',brand='Other',price='10000'),
            offer(4,'Фильтр Other Test (10 м3/ч)',brand='Other',price='300'),
            offer(5,'Фильтр Other Large (12 м3/ч)',brand='Other',price='600'),
            offer(6,'Насос Other Wrong (380V, 8m3/h*8m)',brand='Other',price='900'),
        ))
        s,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),f)
        self.assertEqual([r['catalog']['id'] for r in block(s,variant='premium')['rows'][:2]],['2','5'])
        self.assertEqual(block(s,variant='premium')['rows'][1]['specs']['filter_flow'],'12.000')

    def test_restored_old_finish_is_not_hidden_again_by_geometry_recalculation(self):
        s = recalculate(generate({**INPUTS,'finish':'film'}), {**INPUTS,'finish':'tile'})
        old = block(s,'finish')['rows'][0]; old.update(excluded=False,reason='')
        changed = recalculate(s,{**INPUTS,'finish':'tile','length':'10'})
        self.assertFalse(block(changed,'finish')['rows'][0]['excluded'])

    def test_validation_keeps_embedded_line_quantities_in_sync_with_pump_change(self):
        s = generate({**INPUTS,'finish':'film'})
        fitting(s,'drain').update(existing=True,quantity='1')
        fitting(s,'vacuum').update(auto_quantity=False,quantity='2')
        block(s)['rows'][0]['specs']['flow']='40'
        changed = evaluate(s)
        self.assertEqual(fitting(changed,'skimmer')['quantity'],'8.000')
        self.assertEqual(fitting(changed,'inlet')['quantity'],'15.000')
        self.assertEqual(fitting(changed,'inlet')['filtration_quantity'],'14.000')
        self.assertEqual(fitting(changed,'inlet')['level_quantity'],'1.000')
        self.assertEqual(fitting(changed,'drain')['quantity'],'1.000')
        self.assertEqual(fitting(changed,'vacuum')['quantity'],'2.000')

    def test_excluded_legacy_supply_is_not_reintroduced_by_recalculation(self):
        from pool_service.services.pool_estimates import make_row
        s = generate(INPUTS)
        legacy = make_row('embedded_supply','Закладные: подобрать состав и количество')
        legacy.update(excluded=True, reason='Исключено пользователем')
        block(s,'embedded')['rows']=[legacy]
        changed,_=catalog.auto_select(recalculate(s,{**INPUTS,'finish':'film'}),stock())
        self.assertEqual(len(block(changed,'embedded')['rows']),1)
        self.assertTrue(block(changed,'embedded')['rows'][0]['excluded'])

    def test_ambiguous_grade_accessories_and_incompatible_lining_not_inferred(self):
        for name in ('Форсунка гидромассажная под лайнер AISI 316', 'Крышка скиммера AISI 316', 'Скиммер Intex под лайнер ABS'):
            self.assertEqual(embedded.specifications(name,'','Aquaviva')['embedded_role'],'')
        for name in ('Форсунка стеновая под лайнер AISI 304/316', 'Скиммер Test 304 под лайнер'):
            self.assertEqual(embedded.specifications(name,'','Aquaviva')['material'],'')
        for name in ('Скиммер пластиковый под лайнер с крышкой AISI 316', 'Форсунка под лайнер с винтами AISI 316'):
            spec = embedded.specifications(name,'Материал корпуса ABS','Aquaviva')
            # Explicit BODY ABS survives a different material of hardware.
            self.assertEqual(spec['material'],'plastic')
            self.assertFalse(embedded.matches({**spec,'available':True,'brand':'Aquaviva'},'steel316','film'))
        spec = embedded.specifications('Скиммер под лайнер AISI 316','Материал корпуса ABS','Aquaviva')
        self.assertEqual(spec['material'],'')
        self.assertTrue(spec['specification_conflict'])
        self.assertFalse(embedded.matches({**spec,'available':True,'brand':'Aquaviva'},'steel316','film'))
        s=generate(INPUTS)
        for profile in ([],{},None,'wood'):
            with self.assertRaises(EstimateInputError):embedded.change_profile(s,'economy',profile)
        old=deepcopy(s)
        for v in old['variants'].values():v.pop('embedded_profile')
        self.assertEqual(evaluate(old)['variants']['premium']['embedded_profile'],'steel316')

    def test_catalog_reports_profile_and_ambiguous_unavailable_grade(self):
        products=catalog.parse_catalog(feed(
            offer(801,'Форсунка Kripsol BOL 20 под лайнер',brand='Kripsol'),
            offer(802,'Форсунка Aquaviva AISI 304 под лайнер'),
            offer(803,'Форсунка Aquaviva AISI 304/316 под лайнер',available='false'),
            offer(804,'Форсунка Aquaviva EM4412 под лайнер')))
        inputs=generate({**INPUTS,'finish':'film'})['inputs']
        results=catalog.search_catalog(products,'','inlet','',inputs,Decimal('8'),variant='premium')
        byid={p['id']:p for p in results['products']}
        self.assertIn('профилю КП',byid['801']['warning'])
        self.assertIn('профилю КП',byid['802']['warning'])
        self.assertIn('доступность',byid['803']['warning'])
        self.assertIn('марка стали не подтверждены',byid['803']['warning'])
        self.assertIn('Материал',byid['804']['warning'])
        selected,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),products)
        self.assertIsNotNone(fitting(selected,'inlet','premium')['price'])
        self.assertEqual(fitting(selected,'inlet','premium')['catalog']['material'],'')
        self.assertIn('марка стали корпуса AISI 316',fitting(selected,'inlet','premium')['selection_note'])
        self.assertIn('Не подтверждены',fitting(selected,'inlet','premium')['selection_note'])

    def test_verified_exact_fitting_identity_enriches_only_material_and_lining(self):
        def item(pid,model,sku,brand='Aquaviva',extra=''):
            return offer(pid,f'Форсунка {brand} {model} {extra}',brand=brand,price='123.45',description='Идентификация модели').replace('</offer>',f'<vendorCode>{sku}</vendorCode></offer>')
        products=catalog.parse_catalog(feed(item(901,'EM4412','16785'),item(902,'EM4414','16786'),
            item(903,'EM4408','16781'),item(904,'EM4409','16782'),item(905,'EM4412-X','16785'),
            item(906,'EM4412','wrong'),item(907,'EM4412','16785',brand='Other'),item(908,'EM4412','16785',extra='под бетон')))
        for pid,lining in [('901','film'),('902','film'),('903','concrete'),('904','concrete')]:
            product=products['products'][pid]
            self.assertEqual(product['material'],'plastic')
            self.assertEqual(product['lining'],[lining])
            self.assertEqual(product['price'],'123.45')
            self.assertTrue(product['available'])
            self.assertIsNone(product['nominal_flow'])
            self.assertIn('дилера',product['specification_note'])
        self.assertIn('9 и 5',products['products']['902']['specification_note'])
        for pid in ('905','906','907','908'):
            self.assertEqual(products['products'][pid]['material'],'')
        self.assertIn('Конфликт',products['products']['908']['specification_note'])
        selected,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),products)
        nozzle=fitting(selected,'inlet')
        self.assertEqual(nozzle['catalog']['id'],'901')
        self.assertEqual(nozzle['price'],'123.45')
        self.assertIn('не подтверждён',nozzle['catalog']['specification_note'])
        self.assertIsNotNone(fitting(selected,'inlet','premium')['price'])
        self.assertEqual(fitting(selected,'inlet','premium')['catalog']['material'],'')
        self.assertIn('марка стали корпуса AISI 316',fitting(selected,'inlet','premium')['selection_note'])

    def test_enrichment_does_not_override_explicit_body_or_lining_conflicts(self):
        from pool_service.services.estimate_fitting_facts import enrich
        for description in ('Корпус из нержавеющей стали AISI 316.', 'Для бетонных бассейнов.', 'Материал корпуса: AISI 316', 'Корпус из латуни', '<b>Материал корпуса:</b><span>AISI 316</span>'):
            specs={'material':'','lining':[],'embedded_role':'inlet'}
            enrich(specs,'Форсунка Aquaviva EM4412','Aquaviva','16785',description)
            self.assertEqual(specs['material'],'')
            self.assertIn('Конфликт',specs['specification_note'])
        specs={'material':'','lining':[],'embedded_role':'inlet'}
        enrich(specs,'Форсунка Aquaviva EM4414','Aquaviva','16786','Корпус ABS; винты из нержавеющей стали AISI 316, латунные вставки.')
        self.assertEqual(specs['material'],'plastic')
