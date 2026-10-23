from copy import deepcopy
from django.test import SimpleTestCase
from pool_service.services import pool_estimates as engine, estimate_catalog as shop
from pool_service.services import estimate_lighting as lighting
from .test_estimate_construction import NEW
from .test_estimate_catalog import feed, offer


def fixture():
    return shop.parse_catalog(feed(
        offer(901,'Прожектор для бассейна ABS LED003 100LED 10W 12V AC','100'),
        offer(902,'Прожектор для бассейна ABS LED003 300LED 20W 12V AC','200'),
        offer(903,'Прожектор для бассейна AISI316 HT201S 500LED 30W 12V AC + закладная','300'),
        offer(904,'Закладная прожектора LED003 под лайнер','10'),
        offer(905,'Трансформатор для бассейна 100W 12V AC','50'),
        offer(906,'Распределительная коробка для бассейна','20'),
        offer(907,'Трансформатор для бассейна 10W 12V AC','1'),
        offer(908,'Прожектор для бассейна ABS 100LED 10W 230V','1'),
    ))


def rows(s,tier='economy'):
    return {r['kind']:r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind'] in lighting.ROLES}

class LightingTests(SimpleTestCase):
    def test_catalog_selection_and_included_niche(self):
        s=engine.generate({**NEW,'lighting':'yes','light_count':'2'})
        s,_=shop.auto_select(s,fixture())
        for tier,pid in [('economy','901'),('standard','902'),('premium','903')]:
            r=rows(s,tier)
            self.assertEqual(r['pool_light']['catalog']['id'],pid)
            self.assertEqual(r['pool_light']['quantity'],'2')
            self.assertEqual(r['light_transformer']['catalog']['id'],'905')
            self.assertEqual(r['light_transformer']['quantity'],'1')
            self.assertIn('Проверить',r['pool_light']['selection_note'])
        self.assertTrue(rows(s,'premium')['light_niche']['excluded'])
        self.assertEqual(rows(s)['light_niche']['catalog']['id'],'904')
        self.assertEqual(engine.evaluate(s),engine.evaluate(engine.evaluate(s)))

    def test_off_on_recalc_history_manual(self):
        s=engine.generate(NEW); old=deepcopy(s)
        self.assertFalse(rows(s))
        s=engine.recalculate(s,{**s['inputs'],'lighting':'yes','light_count':'3'})
        s,_=shop.auto_select(s,fixture()); r=rows(s)
        r['pool_light'].update(price='0',unit_price_manual=True,quantity='4',auto_quantity=False)
        r['light_box'].update(excluded=True,reason='Исключено пользователем')
        s=engine.recalculate(s,{**s['inputs'],'lighting':'no'})
        self.assertTrue(all(r['excluded'] for r in rows(s).values()))
        s=engine.recalculate(s,{**s['inputs'],'lighting':'yes'})
        s,_=shop.auto_select(s,fixture())
        self.assertEqual(rows(s)['pool_light']['price'],'0.00')
        self.assertEqual(rows(s)['pool_light']['quantity'],'4.000')
        self.assertTrue(rows(s)['light_box']['excluded'])
        self.assertEqual(len(rows(s)),4)
        self.assertEqual(old,engine.evaluate(old))

    def test_unknown_count_stays_unknown(self):
        s=engine.generate({**NEW,'lighting':'yes','light_count':''})
        self.assertIsNone(rows(s)['pool_light']['quantity'])
        self.assertGreater(s['variants']['economy']['missing_quantities'],0)

    def test_material_override_and_conflicting_power(self):
        s=engine.generate({**NEW,'lighting':'yes','light_count':'2'})
        s['variants']['premium']['embedded_profile']='plastic_budget'
        s,_=shop.auto_select(s,fixture())
        self.assertEqual(rows(s,'premium')['pool_light']['catalog']['id'],'902')
        bad=shop.parse_catalog(feed(offer(910,'Прожектор для бассейна ABS AISI316 10W 20W 12V','1')))
        p=bad['products']['910']
        self.assertTrue(lighting.assess(p,'plastic_budget')[0])

    def test_deleted_manual_prices_units_and_manual_niche_protected(self):
        s,_=shop.auto_select(engine.generate({**NEW,'lighting':'yes','light_count':'2'}),fixture())
        r=rows(s)
        r['pool_light']['price']='999'
        r['light_box']['unit']='м'
        r['light_box']['quantity']='7'
        r['light_box']['auto_quantity']=True
        block=next(b for b in s['variants']['economy']['blocks'] if b['id']=='embedded')
        block['rows'].remove(r['light_niche'])
        s=engine.recalculate(s,s['inputs']);s,_=shop.auto_select(s,fixture())
        self.assertEqual(rows(s)['pool_light']['price'],'999.00')
        self.assertIsNone(rows(s)['light_box']['quantity'])
        self.assertNotIn('light_niche',rows(s))

    def test_body_evidence_finish_and_power_rejections(self):
        data=shop.parse_catalog(feed(
            offer(911,'Прожектор для бассейна ABS LED003 100LED 10W 12V AC','100',description='Не для плитки и пленки.'),
            offer(912,'Прожектор для бассейна ABS LED003 100LED 10W 12V AC','100',description='Материал корпуса: AISI 316.'),
            offer(913,'Прожектор для бассейна ABS LED003 100LED 10W 12V AC','100',description='Крепеж AISI316.'),
        ))
        self.assertTrue(lighting.assess(data['products']['911'],'plastic_budget',finish='film')[0])
        self.assertTrue(lighting.assess(data['products']['912'],'plastic_budget')[0])
        self.assertFalse(lighting.assess(data['products']['913'],'plastic_budget')[0])
        s,_=shop.auto_select(engine.generate({**NEW,'lighting':'yes','light_count':'2'}),fixture())
        rows(s)['pool_light'].update(quantity='11',auto_quantity=False)
        s,_=shop.auto_select(s,fixture())
        self.assertIsNone(rows(s)['light_transformer']['price'])
        self.assertEqual(rows(s)['light_box']['quantity'],'11.000')

    def test_sections_partition_cost_without_changing_history(self):
        from pool_service.templatetags.estimate_numbers import estimate_sections
        from decimal import Decimal
        s=engine.generate(NEW); before=deepcopy(s)
        for v in s['variants'].values():
            sections=estimate_sections(v)
            ids=[r['id'] for section in sections for b in section['blocks'] for r in b['rows']]
            self.assertEqual(len(ids),len(set(ids)))
            self.assertEqual(sum((Decimal(b['subtotal']) for section in sections for b in section['blocks']),Decimal(0)),Decimal(v['total']))
        self.assertEqual(s,before)

    def test_review_explicit_grade_and_suffix_niche_denial(self):
        products=shop.parse_catalog(feed(
            offer(921,'Прожектор подводный AISI430 12V 20W 100LED','100'),
            offer(922,'Прожектор подводный AISI316 12V 20W 100LED с закладной','100',description='Закладная не входит в комплект.'),
            offer(923,'Прожектор подводный AISI316 12V 20W 100LED с закладной','100',description='Коробка не входит, закладная входит в комплект.'),
        ))['products']
        for profile in ('plastic_budget','steel316'):
            self.assertTrue(lighting.assess(products['921'],profile)[0])
        self.assertTrue(lighting.assess(products['922'],'steel316')[0])
        self.assertFalse(products['922']['lighting']['niche_included'])
        self.assertFalse(lighting.assess(products['923'],'steel316')[0])

    def test_review_manual_niche_and_wrong_automatic_unit(self):
        s=engine.generate({**NEW,'lighting':'yes','light_count':'2'})
        niche=rows(s,'premium')['light_niche']
        niche.update(price='123.45',quantity='3',auto_quantity=False)
        s,_=shop.auto_select(s,fixture())
        self.assertFalse(rows(s,'premium')['light_niche']['excluded'])
        self.assertEqual(rows(s,'premium')['light_niche']['amount'],'369.00')
        rows(s)['pool_light']['unit']='м'
        s=engine.evaluate(s)
        self.assertIsNone(rows(s)['pool_light']['quantity'])
        self.assertIsNone(rows(s)['pool_light']['amount'])
