from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services import pool_estimates as engine, estimate_catalog as shop
from pool_service.services import estimate_lighting as lighting
from pool_service.services.estimate_layout import sections
from .test_estimate_construction import NEW
from .test_estimate_catalog import feed, offer
from .test_estimate_lighting import rows


def fixture():
    return shop.parse_catalog(feed(
        offer(951,'Прожектор для фонтана ABS 100LED 10W 12V AC White','1'),
        offer(952,'Прожектор подводный ABS LED003 100LED 10W 12V AC White','100'),
        offer(953,'Прожектор подводный ABS LED003 300LED 20W 12V AC White','200'),
        offer(954,'Прожектор подводный AISI316 HT201S 500LED 30W 12V AC White + закладная','300'),
        offer(955,'Прожектор подводный ABS LED003 100LED 10W 12V AC RGB','110'),
        offer(956,'Прожектор подводный ABS LED003 300LED 20W 12V AC RGB','220'),
        offer(957,'Прожектор подводный AISI316 HT201S 500LED 30W 12V AC RGB + закладная','330'),
        offer(958,'Пульт управления к прожекторам Aquaviva RC-01','50'),
        offer(959,'Пульт управления к прожекторам Aquaviva RC-01 Wi-Fi','70'),
        offer(960,'Трансформатор для бассейна 300W 12V AC','80'),
        offer(961,'Распределительная коробка для бассейна','20'),
        offer(962,'Закладная прожектора LED003 под лайнер','10'),
    ))

class LightingColorTests(SimpleTestCase):
    def test_fontan_rejected_in_name_description_and_category(self):
        data=fixture();p=data['products']['951']
        self.assertTrue(lighting.assess(p,'plastic_budget',color='white')[0])
        p=shop.parse_catalog(feed(offer(970,'Прожектор подводный ABS LED003 100LED 10W 12V AC White','10',description='Назначение: освещение фонтанов.')))['products']['970']
        self.assertTrue(lighting.assess(p,'plastic_budget')[0])
        negative=shop.parse_catalog(feed(offer(971,'Прожектор подводный ABS LED003 100LED 10W 12V AC White','10',description='Не подходит для фонтанов. Для бассейнов.')))['products']['971']
        self.assertFalse(lighting.assess(negative,'plastic_budget')[0])

    def test_white_rgb_choices_and_controller_price_tiers(self):
        for color,expected in [('white',['952','953','954']),('rgb',['955','956','957'])]:
            s,_=shop.auto_select(engine.generate({**NEW,'lighting':'yes','light_color':color,'light_count':'2'}),fixture())
            for tier,pid in zip(s['variants'],expected):
                self.assertEqual(rows(s,tier)['pool_light']['catalog']['id'],pid)
                if color=='white':self.assertNotIn('light_controller',rows(s,tier))
                else:self.assertEqual(rows(s,tier)['light_controller']['catalog']['id'],'958' if tier=='economy' else '959')
                tables={b['id']:b for st in sections(s['variants'][tier]) for b in st['blocks']}
                self.assertIn('light_transformer',[r['kind'] for r in tables['equipment']['rows']])
                self.assertEqual(tables['embedded']['rows'][-1]['id'],'pvc_embedded')
                self.assertEqual(tables['materials']['rows'][-1]['id'],'piping')

    def test_rgb_white_toggle_recalc_no_duplicates_protect_manual(self):
        s,_=shop.auto_select(engine.generate({**NEW,'lighting':'yes','light_color':'rgb','light_count':'2'}),fixture())
        before=deepcopy(s)
        for _ in range(3):
            s=engine.recalculate(s,{**s['inputs'],'light_color':'white'});s,_=shop.auto_select(s,fixture())
            self.assertTrue(rows(s)['light_controller']['excluded'])
            s=engine.recalculate(s,{**s['inputs'],'light_color':'rgb'});s,_=shop.auto_select(s,fixture())
            self.assertFalse(rows(s)['light_controller']['excluded'])
            self.assertEqual(s['variants']['economy']['total'],before['variants']['economy']['total'])
        target=rows(s)['light_controller'];target.update(price='0',unit_price_manual=True)
        s,_=shop.auto_select(engine.recalculate(s,s['inputs']),fixture())
        self.assertEqual(rows(s)['light_controller']['price'],'0.00')

    def test_manual_lamp_quantity_updates_niche_boxes_and_installation(self):
        s=engine.generate({**NEW,'lighting':'yes','light_color':'white','light_count':'2'})
        before=next(r for b in s['variants']['economy']['blocks'] for r in b['rows'] if r['id']=='embedded_work')
        old=Decimal(before['quantity'])
        rows(s)['pool_light'].update(quantity='5',auto_quantity=False)
        s=engine.evaluate(s)
        self.assertEqual(rows(s)['light_niche']['quantity'],'5.000')
        self.assertEqual(rows(s)['light_box']['quantity'],'5.000')
        after=next(r for b in s['variants']['economy']['blocks'] for r in b['rows'] if r['id']=='embedded_work')
        self.assertEqual(Decimal(after['quantity']),old+6)

    def test_empties_only_drain_volume_boundary_without_overriding_manual(self):
        for length,width,depth,expected in [('10','5','1','1'),('10','5','1.001','2')]:
            s=engine.generate({**NEW,'length':length,'width':width,'depth_start':depth,'depth_end':depth,'drain_scheme':'empty_only'})
            self.assertEqual(s['variants']['economy']['embedded_counts']['drain'],expected)
        s=engine.generate({**NEW,'drain_scheme':'suction'})
        self.assertEqual(s['variants']['economy']['embedded_counts']['drain'],'2')
        r=next(r for b in s['variants']['economy']['blocks'] for r in b['rows'] if r['kind']=='drain');r.update(quantity='3',auto_quantity=False)
        s=engine.recalculate(s,{**s['inputs'],'drain_scheme':'empty_only'})
        self.assertEqual(next(r for b in s['variants']['economy']['blocks'] for r in b['rows'] if r['kind']=='drain')['quantity'],'3.000')

    def test_color_negative_and_conflicting_claims(self):
        products=shop.parse_catalog(feed(
            offer(973,'Прожектор подводный ABS 10W 12V AC White без RGB','100'),
            offer(974,'Прожектор подводный ABS 10W 12V AC RGB','100',description='Не поддерживает RGB.'),
            offer(975,'Прожектор подводный ABS 10W 12V AC','100',description='Без RGB.'),
        ))['products']
        self.assertFalse(lighting.assess(products['973'],'plastic_budget',color='white')[0])
        self.assertTrue(lighting.assess(products['973'],'plastic_budget',color='rgb')[0])
        self.assertTrue(lighting.assess(products['974'],'plastic_budget',color='rgb')[0])
        self.assertTrue(lighting.assess(products['975'],'plastic_budget',color='rgb')[0])

    def test_zero_lamps_gives_zero_automatic_accessories_no_new_charge(self):
        s,_=shop.auto_select(engine.generate({**NEW,'lighting':'yes','light_color':'rgb','light_count':'2'}),fixture())
        rows(s)['pool_light'].update(quantity='0',auto_quantity=False)
        s=engine.evaluate(s)
        for kind in ('light_niche','light_box','light_transformer','light_controller'):
            self.assertEqual(Decimal(rows(s)[kind]['quantity']),0)
            if not rows(s)[kind]['excluded']:self.assertEqual(rows(s)[kind]['amount'],'0.00')
