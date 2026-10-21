"""Synthetic per-area prices, not supplier quotations."""
from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_estimate_catalog import feed, offer
from .test_pool_estimates import INPUTS


def geo(snapshot, tier='economy'):
    return next(r for b in snapshot['variants'][tier]['blocks'] for r in b['rows'] if r['id']=='finish_geotextile')


class GeotextileTests(SimpleTestCase):
    def test_square_metre_price_stock_once_roundtrip_and_recalculate(self):
        source=catalog.parse_catalog(feed(offer(1,'Геотекстиль под пленку бассейна, рулон 50 м2','10',description='')))
        initial=generate({**INPUTS,'finish':'film'})
        result,_=catalog.auto_select(initial,source)
        for tier in result['variants']:
            row=geo(result,tier)
            self.assertEqual(row['quantity'],geo(initial,tier)['quantity'])
            self.assertEqual(Decimal(row['amount']),Decimal(row['quantity'])*10)
            self.assertIn('без деления на рулон',row['price_conversion_note'])
        again,_=catalog.auto_select(recalculate(evaluate(result),result['inputs']),source)
        self.assertEqual(geo(again)['amount'],geo(result)['amount'])
        self.assertEqual(geo(again)['quantity'],geo(result)['quantity'])

    def test_unknown_purpose_warns_known_wrong_or_price_unit_blocks(self):
        bad=offer(1,'Геотекстиль дорожный','1',description='')
        unit=offer(2,'Геотекстиль для бассейна','2',description='').replace('</offer>','<param name="Единица цены">рулон</param></offer>')
        source=catalog.parse_catalog(feed(bad,unit,offer(3,'Геотекстиль Synthetic','5',description='')))
        result,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),source)
        self.assertEqual(geo(result)['catalog']['id'],'3')
        self.assertIn('Проверить',geo(result)['selection_note'])
        self.assertIn('не подтверждены',geo(result)['selection_note'])

    def test_manual_and_legacy_values_not_repriced(self):
        source=catalog.parse_catalog(feed(offer(1,'Геотекстиль для бассейна','10',description='')))
        initial=generate({**INPUTS,'finish':'film'})
        row=geo(initial);row.update(price='123.45',quantity='2',auto_quantity=False,unit_price_manual=True)
        result,_=catalog.auto_select(initial,source)
        self.assertEqual(geo(result)['amount'],'246.90')
        old=deepcopy(initial);old.pop('auxiliary_version')
        for v in old['variants'].values():
            for b in v['blocks']:
                for r in b['rows']:
                    if r['id']=='finish_geotextile':r['kind']='other'
        result,_=catalog.auto_select(old,source)
        self.assertIsNone(geo(result,'standard')['catalog'])

    def test_explicit_description_purpose_and_local_negation(self):
        for description, purpose in [
            ('Не подходит для бассейна.', 'denied'),
            ('Не для бассейнов.', 'denied'),
            ('Не под плёнку.', 'denied'),
            ('Для бассейна не подходит.', 'denied'),
            ('Для бассейна, не подходит для дорожного строительства.', 'pool'),
            ('Не предназначен под плёнку.', 'denied'),
            ('Под лайнер не подходит.', 'denied'),
            ('Предназначен только для дорожного строительства.', 'other'),
            ('Для бассейна; для дорог не подходит.', 'pool'),
            ('Для бассейна. Не предназначен для дорожного строительства.', 'pool'),
        ]:
            with self.subTest(description=description):
                source=catalog.parse_catalog(feed(offer(1,'Геотекстиль Synthetic','10',description=description)))
                self.assertEqual(source['products']['1']['geotextile']['purpose'],purpose)
                result,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),source)
                for tier in result['variants']:
                    self.assertEqual(geo(result,tier)['catalog'] is not None,purpose=='pool')

    def test_changed_purpose_withdraws_auto_but_warns_protected_manual(self):
        source=catalog.parse_catalog(feed(offer(1,'Геотекстиль для бассейна','10',description='')))
        initial,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),source)
        changed=catalog.parse_catalog(feed(offer(1,'Геотекстиль для бассейна','10',description='Не подходит для бассейна.')))
        withdrawn,_=catalog.auto_select(initial,changed)
        self.assertIsNone(geo(withdrawn)['catalog'])
        self.assertEqual(Decimal(initial['variants']['economy']['total'])-Decimal(withdrawn['variants']['economy']['total']),Decimal(geo(initial)['amount']))
        manual=deepcopy(initial)
        geo(manual).update(price='123.45',unit_price_manual=True,quantity='2',auto_quantity=False)
        origin=deepcopy(geo(manual)['catalog'])
        kept,_=catalog.auto_select(manual,changed)
        for result in [kept,evaluate(kept),recalculate(kept,kept['inputs'])]:
            self.assertEqual(geo(result)['catalog'],origin)
            self.assertEqual(geo(result)['amount'],'246.90')
            self.assertIn('не подходит',geo(result)['selection_note'])
            self.assertIn('Текущая выгрузка',geo(result)['selection_note'])

    def test_short_title_denials_do_not_become_pool_purpose(self):
        for suffix in ['не для бассейнов', 'не под плёнку', 'для бассейнов', 'под плёнку']:
            with self.subTest(suffix=suffix):
                source=catalog.parse_catalog(feed(offer(1,'Геотекстиль '+suffix,'10',description='')))
                expected='denied' if suffix.startswith('не ') else 'pool'
                self.assertEqual(source['products']['1']['geotextile']['purpose'],expected)
                selected,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),source)
                self.assertEqual(geo(selected)['catalog'] is not None,expected=='pool')
