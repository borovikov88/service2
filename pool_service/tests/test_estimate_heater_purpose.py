"""Synthetic heat-pump purpose fixtures. No live requests or supplier prices."""
from copy import deepcopy
from decimal import Decimal
import json

from django.test import SimpleTestCase
from django.template.loader import render_to_string

from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_estimate_catalog import feed, offer
from .test_estimate_candidates import target
from .test_pool_estimates import INPUTS


HEAT_INPUTS = {**INPUTS, 'heating':'heatpump', 'heating_types':['heatpump'],
               'heating_power':'18', 'power':'230'}
HOME_URL = ('https://shop.aqualine22.ru/basseini/274-oborudovanie-dlya-bassejnov/'
            'teplovye-nasosy-dlya-doma/teplovoj-nasos-dlya-doma-aquaviva-avh18s-183-kvt.html')
HOME_NAME = 'Тепловой насос для дома Aquaviva AVH18S (18.3 кВт)'
POOL_NAME = 'Тепловой насос для бассейна Pool Test 20 кВт 220В'


def pool_offer(pid=2, price='300'):
    return offer(pid, POOL_NAME, price=price, description='Для бассейна рядом с домом.')


def domestic_feed(signal, include_pool=False):
    name = HOME_NAME if signal == 'name' else 'Тепловой насос Test 20 кВт 220В'
    description = '<p>Для <strong>отопления дома</strong>.</p>' if signal == 'description' else ''
    xml = offer(1, name, price='100', description=description,
                url=HOME_URL if signal == 'url' else None)
    if signal in {'category','ancestor'}:
        xml = xml.replace('</offer>', '<categoryId>7</categoryId></offer>')
    payload = feed(xml, *([pool_offer()] if include_pool else []))
    if signal == 'category':
        categories = '<categories><category id="7">Тепловые насосы для дома</category></categories>'
        payload = payload.replace(b'<offers>', categories.encode() + b'<offers>')
    elif signal == 'ancestor':
        categories = ('<categories><category id="5">Оборудование для бассейнов</category>'
                      '<category id="6" parentId="5">Тепловые насосы для дома</category>'
                      '<category id="7" parentId="6">Инверторные модели</category></categories>')
        payload = payload.replace(b'<offers>', categories.encode() + b'<offers>')
    return payload


