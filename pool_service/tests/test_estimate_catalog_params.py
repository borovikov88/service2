"""Synthetic XML, prices and compatibility declarations; never live shop facts."""
from copy import deepcopy
from decimal import Decimal
from xml.sax.saxutils import escape
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_estimate_catalog import offer, feed
from .test_pool_estimates import INPUTS, block


def item(pid, name, sku='', brand='Aquaviva', price='100', params=(), description=''):
    extras = '<vendorCode>' + sku + '</vendorCode>' + ''.join(
        '<param name="' + escape(k) + '">' + escape(v) + '</param>' for k, v in params)
    return offer(pid, name, price=price, brand=brand, description=description).replace('</offer>', extras + '</offer>')


def pair(s, tier='economy'):
    return [r for r in block(s, 'embedded', tier)['rows'] if r['kind'] in {'level_regulator', 'level_inlet'}]


REGS = [item(901, 'Регулятор уровня Aquaviva RO-7', '16791', price='4000'),
        item(902, 'Регулятор уровня Kripsol RGP.C', '528', 'Kripsol', '11000'),
        item(903, 'Регулятор уровня Hayward 3150', '32304', 'Hayward', '13000')]


def nozzle(pid=904, skus='16791,528,32304', price='100', brand='Aquaviva', material='ABS', lining='пленка'):
    return item(pid, 'Форсунка тестовая', str(pid), brand, price,
                [('Материал корпуса', material), ('Тип отделки', lining),
                 ('Совместимые регуляторы уровня (артикулы)', skus)])


class StructuredFactsTests(SimpleTestCase):
    def parse(self, params=(), name='Скиммер тестовый', description=''):
        return catalog.parse_catalog(feed(item(1, name, params=params, description=description)))['products']['1']

    def test_structured_body_grade_lining_and_boolean(self):
        p = self.parse([('Материал корпуса', 'Нержавеющая сталь'), ('Марка стали корпуса', 'AISI 316L'),
                        ('Тип отделки', 'пленка / плитка'), ('Регулятор уровня в комплекте', 'Да')])
        self.assertEqual((p['material'], p['lining'], p['level_control']), ('steel316', ['concrete','film'], 'included'))
        p = self.parse([('Материал корпуса','ABS'), ('Встроенный регулятор уровня','нет')])
        self.assertEqual((p['material'],p['level_control']), ('plastic','absent'))
        self.assertEqual(self.parse()['level_control'], 'unknown')

    def test_screws_generic_material_and_bare_grade_do_not_prove_body(self):
        for params in [[('Материал винтов','AISI 316')], [('Материал','AISI 316')], [('Марка стали','316')]]:
            self.assertEqual(self.parse(params)['material'], '')
        p = self.parse([('Материал корпуса','ABS'), ('Материал винтов','316')])
        self.assertEqual(p['material'],'plastic')

    def test_conflicting_sources_duplicates_and_unrecognized_values(self):
        for params in [[('Материал корпуса','ABS'),('Материал корпуса','AISI 316')],
                       [('Материал корпуса','ABS'),('Марка стали корпуса','316')]]:
            p=self.parse(params)
            self.assertEqual(p['material'],'')
            self.assertTrue(p['specification_conflict'])
        p=self.parse([('Тип отделки','не для пленки'),('Встроенный регулятор уровня','опция')])
        self.assertEqual(p['lining'],[])
        self.assertEqual(p['level_control'],'unknown')
        p=self.parse([('Материал корпуса','ABS')], name='Скиммер AISI 316 под пленку')
        self.assertEqual(p['material'],'')
        p=self.parse([('Марка стали корпуса','AISI 316')], name='Скиммер ABS под пленку')
        self.assertEqual(p['material'],'')
        self.assertTrue(p['specification_conflict'])
        p=self.parse([('Тип отделки','плитка')], name='Скиммер ABS под пленку')
        self.assertEqual(p['lining'],[])

    def test_existing_enrichment_survives_but_conflict_blocks(self):
        for params, expected in [((), 'plastic'), ([('Материал корпуса','AISI 316')], '')]:
            p=catalog.parse_catalog(feed(item(1,'Форсунка Aquaviva EM4412','16785',params=params)))['products']['1']
            self.assertEqual(p['material'],expected)
            self.assertTrue(p['specification_source'])

    def test_builtin_param_roundtrip_and_manual_override(self):
        s=generate(INPUTS)
        skimmer=next(r for r in block(s,'embedded')['rows'] if r['kind']=='skimmer')
        c=catalog.parse_catalog(feed(item(1,'Скиммер ABS под пленку',params=[('Встроенный регулятор уровня','да')])))
        catalog.apply_product(skimmer,c['products']['1'],c)
        checked=evaluate(s)
        self.assertTrue(all(r['excluded'] for r in pair(checked)))
        checked=recalculate(checked,checked['inputs'])
        self.assertTrue(all(r['excluded'] for r in pair(checked)))
        target=next(r for r in block(checked,'embedded')['rows'] if r['kind']=='skimmer')
        target.update(level_status='absent',level_evidence='Уточнён паспорт поставки')
        self.assertTrue(all(not r['excluded'] for r in pair(evaluate(checked))))


