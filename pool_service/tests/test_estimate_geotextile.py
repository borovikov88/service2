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
        self.assertEqual(geo(result)['amount'],'246.00')
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
            self.assertEqual(geo(result)['amount'],'246.00')
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


class GeotextileRollTests(SimpleTestCase):
    def source(self, price='16606', extra=''):
        from pool_service.services.estimate_geotextile import PREFERRED_URL
        return catalog.parse_catalog(feed(offer(26930,'Геотекстиль Aquaviva 300 г/м2 (50 м)',price,
            description='Для защиты лайнера бассейна.',url=PREFERRED_URL).replace('</offer>',extra+'</offer>')))

    def selected(self):
        return catalog.auto_select(generate({**INPUTS,'finish':'film'}),self.source())[0]

    def test_roll_conversion_all_quotes_exact_totals_and_roundtrip(self):
        initial=generate({**INPUTS,'finish':'film'});selected=self.selected()
        for tier in selected['variants']:
            row=geo(selected,tier)
            self.assertEqual(row['price'],'166.06')
            self.assertEqual(row['catalog']['roll_price'],'16606.00')
            self.assertEqual(row['catalog']['roll_area'],'100')
            self.assertEqual(row['quantity'],geo(initial,tier)['quantity'])
            self.assertEqual(Decimal(row['amount']),Decimal(row['quantity'])*Decimal('166'))
            self.assertEqual(Decimal(selected['variants'][tier]['total'])-Decimal(initial['variants'][tier]['total']),Decimal(row['amount']))
            self.assertIn('100 м²',row['price_conversion_note']);self.assertIn('15%',row['price_conversion_note'])
        for result in (evaluate(selected),recalculate(selected,selected['inputs'])):
            for tier in selected['variants']:
                self.assertEqual(geo(result,tier)['amount'],geo(selected,tier)['amount'])

    def test_old_read_unchanged_explicit_recalculation_corrects_auto_only(self):
        old=self.selected()
        for tier in old['variants']:
            r=geo(old,tier);r['catalog'].pop('roll_area');r['catalog'].pop('roll_price')
            r['catalog']['geotextile']={'purpose':'pool','price_unit':'m2'}
            r['catalog']['price']=r['price']='16606.00'
        self.assertEqual(geo(evaluate(old))['price'],'16606.00')
        changed=self.source('20000')
        updated,_=catalog.auto_select(recalculate(old,old['inputs']),changed)
        self.assertEqual(geo(updated)['price'],'166.06') # frozen source, not current 20000
        for price in ('0',None,'333'):
            manual=deepcopy(old);geo(manual).update(price=price,unit_price_manual=True)
            kept,_=catalog.auto_select(recalculate(manual,manual['inputs']),changed)
            self.assertEqual(geo(kept)['price'],None if price is None else price+'.00')
        manual=deepcopy(old);geo(manual)['catalog']['selection']='manual'
        kept,_=catalog.auto_select(manual,changed);self.assertEqual(geo(kept)['price'],'16606.00')
        refreshed,n,_=catalog.refresh_prices(kept,changed,'economy')
        self.assertEqual(n,1);self.assertEqual(geo(refreshed)['price'],'200.00')

    def test_frozen_source_repeated_selection_and_explicit_refresh_manual_protection(self):
        s=self.selected();amount=geo(s)['amount'];changed=self.source('20000')
        for _ in range(2):s=catalog.auto_select(recalculate(evaluate(s),s['inputs']),changed)[0]
        self.assertEqual(geo(s)['amount'],amount)
        fresh,n,_=catalog.refresh_prices(s,changed,'economy')
        self.assertEqual(n,1);self.assertEqual(geo(fresh)['price'],'200.00')
        fresh,n,_=catalog.refresh_prices(fresh,self.source('25000'),'economy')
        self.assertEqual(n,1);self.assertEqual(geo(fresh)['price'],'250.00')
        for price in ('0',None,'123.45'):
            manual=deepcopy(s);geo(manual).update(price=price,unit_price_manual=True)
            kept,n,skipped=catalog.refresh_prices(manual,changed,'economy')
            self.assertEqual(n,0);self.assertEqual(skipped,1)
            self.assertEqual(geo(kept)['price'],None if price is None else str(Decimal(price).quantize(Decimal('.01'))))
        hidden=deepcopy(s);geo(hidden)['excluded']=True
        kept,n,_=catalog.refresh_prices(hidden,changed,'economy');self.assertEqual(n,0);self.assertEqual(geo(kept)['price'],'166.06')

    def test_conflicting_units_or_dimensions_not_auto_selected(self):
        for param in ('<param name="Единица цены">м²</param>', '<param name="Ширина рулона, м">3</param>', '<param name="Длина рулона, м">40</param>'):
            with self.subTest(param=param):
                source=self.source(extra=param)
                s,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),source)
                self.assertIsNone(geo(s)['catalog'])
                original=self.selected();fresh,n,skipped=catalog.refresh_prices(original,source,'economy')
                self.assertEqual(n,0);self.assertEqual(skipped,1);self.assertEqual(geo(fresh)['price'],'166.06')

    def test_explicit_title_description_dimensions_override_known_identity(self):
        from pool_service.services.estimate_geotextile import PREFERRED_URL
        for title, description, conflict in [
            ('Геотекстиль Aquaviva 300 г/м2 рулон 3×50 м','Для бассейна.',True),
            ('Геотекстиль Aquaviva 300 г/м2 (50 м)','Ширина рулона: 3 м. Длина рулона: 50 м.',True),
            ('Геотекстиль Aquaviva 300 г/м2 (50 м)','Размер рулона: 3х50 м.',True),
            ('Геотекстиль Aquaviva 300 г/м2 (50 м)','В рулоне 150 м2.',True),
            ('Геотекстиль Aquaviva 300 г/м2 (50 м)','Площадь рулона: 150 м².',True),
            ('Геотекстиль Aquaviva 300 г/м2 2×50 м','Ширина рулона: 2 м. Длина рулона: 50 м. В рулоне 100 м2.',False),
            ('Геотекстиль Aquaviva 300 г/м2 (50 м)','Рекомендуется для бассейна размером 3×50 м.',False),
        ]:
            with self.subTest(title=title,description=description):
                source=catalog.parse_catalog(feed(offer(26930,title,'16606',description=description+' Для бассейна.',url=PREFERRED_URL)))
                self.assertEqual(source['products']['26930']['geotextile']['price_unit']=='conflict',conflict)
                s,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),source)
                self.assertEqual(geo(s)['catalog'] is None,conflict)
                fresh,n,_=catalog.refresh_prices(self.selected(),source,'economy')
                self.assertEqual(n,0 if conflict else 1);self.assertEqual(geo(fresh)['price'],'166.06')