class HeatPumpPurposeTests(SimpleTestCase):
    def test_domestic_adjectives_are_blocked_without_matching_pool_near_house(self):
        for phrase in ('для отопления частного дома', 'для отопления загородного дома',
                       'для обогрева индивидуального жилого дома', 'для дачного дома'):
            for source in ('name', 'description', 'category'):
                with self.subTest(phrase=phrase, source=source):
                    name = 'Тепловой насос Test 20 кВт 220В'
                    xml = offer(1, name + (' ' + phrase if source == 'name' else ''),
                                description=phrase if source == 'description' else '')
                    if source == 'category':
                        xml = xml.replace('</offer>', '<categoryId>7</categoryId></offer>')
                    payload = feed(xml)
                    if source == 'category':
                        categories = '<categories><category id="7">Тепловые насосы ' + phrase + '</category></categories>'
                        payload = payload.replace(b'<offers>', categories.encode()+b'<offers>')
                    c = catalog.parse_catalog(payload)
                    result,_ = catalog.auto_select(generate(HEAT_INPUTS),c)
                    for tier in result['variants']:
                        self.assertIsNone(target(result,'heater',tier)['catalog'])
                    self.assert_purpose_warning(catalog.candidate_reason(c['products']['1'],result['inputs'],Decimal('8')))
        for phrase in ('Для отопления бассейна возле частного дома.',
                       'Для бассейна загородного дома.',
                       'Не предназначен для отопления частного дома.',
                       'Для частного бассейна у загородного дома.'):
            with self.subTest(allowed=phrase):
                c = catalog.parse_catalog(feed(offer(1,POOL_NAME,description=phrase)))
                result,_ = catalog.auto_select(generate(HEAT_INPUTS),c)
                self.assertEqual(target(result,'heater')['catalog']['id'],'1')
        from pool_service.services.estimate_heater_purpose import purpose_note
        for adjective in ('chastnogo','zagorodnogo'):
            self.assertTrue(purpose_note(url='https://shop.aqualine22.ru/teplovoy-nasos-dlya-otopleniya-'+adjective+'-doma.html'))
        self.assertFalse(purpose_note(url='https://shop.aqualine22.ru/teplovoy-nasos-dlya-otopleniya-basseyna-zagorodnogo-doma.html'))

    def test_postposed_denial_is_local_to_domestic_claim(self):
        for description in ('Для бассейна; для отопления дома не подходит.',
                            'Для отопления частного дома не предназначен.',
                            'Для отопления загородного дома — не применяется.'):
            with self.subTest(description=description):
                c = catalog.parse_catalog(feed(offer(1,POOL_NAME,description=description)))
                result,_ = catalog.auto_select(generate(HEAT_INPUTS),c)
                self.assertFalse(c['products']['1']['heater_purpose_note'])
                for tier in result['variants']:
                    self.assertEqual(target(result,'heater',tier)['catalog']['id'],'1')
        for description in ('Для дома, не подходит для бассейна',
                            'Для отопления частного дома, не предназначен для бассейна'):
            with self.subTest(description=description):
                c = catalog.parse_catalog(feed(offer(1,'Тепловой насос Test 20 кВт 220В',description=description)))
                result,_ = catalog.auto_select(generate(HEAT_INPUTS),c)
                for tier in result['variants']:
                    self.assertIsNone(target(result,'heater',tier)['catalog'])
                self.assert_purpose_warning(catalog.candidate_reason(c['products']['1'],result['inputs'],Decimal('8')))

    def assert_purpose_warning(self, text):
        self.assertRegex((text or '').casefold(), r'отопления|теплообменник')

    def saved_home_selection(self, signal='name', selection='auto'):
        c = catalog.parse_catalog(domestic_feed(signal))
        snapshot = generate(HEAT_INPUTS)
        for tier in snapshot['variants']:
            catalog.apply_product(target(snapshot,'heater',tier), c['products']['1'], c, selection)
        return evaluate(snapshot), c

    def test_each_explicit_home_signal_blocks_auto_but_keeps_manual_search(self):
        for signal in ('name','category','ancestor','url','description'):
            with self.subTest(signal=signal):
                c = catalog.parse_catalog(domestic_feed(signal))
                product = c['products']['1']
                self.assertEqual((product['kind'],product['heater_type']),('heater','heatpump'))
                s = generate(HEAT_INPUTS)
                self.assert_purpose_warning(catalog.candidate_reason(product,s['inputs'],Decimal('8')))
                result,_ = catalog.auto_select(s,c)
                for tier in result['variants']:
                    self.assertIsNone(target(result,'heater',tier)['catalog'])
                    self.assertIsNone(target(result,'heater',tier)['amount'])
                    self.assertEqual(result['variants'][tier]['total'],s['variants'][tier]['total'])
                found = catalog.search_catalog(c,'','heater','',s['inputs'],Decimal('8'))
                self.assertEqual(found['total'],1)
                self.assertEqual(found['products'][0]['id'],'1')
                self.assert_purpose_warning(found['products'][0]['warning'])

    def test_pool_model_remains_eligible_even_with_home_word_in_description(self):
        c = catalog.parse_catalog(domestic_feed('name',include_pool=True))
        initial = generate(HEAT_INPUTS)
        result,_ = catalog.auto_select(initial,c)
        for tier in result['variants']:
            self.assertEqual(target(result,'heater',tier)['catalog']['id'],'2')
            self.assertEqual(target(result,'heater',tier)['amount'],'300.00')
            self.assertEqual(Decimal(result['variants'][tier]['total'])-Decimal(initial['variants'][tier]['total']),Decimal('300'))
        again,count = catalog.auto_select(evaluate(json.loads(json.dumps(result))),c)
        self.assertEqual(count,0)
        self.assertEqual(again['variants'],result['variants'])

    def test_existing_auto_home_model_replaced_or_withdrawn_with_exact_totals(self):
        for signal in ('name','ancestor','url','description'):
            with self.subTest(signal=signal):
                initial,c = self.saved_home_selection(signal)
                baseline = {tier:Decimal(v['total']) for tier,v in initial['variants'].items()}
                restored = recalculate(evaluate(json.loads(json.dumps(initial))),initial['inputs'])
                replacement_catalog = catalog.parse_catalog(domestic_feed(signal,include_pool=True))
                replaced,_ = catalog.auto_select(restored,replacement_catalog)
                for tier in replaced['variants']:
                    self.assertEqual(target(replaced,'heater',tier)['catalog']['id'],'2')
                    self.assertEqual(Decimal(replaced['variants'][tier]['total']),baseline[tier]+Decimal('200'))
                removed,_ = catalog.auto_select(restored,c)
                for tier in removed['variants']:
                    row = target(removed,'heater',tier)
                    self.assertIsNone(row['catalog'])
                    self.assertIsNone(row['amount'])
                    self.assertEqual(Decimal(removed['variants'][tier]['total']),baseline[tier]-Decimal('100'))
                    self.assert_purpose_warning(row['selection_note'])
                stable = evaluate(json.loads(json.dumps(recalculate(removed,removed['inputs']))))
                self.assertEqual([v['total'] for v in stable['variants'].values()],
                                 [v['total'] for v in removed['variants'].values()])

    def test_protected_manual_price_quantity_and_choice_stay_with_warning(self):
        for signal in ('name','category','description'):
            with self.subTest(signal=signal):
                manual,c = self.saved_home_selection(signal,selection='manual')
                for tier in manual['variants']:
                    target(manual,'heater',tier).update(price='123.45',quantity='2',auto_quantity=False,
                                                       unit_price_manual=True,selection_locked=True)
                manual = evaluate(manual)
                protected = deepcopy(manual)
                current = catalog.parse_catalog(domestic_feed(signal,include_pool=True))
                result,_ = catalog.auto_select(recalculate(manual,manual['inputs']),current)
                for tier in result['variants']:
                    row = target(result,'heater',tier)
                    before = target(protected,'heater',tier)
                    for field in ('catalog','price','quantity','model','name'):
                        self.assertEqual(row[field],before[field])
                    self.assertEqual(row['amount'],'246.90')
                    self.assertEqual(result['variants'][tier]['total'],protected['variants'][tier]['total'])
                    self.assert_purpose_warning(row['selection_note'])
                    self.assert_purpose_warning(' '.join(result['variants'][tier]['warnings']))
                printed = render_to_string('pool_service/estimates/print.html',{'version':{'snapshot':result,'number':1}})
                self.assert_purpose_warning(printed)

    def test_loading_legacy_snapshot_does_not_silently_change_price_or_totals(self):
        legacy,_ = self.saved_home_selection('name')
        # Old saved rows predate persisted purpose metadata. Original name/URL
        # remain enough to warn, but evaluate/load must never silently reprice.
        for tier in legacy['variants']:
            target(legacy,'heater',tier)['catalog'].pop('heater_purpose_note',None)
        before = deepcopy(legacy)
        loaded = evaluate(json.loads(json.dumps(legacy)))
        recalculated = recalculate(loaded,loaded['inputs'])
        for snapshot in (loaded,recalculated):
            for tier in snapshot['variants']:
                row = target(snapshot,'heater',tier)
                self.assertEqual(row['price'],'100.00')
                self.assertEqual(row['amount'],'100.00')
                self.assertEqual(snapshot['variants'][tier]['total'],before['variants'][tier]['total'])
                self.assert_purpose_warning(row['selection_note'])

    def test_pool_heating_url_generic_home_category_and_negated_home_are_not_domestic(self):
        pool_url = 'https://shop.aqualine22.ru/teplovoy-nasos-dlya-otopleniya-basseyna.html'
        categories = ('<categories><category id="5">Товары для дома</category>'
                      '<category id="6" parentId="5">Бассейны</category>'
                      '<category id="7" parentId="6">Тепловые насосы</category></categories>')
        category_xml = pool_offer(pid=1).replace('</offer>','<categoryId>7</categoryId></offer>')
        allowed = [
            feed(offer(1,POOL_NAME,price='300',description='',url=pool_url)),
            feed(category_xml).replace(b'<offers>',categories.encode()+b'<offers>'),
            feed(offer(1,POOL_NAME,price='300',description='Для бассейна, не предназначен для отопления дома.')),
        ]
        for payload in allowed:
            with self.subTest(payload=payload.decode()[:160]):
                c=catalog.parse_catalog(payload)
                s=generate(HEAT_INPUTS)
                self.assertIsNone(catalog.candidate_reason(c['products']['1'],s['inputs'],Decimal('8')))
                result,_=catalog.auto_select(s,c)
                for tier in result['variants']:
                    self.assertEqual(target(result,'heater',tier)['catalog']['id'],'1')
                    self.assertEqual(target(result,'heater',tier)['amount'],'300.00')
        blocked=catalog.parse_catalog(feed(offer(1,'Тепловой насос Test 20 кВт 220В',price='100',
            description='Для дома, не подходит для бассейна.')))
        s=generate(HEAT_INPUTS)
        self.assert_purpose_warning(catalog.candidate_reason(blocked['products']['1'],s['inputs'],Decimal('8')))
        result,_=catalog.auto_select(s,blocked)
        for tier in result['variants']:
            self.assertIsNone(target(result,'heater',tier)['catalog'])