class LevelAutoTests(SimpleTestCase):
    def start(self):
        s=generate({**INPUTS,'finish':'film'})
        # Deliberately same fitting profile in all tiers to isolate price ranking.
        for v in s['variants'].values(): v['embedded_profile']='plastic_budget'
        return s

    def test_one_two_three_price_tiers_and_totals(self):
        for count, expected in [(1,['901']*3), (2,['901','902','902']), (3,['901','902','903'])]:
            s=self.start(); c=catalog.parse_catalog(feed(*REGS[:count],nozzle()))
            # Isolate this subsystem; general automatic selection isn't under test here.
            from pool_service.services.estimate_level_control import fill
            before=evaluate(deepcopy(s))
            self.assertEqual(fill(s,c),6)
            result=evaluate(s)
            for tier, pid in zip(result['variants'],expected):
                reg,inlet=pair(result,tier)
                self.assertEqual(reg['catalog']['id'],pid)
                self.assertEqual(inlet['catalog']['id'],'904')
                self.assertEqual(inlet['kind'],'level_inlet')
                delta=Decimal(result['variants'][tier]['total'])-Decimal(before['variants'][tier]['total'])
                self.assertEqual(delta,Decimal(reg['price'])+Decimal('100'))
            self.assertEqual(fill(result,c),0)
            self.assertEqual(evaluate(result)['variants']['economy']['total'],result['variants']['economy']['total'])

    def test_complete_compatible_pair_precedes_cheaper_unpaired_regulator(self):
        from pool_service.services.estimate_level_control import fill
        s=self.start(); c=catalog.parse_catalog(feed(*REGS,nozzle(skus='528')))
        fill(s,c)
        self.assertEqual([pair(s,k)[0]['catalog']['sku'] for k in s['variants']], ['528']*3)

    def test_no_inlet_guess_and_specific_warning(self):
        from pool_service.services.estimate_level_control import fill
        s=self.start(); c=catalog.parse_catalog(feed(*REGS,nozzle(skus='999')))
        fill(s,c); s=evaluate(s)
        self.assertEqual(pair(s)[0]['catalog']['sku'],'16791')
        self.assertIsNone(pair(s)[1]['catalog'])
        self.assertIn('арт. 16791 не подтверждена отдельная форсунка', ' '.join(s['variants']['economy']['warnings']))

    def test_wrong_finish_material_or_conflict_cannot_fill_inlet(self):
        from pool_service.services.estimate_level_control import fill
        for bad in [nozzle(lining='плитка'), nozzle(material='AISI 316'), nozzle(material='неизвестно')]:
            s=self.start(); fill(s,catalog.parse_catalog(feed(REGS[0],bad)))
            self.assertIsNone(pair(s)[1]['catalog'])

    def test_manual_regulator_price_quantity_exclusion_and_old_snapshots_preserved(self):
        from pool_service.services.estimate_level_control import fill
        for update in [dict(price='12',unit_price_manual=True),dict(auto_quantity=False,quantity='2'),dict(excluded=True),dict(selection_locked=True)]:
            s=self.start();pair(s)[0].update(update); before=deepcopy(pair(s)[0])
            fill(s,catalog.parse_catalog(feed(*REGS,nozzle())))
            self.assertEqual(pair(s)[0],before)
        s=self.start();s.pop('level_system')
        for v in s['variants'].values():
            for b in v['blocks']: b['rows']=[r for r in b['rows'] if r['kind'] not in {'level_regulator','level_inlet'}]
        old=deepcopy(s);fill(s,catalog.parse_catalog(feed(*REGS,nozzle())))
        self.assertEqual(s,old)

    def test_builtin_hides_auto_pair_but_retains_manual_with_duplicate_warning(self):
        from pool_service.services.estimate_level_control import fill
        s=self.start();c=catalog.parse_catalog(feed(*REGS,nozzle()))
        fill(s,c)
        reg=pair(s)[0];reg['catalog']['selection']='manual'
        skimmer=next(r for r in block(s,'embedded')['rows'] if r['kind']=='skimmer')
        skimmer.update(level_status='included',level_evidence='Паспорт')
        fill(s,c);s=evaluate(s)
        self.assertFalse(pair(s)[0]['excluded']);self.assertTrue(pair(s)[1]['excluded'])
        self.assertIn('Конфликт комплектации',' '.join(s['variants']['economy']['warnings']))

    def test_normalize_preserves_pair_evidence_and_changed_finish_warns(self):
        from pool_service.services.estimate_level_control import fill
        s=self.start();c=catalog.parse_catalog(feed(REGS[0],nozzle()))
        fill(s,c);s=evaluate(s)
        self.assertEqual(pair(s)[1]['catalog']['level_regulator_skus'],['16791','32304','528'])
        changed=recalculate(s,{**s['inputs'],'finish':'tile'})
        self.assertIn('не подтверждена отдельная форсунка',' '.join(changed['variants']['economy']['warnings']))

    def test_conflicting_manual_regulator_never_autofills_inlet(self):
        from pool_service.services.estimate_level_control import fill
        s=self.start();c=catalog.parse_catalog(feed(REGS[0],nozzle()))
        catalog.apply_product(pair(s)[0],c['products']['901'],c)
        pair(s)[0]['catalog']['specification_conflict']=True
        fill(s,c);s=evaluate(s)
        self.assertIsNone(pair(s)[1]['catalog'])
        self.assertEqual(pair(s)[0]['catalog']['selection'],'manual')
        self.assertIn('конфликт характеристик',' '.join(s['variants']['economy']['warnings']))

