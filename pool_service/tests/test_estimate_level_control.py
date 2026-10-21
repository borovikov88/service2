from copy import deepcopy
from django.test import SimpleTestCase
from pool_service.services.pool_estimates import generate as generate_current, evaluate, recalculate, EstimateInputError
from pool_service.services import estimate_catalog as catalog
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import offer, feed


def generate(inputs):
    """Saved estimates retain their separate level-fitting configuration."""
    snapshot = generate_current(inputs)
    snapshot.pop('level_shared_inlet')
    for variant in snapshot['variants'].values():
        for block_ in variant['blocks']:
            for row in block_['rows']:
                row.pop('filtration_quantity', None)
                row.pop('level_quantity', None)
    return recalculate(snapshot, snapshot['inputs'])

class LevelControlTests(SimpleTestCase):
    def pair(self, s):
        return [r for r in block(s,'embedded')['rows'] if r['kind'] in ('level_regulator','level_inlet')]

    def test_one_pair_independent_of_skimmers_and_filtration_counts(self):
        s=generate(INPUTS)
        before=deepcopy(s)
        changed=recalculate(s,{**s['inputs'],'length':'16'})
        self.assertEqual(len(self.pair(changed)),2)
        self.assertEqual([r['quantity'] for r in self.pair(changed)],['1.000','1.000'])
        self.assertEqual(s,before)
        self.assertEqual(changed['variants']['economy']['level_control_status'],'unknown')
        self.assertEqual(len(self.pair(evaluate(changed))),2)

    def test_verified_configuration_excludes_only_automatic_pair_and_restores(self):
        s=generate(INPUTS)
        skimmer=next(r for r in block(s,'embedded')['rows'] if r['kind']=='skimmer')
        for r in self.pair(s): r['price']='100'
        priced=evaluate(s)
        skimmer.update(level_status='included',level_evidence='Паспорт модели, стр. 7')
        known=evaluate(s)
        self.assertTrue(all(r['excluded'] for r in self.pair(known)))
        self.assertEqual(float(priced['variants']['economy']['total'])-float(known['variants']['economy']['total']),200)
        skimmer=next(r for r in block(known,'embedded')['rows'] if r['kind']=='skimmer')
        skimmer.update(level_status='unknown',level_evidence='')
        self.assertTrue(all(not r['excluded'] for r in self.pair(evaluate(known))))
        self.pair(s)[0].update(excluded=True,reason='Исключено пользователем')
        self.pair(s)[1].update(existing=True,auto_quantity=False,quantity='2')
        known=evaluate(s)
        self.assertEqual(self.pair(known)[0]['reason'],'Исключено пользователем')
        self.assertFalse(self.pair(known)[1]['excluded'])
        self.assertEqual(self.pair(known)[1]['quantity'],'2.000')
        self.pair(s)[1].update(existing=False, selection_locked=True)
        manual=evaluate(s)
        self.assertFalse(self.pair(manual)[1]['excluded'])
        self.assertEqual(self.pair(manual)[1]['amount'],'200.00')

    def test_evidence_required_and_history_not_backfilled_by_evaluate(self):
        s=generate(INPUTS)
        skimmer=next(r for r in block(s,'embedded')['rows'] if r['kind']=='skimmer')
        skimmer['level_status']='included'
        with self.assertRaises(EstimateInputError): evaluate(s)
        skimmer['level_status']='unknown'
        s.pop('level_system')
        for v in s['variants'].values():
            for b in v['blocks']: b['rows']=[r for r in b['rows'] if r['kind'] not in ('level_regulator','level_inlet')]
        self.assertEqual(self.pair(evaluate(s)),[])
        self.assertEqual(len(self.pair(recalculate(s,s['inputs']))),2)

    def test_catalog_classifies_exact_complete_regulators_not_frames(self):
        def item(pid,name,sku,brand='Aquaviva'):
            return offer(pid,name,brand=brand).replace('</offer>',f'<vendorCode>{sku}</vendorCode></offer>')
        c=catalog.parse_catalog(feed(item(1,'Регулятор уровня Aquaviva RO-7','16791'),
            item(2,'Рамка регулятора уровня Aquaviva RO-7','21142'),
            item(3,'Датчик уровня Aquaviva AVO-01','32756'),
            item(4,'Регулятор уровня Kripsol RGP.C','528','Kripsol'),
            item(5,'Регулятор уровня Hayward 3150','32304','Hayward')))
        self.assertEqual([p['id'] for p in c['products'].values() if p['kind']=='level_regulator'],['1','4','5'])
        s=generate(INPUTS)
        results=catalog.search_catalog(c,'','level_regulator','',s['inputs'],1)
        self.assertEqual(results['total'],3)
        self.assertTrue(all(p['warning'] for p in results['products']))

    def test_retired_legacy_bundle_gets_pair_once_but_manual_exclusion_is_respected(self):
        from pool_service.services.pool_estimates import make_row
        for reason in ('Состав закладных детализирован','Исключено пользователем'):
            s=generate(INPUTS)
            for v in s['variants'].values():
                b=next(b for b in v['blocks'] if b['id']=='embedded')
                b['rows']=[r for r in b['rows'] if r['kind'] not in ('level_regulator','level_inlet')]
                old=make_row('embedded_supply','Прежний комплект')
                old.update(excluded=True,reason=reason);b['rows'].append(old)
            first=recalculate(s,s['inputs']);second=recalculate(first,first['inputs'])
            expected=2 if reason=='Состав закладных детализирован' else 0
            self.assertEqual(len(self.pair(second)),expected)
            self.assertEqual(len(self.pair(first)),expected)
            warning=' '.join(second['variants']['economy']['warnings'])
            self.assertIn('есть отдельный регулятор' if expected else 'не сформирована полностью',warning)

    def test_manual_catalog_pick_survives_builtin_and_reverse(self):
        raw=offer(1,'Регулятор уровня Aquaviva RO-7',price='100').replace('</offer>','<vendorCode>16791</vendorCode></offer>')
        c=catalog.parse_catalog(feed(raw))
        s=generate(INPUTS);reg=self.pair(s)[0]
        catalog.apply_product(reg,c['products']['1'],c)
        self.assertFalse(reg.get('selection_locked',False))
        skimmer=next(r for r in block(s,'embedded')['rows'] if r['kind']=='skimmer')
        skimmer.update(level_status='included',level_evidence='Паспорт exact model')
        checked=evaluate(s)
        self.assertFalse(self.pair(checked)[0]['excluded'])
        self.assertEqual(self.pair(checked)[0]['amount'],'100.00')
        self.assertTrue(any('Конфликт комплектации' in w for w in checked['variants']['economy']['warnings']))
        next(r for r in block(checked,'embedded')['rows'] if r['kind']=='skimmer').update(level_status='absent')
        restored=evaluate(checked)
        self.assertFalse(self.pair(restored)[0]['excluded'])
        self.assertFalse(self.pair(restored)[1]['excluded'])
        self.assertEqual(self.pair(restored)[0]['amount'],'100.00')
