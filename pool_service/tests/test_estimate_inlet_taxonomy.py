"""Synthetic shop taxonomy and no guessed prices/engineering characteristics."""
from copy import deepcopy
import json
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog, pool_estimates as engine
from pool_service.services.estimate_embedded_candidates import assess
from .test_estimate_catalog import feed
from .test_estimate_catalog_params import item
from .test_pool_estimates import INPUTS, block


def product(name='Форсунка Test',params=(),description='',price='100',pid=1):
    return catalog.parse_catalog(feed(item(pid,name,params=params,description=description,price=price)))['products'][str(pid)]


class InletTaxonomyTests(SimpleTestCase):
    def test_shop_material_grade_covering_and_return_type(self):
        for material,grade,profile in [('Пластик','ABS','plastic_budget'),('Нержавеющая сталь','AISI 304','steel304'),('Нержавеющая сталь','AISI 316','steel316')]:
            p=product(params=[('Тип товара','Возвратная форсунка'),('Материал',material),('Марка материала',grade),('Покрытие чаши','Плёнка'),('Размещение','Стеновое')])
            self.assertEqual(assess(p,profile,'film'),(None,[]))
            self.assertEqual(p['embedded_role'],'inlet')
            self.assertIn('Тип товара',p['inlet_taxonomy']['source'])
        p=product('Возвратная форсунка Test ABS под пленку')
        self.assertEqual(p['embedded_role'],'inlet')

    def test_wrong_types_components_placements_and_material_are_not_returns(self):
        for kind in ['Гидромассажная форсунка','Пылесосная форсунка','Фонтанная форсунка','Вставка форсунки','Резьбовая заглушка','Донная форсунка']:
            p=product('Форсунка Test ABS под пленку',params=[('Тип товара',kind)])
            self.assertTrue(p['embedded_role'] != 'inlet' or assess(p,'plastic_budget','film')[0],kind)
        p=product(params=[('Тип товара','Возвратная форсунка'),('Размещение','Донное')])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=product(params=[('Тип товара','Возвратная форсунка'),('Материал','Бронза')])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])

    def test_vacuum_type_is_kept_separate_from_return_points(self):
        p=product('Форсунка Test ABS под пленку',params=[('Тип товара','Пылесосная форсунка')])
        self.assertEqual(p['embedded_role'],'vacuum')
        self.assertIsNone(assess(p,'plastic_budget','film')[0])

    def test_fountain_title_description_and_negative_control(self):
        for title,desc in [('Форсунка фонтанная Test ABS под пленку',''),('Форсунка ABS под пленку','Назначение: для фонтанов.'),('Форсунка ABS под пленку','Предназначена для гидромассажа.')]:
            self.assertIsNotNone(assess(product(title,description=desc),'plastic_budget','film')[0])
        p=product('Форсунка ABS под пленку',description='Возврат воды в бассейн. Не для фонтанов.')
        self.assertIsNone(assess(p,'plastic_budget','film')[0])

    def test_explicit_other_duty_cannot_be_overridden_by_structured_return_type(self):
        for name in ['Форсунка гидромассажная Test ABS под пленку','Форсунка донная Test ABS под пленку',
                     'Пылесосная форсунка Test ABS под пленку','Возвратная форсунка противотока Test ABS под пленку',
                     'Форсунка воздушная Test ABS под пленку']:
            for params in ([],[('Тип товара','Возвратная форсунка')]):
                p=product(name,params=params)
                self.assertTrue(p['embedded_role'] != 'inlet' or assess(p,'plastic_budget','film')[0],name)

    def test_unknown_duplicate_material_grade_is_not_silently_discarded(self):
        p=product('Форсунка Test под пленку',params=[('Тип товара','Возвратная форсунка'),
            ('Материал','Нержавеющая сталь'),('Марка материала','AISI 304'),('Марка материала','AISI 999')])
        self.assertTrue(p['specification_conflict'])
        self.assertNotEqual(p['material'],'steel304')
        self.assertIsNotNone(assess(p,'steel304','film')[0])

    def test_vacuum_components_without_xml_are_not_complete_fittings(self):
        p=product('Клапан для пылесосной форсунки ABS под пленку')
        self.assertNotIn(p['embedded_role'],{'inlet','vacuum'})
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=product('Пылесосная форсунка Test ABS под пленку')
        self.assertEqual(p['embedded_role'],'vacuum')
        self.assertIsNone(assess(p,'plastic_budget','film')[0])

    def test_category_ancestry_and_explicit_fountain_url_are_not_pool_returns(self):
        xml=feed(item(1,'Форсунка ABS под пленку')).decode().replace('<offers>', '<categories><category id="7">Фонтанное оборудование</category><category id="8" parentId="7">Форсунки</category></categories><offers>').replace('<price>', '<categoryId>8</categoryId><price>',1)
        p=catalog.parse_catalog(xml.encode())['products']['1']
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        xml=feed(item(1,'Форсунка ABS под пленку')).replace(b'/product-1.html',b'/fontany/forsunka.html')
        self.assertIsNotNone(assess(catalog.parse_catalog(xml)['products']['1'],'plastic_budget','film')[0])

    def test_conflicts_and_conditional_liner_kit_are_preserved(self):
        for params in [[('Материал','Пластик'),('Марка материала','AISI 316')],
                       [('Тип товара','Возвратная форсунка'),('Тип товара','Гидромассажная форсунка')],
                       [('Покрытие чаши','Плёнка с фланцевым комплектом 3862050')]]:
            p=product('Форсунка ABS под пленку',params=params)
            self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=product('Форсунка для пылесоса ABS под пленку',params=[('Тип товара','Возвратная форсунка')])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])

    def test_auto_replaces_wrong_candidate_without_changing_manual_history(self):
        good=item(1,'Форсунка ABS под пленку',price='500',params=[('Тип товара','Возвратная форсунка')])
        bad=item(2,'Форсунка ABS под пленку',price='10',description='Для фонтанов.',params=[('Тип товара','Фонтанная форсунка')])
        c=catalog.parse_catalog(feed(good,bad));s=engine.generate({**INPUTS,'finish':'film'})
        row=next(r for r in block(s,'embedded')['rows'] if r['kind']=='inlet')
        catalog.apply_product(row,c['products']['2'],c,'auto')
        original=deepcopy(s)
        selected,_=catalog.auto_select(s,c)
        row=next(r for r in block(selected,'embedded')['rows'] if r['kind']=='inlet')
        self.assertEqual(row['catalog']['id'],'1')
        self.assertEqual(row['price'],'500.00')
        self.assertEqual(s,original)
        oldrow=next(r for r in block(s,'embedded')['rows'] if r['kind']=='inlet')
        oldrow.update(selection_locked=True,price='7.00',quantity='9',auto_quantity=False)
        kept,_=catalog.auto_select(s,c)
        kept=engine.evaluate(json.loads(json.dumps(kept)))
        protected=next(r for r in block(kept,'embedded')['rows'] if r['kind']=='inlet')
        self.assertEqual((protected['catalog']['id'],protected['price'],protected['quantity']),('2','7.00','9.000'))
        self.assertTrue(protected['catalog']['inlet_taxonomy']['issue'])
        result=catalog.search_catalog(c,'','inlet','',s['inputs'],8)
        self.assertTrue(next(p for p in result['products'] if p['id']=='2')['warning'])