class HydraulicLevelTests(SimpleTestCase):
    def products(self, inlet_changes=(), regulator_changes=(), skus=''):
        # Explicit synthetic interface facts, not assertions about live products.
        common=[('Тип подключения линии уровня','клеевое внутреннее'),
                ('Диаметр подключения линии уровня','50 мм')]
        fitting=[('Назначение линии уровня','отдельная сообщающаяся линия'),
                 ('Свободный проход линии уровня','да'),('Материал корпуса','ABS'),
                 ('Тип отделки','пленка')]
        def merged(base, changes):
            return list({**dict(base), **dict(changes)}.items())
        c=catalog.parse_catalog(feed(
            item(901,'Регулятор уровня Aquaviva RO-7','16791',params=merged(common,regulator_changes)),
            item(904,'Форсунка тестовая','904',params=merged(common+fitting+
                 ([('Совместимые регуляторы уровня (артикулы)',skus)] if skus else []),inlet_changes))))
        return c, c['products']['904'], c['products']['901']

    def test_technical_match_without_sku_populates_distinct_row_and_roundtrips(self):
        from pool_service.services.estimate_level_control import fill
        c,p,r=self.products()
        s=generate({**INPUTS,'finish':'film'})
        before_total=Decimal(s['variants']['economy']['total'])
        before=[deepcopy(row) for row in block(s,'embedded')['rows'] if row['kind']=='inlet']
        self.assertEqual(fill(s,c,only='economy'),2)
        result=evaluate(s)
        self.assertEqual(Decimal(result['variants']['economy']['total'])-before_total,Decimal('200'))
        self.assertEqual(pair(result)[1]['catalog']['id'],'904')
        self.assertEqual(pair(result)[1]['catalog']['level_diameter'],'50 mm')
        self.assertEqual(pair(result)[1]['catalog']['level_regulator_skus'],[])
        self.assertEqual([row for row in block(s,'embedded')['rows'] if row['kind']=='inlet'],before)
        self.assertEqual(fill(result,c,only='economy'),0)
        self.assertEqual(pair(result)[1]['kind'],'level_inlet')

    def test_unknown_is_not_incompatible_and_gives_specific_missing_field(self):
        from pool_service.services.estimate_level_control import inlet_reason
        c,p,r=self.products()
        p['level_passage']=''
        reason=inlet_reason(p,r,{'finish':'film'},'plastic_budget')
        self.assertIn('свободный проход форсунки',reason)
        self.assertIn('не доказанная несовместимость',reason)
        p['level_passage']='yes';r['level_connection']=''
        self.assertIn('тип присоединения регулятора',inlet_reason(p,r,{'finish':'film'},'plastic_budget'))

    def test_allowlist_never_overrides_filtration_closed_or_mismatched_line(self):
        from pool_service.services.estimate_level_control import inlet_reason
        for change, expected in [([('Назначение линии уровня','напорная линия фильтрации')],'напорная'),
                                 ([('Свободный проход линии уровня','нет')],'закрыт'),
                                 ([('Диаметр подключения линии уровня','40 мм')],'Диаметры'),
                                 ([('Тип подключения линии уровня','bsp внутренняя')],'Типы')]:
            c,p,r=self.products(change,skus='16791')
            self.assertIn(expected,inlet_reason(p,r,{'finish':'film'},'plastic_budget'))

    def test_optional_sku_absence_or_different_list_does_not_block_technical_match(self):
        from pool_service.services.estimate_level_control import inlet_reason
        for skus in ('','528'):
            c,p,r=self.products(skus=skus)
            self.assertIsNone(inlet_reason(p,r,{'finish':'film'},'plastic_budget'))

    def test_diameter_units_and_unknown_values_never_assumed(self):
        from pool_service.services.estimate_level_control import inlet_reason
        for value in ('50','DN50','1 1/2','неизвестно'):
            c,p,r=self.products([('Диаметр подключения линии уровня',value)],skus='16791')
            self.assertEqual(p['level_diameter'],'')
            self.assertIn('не распознано',inlet_reason(p,r,{'finish':'film'},'plastic_budget'))
        c,p,r=self.products([('Диаметр подключения линии уровня','1.5 inch')])
        self.assertIn('Диаметры',inlet_reason(p,r,{'finish':'film'},'plastic_budget'))

    def test_duplicate_conflicting_hydraulics_not_overridden_by_allowlist(self):
        from pool_service.services.estimate_level_control import inlet_reason
        c,p,r=self.products()
        p=catalog.parse_catalog(feed(item(904,'Форсунка тестовая',params=[
            ('Материал корпуса','ABS'),('Тип отделки','пленка'),
            ('Свободный проход линии уровня','да'),('Свободный проход линии уровня','нет'),
            ('Совместимые регуляторы уровня (артикулы)','16791') ])))['products']['904']
        self.assertEqual(p['level_data_issue'],'conflict')
        self.assertIn('Противоречивые',inlet_reason(p,r,{'finish':'film'},'plastic_budget'))

