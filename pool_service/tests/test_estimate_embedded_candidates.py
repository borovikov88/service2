"""Synthetic fitting descriptions: eligibility, no inferred catalogue facts."""
from copy import deepcopy
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.estimate_embedded_candidates import assess
from .test_estimate_catalog import offer, feed
from .test_estimate_catalog_params import item


class EmbeddedCandidateTests(SimpleTestCase):
    def product(self, name='Форсунка ABS под пленку', brand='Aquaviva', description=''):
        return catalog.parse_catalog(feed(offer(1,name,brand=brand,description=description)))['products']['1']

    def test_confirmed_and_unknown_are_distinct_without_mutating_facts(self):
        p=self.product()
        self.assertEqual(assess(p,'plastic_budget','film'),(None,[]))
        p=self.product('Форсунка тестовая')
        frozen=deepcopy(p)
        issue,missing=assess(p,'plastic_budget','film')
        self.assertIsNone(issue)
        self.assertEqual(missing,['материал корпуса','исполнение закладной под отделку чаши'])
        self.assertEqual(p,frozen)
        self.assertEqual((p['material'],p['lining']),('',[]))
        issue,missing=assess(p,'steel316','film')
        self.assertIsNone(issue)
        self.assertIn('марка стали корпуса AISI 316',missing)

    def test_known_wrong_material_brand_or_finish_are_never_unknown(self):
        for p,profile,finish in [(self.product(),'steel316','film'),
                                  (self.product(brand='Hayward'),'plastic_budget','film'),
                                  (self.product('Форсунка ABS под бетон'),'plastic_budget','film'),
                                  (self.product('Форсунка AISI 304 под пленку'),'steel316','film'),
                                  (self.product('Форсунка корпус из нержавеющей стали'),'plastic_budget','film')]:
            issue,missing=assess(p,profile,finish)
            self.assertIsNotNone(issue)
            self.assertEqual(missing,[])
        self.assertIsNone(assess(self.product(brand=''),'plastic_budget','film')[0])
        self.assertIn('бренд для выбранного профиля КП',assess(self.product(brand=''),'plastic_budget','film')[1])

    def test_explicit_negative_lining_is_not_positive_evidence(self):
        p=self.product('Форсунка ABS для бетона, не под пленку')
        self.assertEqual(p['lining'],['concrete'])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        self.assertEqual(assess(p,'plastic_budget','tile'),(None,[]))
        p=self.product('Форсунка ABS не для пленки')
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        self.assertEqual(p['lining'],[])

    def test_conflicts_remain_blocked_after_xml_backfill(self):
        for name in ['Форсунка AISI304/316 под пленку',
                     'Форсунка ABS под пленку, не для пленки']:
            p=catalog.parse_catalog(feed(item(1,name,params=[('Материал корпуса','ABS'),('Тип отделки','пленка')])))['products']['1']
            self.assertTrue(p['specification_conflict'])
            self.assertEqual((p['material'],p['lining']),('',[]))
            self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        for p in [self.product('Форсунка AISI316 под пленку',description='Корпус ABS.'),
                  self.product('Форсунка ABS AISI316 под пленку')]:
            self.assertTrue(p['specification_conflict'])
            self.assertIsNotNone(assess(p,'steel316','film')[0])
        p=self.product(description='Корпус из ABS. Корпус из нержавеющей стали.')
        self.assertTrue(p['specification_conflict'])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])

    def test_dual_finish_and_hardware_are_not_fabricated_conflicts(self):
        p=self.product('Форсунка ABS под пленку и плитку')
        self.assertEqual(set(p['lining']),{'film','concrete'})
        self.assertEqual(assess(p,'plastic_budget','film'),(None,[]))
        self.assertEqual(assess(p,'plastic_budget','tile'),(None,[]))
        hardware=self.product('Форсунка ABS с винтами AISI316 под пленку')
        self.assertFalse(hardware.get('specification_conflict'))
        p=self.product(description='Корпус ABS. Винты из нержавеющей стали.')
        self.assertFalse(p.get('specification_conflict'))
        self.assertEqual(assess(p,'plastic_budget','film'),(None,[]))

    def test_old_frozen_title_and_category_guards(self):
        p=self.product('Форсунка тестовая')
        p.update(name='Форсунка AISI 304/316',specification_conflict=False)
        self.assertIsNotNone(assess(p,'steel316','film')[0])
        p=self.product('Форсунка AISI304 под плитку');p.update(material='',lining=[])
        self.assertIsNotNone(assess(p,'steel316','tile')[0])
        self.assertIsNotNone(assess(p,'steel304','film')[0])
        p=self.product();p['available']=False
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=self.product();p['embedded_role']=''
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=self.product();p['level_data_issue']='conflict'
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])

    def test_description_body_family_survives_without_inventing_steel_grade(self):
        p=self.product('Форсунка Test под пленку',description='Корпус из нержавеющей стали')
        self.assertEqual(p['body_family'],'steel')
        self.assertEqual(p['material'],'')
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        issue,missing=assess(p,'steel316','film')
        self.assertIsNone(issue)
        self.assertIn('марка стали корпуса AISI 316',missing)
        for description in ['Корпус из бронзы','Материал корпуса: латунь']:
            p=self.product('Форсунка Test под пленку',description=description)
            self.assertEqual(p['body_family'],'other_metal')
            self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
            self.assertIsNotNone(assess(p,'steel316','film')[0])
        p=self.product('Форсунка Test под пленку',description='<strong>Материал корпуса:</strong> ABS')
        self.assertEqual(p['body_family'],'plastic')
        self.assertIsNotNone(assess(p,'steel316','film')[0])
        self.assertEqual(assess(p,'plastic_budget','film'),(None,[]))

    def test_negative_finish_in_description_cannot_be_lost(self):
        p=self.product('Форсунка Test',description='Корпус из ABS. Не для пленки.')
        self.assertEqual(p['lining_denied'],['film'])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=self.product('Форсунка ABS под пленку',description='Не для пленки.')
        self.assertEqual(p['lining_denied'],['film'])
        self.assertTrue(p['specification_conflict'])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=self.product('Форсунка ABS под бетон',description='Не под пленку.')
        self.assertFalse(p.get('specification_conflict'))
        self.assertEqual(assess(p,'plastic_budget','tile'),(None,[]))

    def test_xml_generic_steel_and_description_disagreement_are_preserved(self):
        p=catalog.parse_catalog(feed(item(1,'Форсунка Test под пленку',params=[
            ('Материал корпуса','Нержавеющая сталь')],description='')))['products']['1']
        self.assertEqual(p['body_family'],'steel')
        self.assertEqual(p['material'],'')
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        self.assertIn('марка стали корпуса AISI 316',assess(p,'steel316','film')[1])
        p=catalog.parse_catalog(feed(item(1,'Форсунка Test под пленку',params=[
            ('Материал корпуса','ABS')],description='Корпус из нержавеющей стали.')))['products']['1']
        self.assertTrue(p['specification_conflict'])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
        p=catalog.parse_catalog(feed(item(1,'Форсунка Test',params=[
            ('Материал корпуса','ABS'),('Тип отделки','пленка')],description='Не под пленку.')))['products']['1']
        self.assertEqual(p['lining_denied'],['film'])
        self.assertTrue(p['specification_conflict'])
        self.assertIsNotNone(assess(p,'plastic_budget','film')[0])

    def test_hardware_grade_does_not_override_or_conflict_with_body(self):
        for grade in ('201','430','304/201'):
            p=self.product('Форсунка корпус ABS с винтами AISI'+grade+' под пленку')
            self.assertFalse(p.get('specification_conflict'))
            self.assertIsNone(assess(p,'plastic_budget','film')[0])
        p=self.product('Форсунка корпус AISI304 с винтами AISI201 под пленку')
        self.assertIsNotNone(assess(p,'steel316','film')[0])
        self.assertIsNone(assess(p,'steel304','film')[0])

    def test_explicit_body_grade_in_description_is_not_unknown_or_hardware(self):
        for grade in ('304','316'):
            with self.subTest(grade=grade):
                p=self.product('Форсунка Test под пленку',description='Материал корпуса: AISI '+grade+'.')
                self.assertEqual((p['body_family'],p['material']),('steel','steel'+grade))
                self.assertEqual(assess(p,'steel'+grade,'film'),(None,[]))
                other='steel316' if grade=='304' else 'steel304'
                self.assertIsNotNone(assess(p,other,'film')[0])
        p=self.product('Форсунка Test под пленку',description='Материал корпуса: AISI 201.')
        self.assertTrue(p['specification_conflict'])
        self.assertIsNotNone(assess(p,'steel316','film')[0])
        p=self.product('Форсунка Test под пленку',description='Корпус из ABS. Винты AISI316.')
        self.assertEqual((p['body_family'],p['material']),('plastic','plastic'))
        self.assertEqual(assess(p,'plastic_budget','film'),(None,[]))
        p=self.product('Форсунка корпус ABS с винтами AISI316 под пленку')
        self.assertEqual(p['material'],'plastic')
        p=catalog.parse_catalog(feed(item(1,'Форсунка Test под пленку',params=[
            ('Материал корпуса','AISI316')],description='Материал корпуса: AISI304.')))['products']['1']
        self.assertTrue(p['specification_conflict'])
        self.assertEqual(p['material'],'')
        self.assertIsNotNone(assess(p,'steel316','film')[0])

    def test_negative_finish_lists_include_every_item_but_stop_at_sentence(self):
        from pool_service.services.estimate_embedded_candidates import lining_claims
        for words in ('плитки и пленки','плитки, пленки, мозаики и бетона',
                      'плитки/пленки/мозаики/бетона', 'пленки или плитки'):
            sentence='Не подходит для '+words
            with self.subTest(words=words):
                self.assertEqual(lining_claims(sentence),(set(),{'film','concrete'}))
                for name,description in [('Форсунка ABS '+sentence,''),('Форсунка ABS',sentence)]:
                    p=self.product(name,description=description)
                    self.assertEqual(p['lining_denied'],['concrete','film'])
                    self.assertIsNotNone(assess(p,'plastic_budget','film')[0])
                    self.assertIsNotNone(assess(p,'plastic_budget','tile')[0])
        self.assertEqual(lining_claims('Не подходит для плитки. Для пленки.'),({'film'},{'concrete'}))
        p=self.product('Форсунка ABS не подходит для плитки. Под пленку.')
        self.assertEqual(p['lining_denied'],['concrete'])
        self.assertEqual(assess(p,'plastic_budget','film'),(None,[]))

    def test_rejected_body_or_finish_withdraws_auto_but_preserves_manual_amount(self):
        import json
        from decimal import Decimal
        from pool_service.services.pool_estimates import generate, evaluate, recalculate
        from .test_pool_estimates import INPUTS
        from .test_estimate_candidates import target
        cases = [
            ('premium', item(1,'Форсунка AISI316 под пленку',price='100'),
             item(1,'Форсунка Test под пленку',price='100',params=[('Материал корпуса','AISI316')],
                  description='Материал корпуса: AISI304.')),
            ('economy', item(1,'Форсунка ABS под пленку',price='100'),
             item(1,'Форсунка ABS не подходит для плитки и пленки',price='100')),
        ]
        from itertools import product
        from .test_estimate_level_control import generate as generate_legacy
        for shared_mode, (tier, good_xml, bad_xml) in product((False, True), cases):
            with self.subTest(tier=tier, shared_mode=shared_mode):
                # Saved three-inlet quotes retain their historical 300 total;
                # new quotes add a fourth physical fitting for the level line.
                factory = generate if shared_mode else generate_legacy
                initial,_=catalog.auto_select(factory({**INPUTS,'finish':'film'}),catalog.parse_catalog(feed(good_xml)))
                quantity = Decimal(4 if shared_mode else 3)
                self.assertEqual(Decimal(target(initial,'inlet',tier)['amount']), quantity * 100)
                if shared_mode:
                    self.assertEqual(target(initial,'inlet',tier)['filtration_quantity'], '3.000')
                    self.assertEqual(target(initial,'inlet',tier)['level_quantity'], '1.000')
                before_total=Decimal(initial['variants'][tier]['total'])
                changed=catalog.parse_catalog(feed(bad_xml))
                # New project parameters and a save/load-like JSON roundtrip must
                # not hide a newly proven catalogue contradiction.
                restored=evaluate(json.loads(json.dumps(initial)))
                result,_=catalog.auto_select(recalculate(restored,restored['inputs']),changed)
                row=target(result,'inlet',tier)
                self.assertIsNone(row['catalog'])
                self.assertIsNone(row['price'])
                self.assertIsNone(row['amount'])
                self.assertIn('отклонён',row['selection_note'])
                self.assertEqual(Decimal(result['variants'][tier]['total']),before_total-quantity * 100)
                roundtrip=evaluate(json.loads(json.dumps(recalculate(result,result['inputs']))))
                self.assertIsNone(target(roundtrip,'inlet',tier)['amount'])
                self.assertEqual(roundtrip['variants'][tier]['total'],result['variants'][tier]['total'])
                manual=deepcopy(initial)
                protected=target(manual,'inlet',tier)
                protected['catalog']['selection']='manual'
                protected.update(price='123.45',unit_price_manual=True)
                kept,_=catalog.auto_select(manual,changed)
                protected=target(kept,'inlet',tier)
                self.assertEqual(protected['price'],'123.45')
                self.assertEqual(Decimal(protected['amount']),quantity * Decimal('123'))
                self.assertIsNotNone(protected['catalog'])
                self.assertIn('Текущая выгрузка',protected['selection_note'])
                self.assertEqual(Decimal(target(evaluate(kept),'inlet',tier)['amount']),quantity * Decimal('123'))
