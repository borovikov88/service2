from copy import deepcopy
from decimal import Decimal as D, ROUND_HALF_UP
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate, EstimateInputError
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import offer, feed


def stock():
    def item(pid, sku, name, price):
        return offer(pid,name,price=price,description='',brand='Cefil' if 'Cefil' in name else 'Aquaviva').replace('</offer>',f'<vendorCode>{sku}</vendorCode></offer>')
    return catalog.parse_catalog(feed(item(1,'34500','Лайнер Aquaviva Azure Blue с акрилом','100001'),
        item(2,'24920','Лайнер Cefil France голубой','120001'),
        item(3,'30528','Лайнер Cefil Touch Onyx Hawai голубой сланец','190001'),
        item(4,'32505','Лайнер Cefil Touch Comfort Urdike голубой','1')))


def film(s,tier='economy'):
    return next(r for r in block(s,'finish',tier)['rows'] if r['id']=='finish_material')


class LinerTests(SimpleTestCase):
    def test_exact_series_and_roll_conversion_without_intermediate_rounding(self):
        s,count=catalog.auto_select(generate({**INPUTS,'finish':'film','bowl_height':'1.7'}),stock())
        self.assertEqual(count,3)
        self.assertEqual([film(s,t)['catalog']['sku'] for t in s['variants']],['34500','24920','30528'])
        for tier in s['variants']:
            r=film(s,tier); c=r['catalog']
            exact=(D(r['quantity'])*D(c['roll_price'])/D(c['roll_area'])).quantize(D('.01'),rounding=ROUND_HALF_UP)
            self.assertEqual(D(r['amount']),exact)
            self.assertNotEqual(D(r['amount']),D(r['quantity'])*D(r['price']))
            self.assertIn('Цена рулона',r['price_conversion_note'])
            self.assertEqual(r['unit'],'м²')
        self.assertEqual(evaluate(s)['variants'],s['variants'])
        self.assertEqual(recalculate(s,s['inputs'])['variants']['economy']['total'],s['variants']['economy']['total'])

    def test_refresh_manual_price_and_frozen_old_catalog(self):
        c=stock();s,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),c)
        original=deepcopy(s)
        c['products']['1']['price']='200001.00'
        refreshed,_,_=catalog.refresh_prices(s,c,'economy')
        self.assertEqual(film(refreshed)['catalog']['roll_price'],'200001.00')
        self.assertEqual(s,original)
        r=film(s);r.update(price='12.34',unit_price_manual=True)
        manual,_,_=catalog.refresh_prices(s,c,'economy')
        self.assertEqual(film(manual)['price'],'12.34')
        self.assertEqual(D(film(manual)['amount']),D(film(manual)['quantity'])*D('12.34'))
        old=deepcopy(original)
        film(old)['catalog'].pop('roll_area');film(old)['catalog'].pop('roll_price')
        before=evaluate(old)
        after,_=catalog.auto_select(before,c)
        self.assertEqual(film(after)['amount'],film(before)['amount'])
        self.assertNotIn('roll_area',film(after)['catalog'])

    def test_no_unapproved_substitution_unknown_units_or_forged_area(self):
        c=stock();del c['products']['3']
        s,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),c)
        self.assertIsNone(film(s,'premium')['price'])
        film(s)['catalog']['roll_area']='1'
        with self.assertRaises(EstimateInputError):evaluate(s)
        c=stock();c['products']['1']['name']='Другой лайнер'
        s,_=catalog.auto_select(generate({**INPUTS,'finish':'film'}),c)
        self.assertIsNone(film(s)['price'])

    def test_explicit_selection_converts_legacy_and_manual_quantity_bases(self):
        c=stock()
        for basis in ('','finish_net','finish_stock'):
            s=generate({**INPUTS,'finish':'film'})
            r=film(s);r.update(basis=basis,auto_quantity=False,quantity='79')
            catalog.apply_product(r,c['products']['1'],c)
            self.assertEqual(r['catalog']['roll_area'],'41.58')
            self.assertEqual(film(evaluate(s))['amount'],'189997.09')
        s=generate({**INPUTS,'finish':'film'});before=deepcopy(film(s))
        with self.assertRaises(EstimateInputError): catalog.apply_product(film(s),c['products']['4'],c)
        self.assertEqual(film(s),before)