class ReviewRegressionTests(SimpleTestCase):
    start = LevelAutoTests.start
    def test_enrichment_conflict_is_sticky_through_params_and_roundtrip(self):
        from pool_service.services import estimate_embedded as embedded
        c=catalog.parse_catalog(feed(item(1,'Форсунка Aquaviva EM4412 AISI316','16785',
            params=[('Материал корпуса','ABS'),('Тип отделки','пленка')])))
        p=c['products']['1']
        self.assertTrue(p['specification_conflict'])
        self.assertEqual((p['material'],p['lining']),('',[]))
        self.assertFalse(embedded.matches(p,'plastic_budget','film'))
        s=self.start();catalog.apply_product(pair(s)[1],p,c)
        self.assertTrue(pair(evaluate(s))[1]['catalog']['specification_conflict'])
        self.assertIn('Конфликт характеристик',catalog.candidate_reason(p,s['inputs'],Decimal('1')))

    def test_protected_inlet_ranks_its_pairs_not_unrelated_live_pairs(self):
        from pool_service.services.estimate_level_control import fill
        s=self.start(); frozen=catalog.parse_catalog(feed(nozzle(skus='16791,528')))
        catalog.apply_product(pair(s)[1],frozen['products']['904'],frozen)
        before=evaluate(deepcopy(s)); original=deepcopy(pair(before)[1])
        live=catalog.parse_catalog(feed(*REGS[:2],nozzle(905,skus='528')))
        fill(s,live,only='economy');result=evaluate(s)
        self.assertEqual(pair(result)[0]['catalog']['sku'],'16791')
        self.assertEqual(pair(result)[1],original)
        self.assertEqual(Decimal(result['variants']['economy']['total'])-Decimal(before['variants']['economy']['total']),Decimal('4000'))
        self.assertEqual(fill(result,live,only='economy'),0)

    def test_conflicting_regulator_diameter_excluded_with_specific_warning(self):
        bad=item(901,'Регулятор уровня Aquaviva RO-7','16791',price='4000',params=[
            ('Диаметр подключения линии уровня','50 мм'),('Диаметр подключения линии уровня','40 мм')])
        c=catalog.parse_catalog(feed(bad,REGS[1],nozzle(skus='16791,528')))
        self.assertEqual(c['products']['901']['level_data_issue'],'conflict')
        s,count=catalog.auto_select(self.start(),c)
        self.assertEqual(pair(s)[0]['catalog']['sku'],'528')
        reason=catalog.candidate_reason(c['products']['901'],s['inputs'],Decimal('1'))
        self.assertIn('Противоречивые',reason)
        self.assertIn('диаметр подключения',reason)
        # A saved manual conflicting selection stays visible with its exact issue.
        catalog.apply_product(pair(s)[0],c['products']['901'],c)
        result=evaluate(s)
        self.assertIn('диаметр подключения',' '.join(result['variants']['economy']['warnings']))
