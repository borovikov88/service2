from copy import deepcopy
from django.test import SimpleTestCase
from pool_service.services.pool_estimates import generate, evaluate, recalculate, EstimateInputError
from pool_service.services import estimate_catalog as catalog
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import offer, feed

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
