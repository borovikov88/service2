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
        self.assertEqual(rows(s)['light_box']['quantity'],'7.000')
        self.assertNotIn('light_niche',rows(s))
