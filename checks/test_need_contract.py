"""Dependency-free checks of actual pure validators extracted by AST.

Not Django/ORM/security/race tests. This intentionally does not mock a database.
"""
import ast
from pathlib import Path
import re
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
class ValidationError(Exception):
    pass
namespace = {'re': re, 'ValidationError': ValidationError}
for file, names in (
    ('pool_service/services/crm_commercial_commands.py', {'_positive_id'}),
    ('pool_service/services/crm_need_deals.py', {'_key', '_selected', '_positions'}),
):
    tree = ast.parse((ROOT / file).read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, 'exec'), namespace)

class ContractTests(unittest.TestCase):
    def test_selected_bounds_and_exact_fields(self):
        select = namespace['_selected']
        for value in (None, {}, [], [dict(line_token=f'x{i}', line_id=i+1) for i in range(51)]):
            with self.assertRaises(ValidationError): select(value)
        self.assertEqual(len(select([dict(line_token=f'x{i}', line_id=i+1) for i in range(50)])), 50)
        for row in ({'line_token':'x','line_id':1,'kind':'existing'}, {'line_token':'x'},
                    {'line_token':'X','line_id':1}, {'line_token':'x','line_id':True},
                    {'line_token':'x','line_id':0}):
            with self.assertRaises(ValidationError): select([row])

    def test_no_duplicate_tokens_or_identities(self):
        for rows in ([dict(line_token='x',line_id=1)]*2,
                     [dict(line_token='x',line_id=1),dict(line_token='y',line_id=1)]):
            with self.assertRaises(ValidationError): namespace['_selected'](rows)

    def test_explicit_identity_and_canonical_order(self):
        select = namespace['_selected']
        rows = [dict(line_token='b',kind='new',line_id=None),dict(line_token='a',kind='existing',line_id=1)]
        self.assertEqual([x['line_token'] for x in select(rows,reconcile=True)], ['a','b'])
        for row in (dict(line_token='a',kind='new',line_id=1), dict(line_token='a',kind='',line_id=None)):
            with self.assertRaises(ValidationError): select([row],reconcile=True)

    def test_only_approval_required_positions(self):
        for mode in ('included','chemistry_monthly','warranty','error','cancelled','already_supplied'):
            review = types.SimpleNamespace(positions=[dict(line_token='x', mode=mode)])
            with self.assertRaises(ValidationError): namespace['_positions'](review,[dict(line_token='x')])
        review = types.SimpleNamespace(positions=[dict(line_token='x',mode='approval_required')])
        namespace['_positions'](review,[dict(line_token='x')])
        with self.assertRaises(ValidationError): namespace['_positions'](review,[dict(line_token='missing')])

    def test_operation_keys(self):
        for value in ('', 'A', 'x'*101, None, 1, 'has space'):
            with self.assertRaises(ValidationError): namespace['_key'](value)
        namespace['_key']('need_123-abc')

if __name__ == '__main__':
    unittest.main()
