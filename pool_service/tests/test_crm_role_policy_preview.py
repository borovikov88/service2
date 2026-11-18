"""Dependency-free tests: run with python -m unittest, no Django/DB settings."""
import ast
import dataclasses
import inspect
import itertools
import unittest

from pool_service.services import crm_role_policy_preview as policy


class RolePreviewTests(unittest.TestCase):
    def preview(self, **changes):
        args = dict(proposed_roles=('manager',), current_flags=dict.fromkeys(policy.CAPABILITIES, False),
            active=True, dismissed=False, member=True, crm_read=True, crm_write=True,
            account_kind='person', provenance='managed')
        args.update(changes)
        return policy.preview_role_change(**args)

    def test_new_manager_has_all_crm_defaults(self):
        result = self.preview()
        self.assertEqual(set(result.increases), set(policy.CAPABILITIES))
        self.assertFalse(result.authorizes_change)

    def test_role_matrix(self):
        for role in policy.ROLES:
            with self.subTest(role=role):
                self.assertEqual(bool(self.preview(proposed_roles=(role,)).increases),
                    role in ('owner', 'admin', 'manager'))

    def test_multiple_roles_order_independent(self):
        values = [self.preview(proposed_roles=roles) for roles in
            itertools.permutations(('manager', 'service', 'accountant'))]
        self.assertTrue(all(value == values[0] for value in values))

    def test_downgrade_does_not_reuse_legacy_allow(self):
        result = self.preview(proposed_roles=('service',), provenance='legacy_unverified',
            current_flags=dict.fromkeys(policy.CAPABILITIES, True))
        self.assertEqual(set(result.decreases), set(policy.CAPABILITIES))
        self.assertIn('legacy_grants_require_review_not_automatic_override', result.review_reasons)

    def test_explicit_deny_overrides_inherited_role(self):
        result = self.preview(overrides={'view_leads': 'deny'})
        row = next(row for row in result.rows if row.capability == 'view_leads')
        self.assertFalse(row.proposed)
        self.assertEqual(row.reason, 'explicit_deny')

    def test_explicit_allow_is_preview_not_authority(self):
        result = self.preview(proposed_roles=('service',), overrides={'view_leads': 'allow'})
        self.assertEqual(result.increases, ('view_leads',))
        self.assertFalse(result.authorizes_change)

    def test_hard_guards_override_explicit_allow(self):
        for change in ({'active': False}, {'dismissed': True}, {'member': False},
                       {'proposed_roles': ()}, {'crm_read': False}):
            with self.subTest(change=change):
                result = self.preview(overrides=dict.fromkeys(policy.CAPABILITIES, 'allow'), **change)
                self.assertFalse(result.increases)
                self.assertTrue(all(not row.proposed for row in result.rows))

    def test_write_disabled_keeps_only_read(self):
        result = self.preview(crm_write=False)
        self.assertEqual(set(result.increases), set(policy.READ_CAPABILITIES))

    def test_nonhuman_and_unknown_require_separate_scope(self):
        for kind in ('service', 'system', 'unknown'):
            with self.subTest(kind=kind):
                result = self.preview(account_kind=kind)
                self.assertEqual(result.rows, ())
                self.assertFalse(result.authorizes_change)
                self.assertIn('account_kind_requires_separate_scope', result.review_reasons)

    def test_missing_current_value_is_not_silently_false(self):
        with self.assertRaisesRegex(ValueError, 'incomplete_current_flags'):
            self.preview(current_flags={})

    def test_unknown_role_capability_or_invalid_values(self):
        bad = ({'proposed_roles': 'manager'}, {'proposed_roles': ('superuser',)},
            {'active': 1}, {'overrides': {'can_listen_calls': 'allow'}},
            {'overrides': {'view_leads': True}}, {'account_kind': 'robot'},
            {'provenance': 'assumed'}, {'current_flags': dict.fromkeys(policy.CAPABILITIES, 1)})
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.preview(**change)

    def test_inputs_not_mutated_and_result_immutable(self):
        flags = dict.fromkeys(policy.CAPABILITIES, False)
        overrides = {'view_leads': 'deny'}
        result = self.preview(current_flags=flags, overrides=overrides)
        self.assertEqual(flags, dict.fromkeys(policy.CAPABILITIES, False))
        self.assertEqual(overrides, {'view_leads': 'deny'})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.rows = ()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.rows[0].proposed = False

    def test_noop_still_not_authorized(self):
        result = self.preview(current_flags=dict.fromkeys(policy.CAPABILITIES, True))
        self.assertFalse(result.increases)
        self.assertFalse(result.decreases)
        self.assertFalse(result.authorizes_change)

    def test_module_has_no_database_network_or_runtime_imports(self):
        tree = ast.parse(inspect.getsource(policy))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertEqual(imports, ['dataclasses'])
        self.assertFalse(any(isinstance(node, ast.Import) for node in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main()
