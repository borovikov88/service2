"""Shard coverage and class fixture preservation without running a database."""
from collections import Counter
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_test_runner", ROOT / ".github/scripts/ci_test_runner.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


class ShardTests(unittest.TestCase):
    def cases(self, count=12):
        result = []
        for index in range(count):
            cls = type("Cases" + str(index), (unittest.TestCase,), {
                "__module__": __name__, "test_first": lambda self: None, "test_second": lambda self: None})
            result.extend(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
        return result

    def test_every_case_occurs_once_and_class_stays_together(self):
        cases = self.cases()
        for count in (1, 2, 4, 8):
            shards = R.partition(cases, count)
            self.assertEqual(Counter(t.id() for s in shards for t in s), Counter(t.id() for t in cases))
            self.assertTrue(all(shards))
            for cls in {type(t) for t in cases}:
                self.assertEqual(sum(any(type(t) is cls for t in s) for s in shards), 1)
            self.assertEqual([[t.id() for t in s] for s in shards],
                             [[t.id() for t in s] for s in R.partition(cases, count)])

    def test_class_fixtures_execute_once(self):
        events = []
        class Fixtures(unittest.TestCase):
            @classmethod
            def setUpClass(cls): events.append("setup")
            @classmethod
            def tearDownClass(cls): events.append("teardown")
            def test_first(self): events.append("first")
            def test_second(self): events.append("second")
        cases = list(unittest.defaultTestLoader.loadTestsFromTestCase(Fixtures)) + self.cases()
        for shard in R.partition(cases, 4):
            result = unittest.TestResult()
            unittest.TestSuite(shard).run(result)
            self.assertTrue(result.wasSuccessful())
        self.assertEqual(events, ["setup", "first", "second", "teardown"])

    def test_ui_keeps_security_integration_and_new_unknown_tests(self):
        modules = ("test_crm_leads", "test_owner_call_privacy", "test_task_read_privacy", "test_avito_crm_coverage",
                   "test_onec_crm_integration", "test_new_unknown", "test_finance_overview")
        cases = []
        for name in modules:
            cls = type("Cases", (unittest.TestCase,), {"__module__": "pool_service.tests." + name, "test_one": lambda self: None})
            cases.append(cls("test_one"))
        self.assertEqual(R.selected_tests(unittest.TestSuite(cases), "full"), cases)
        selected = R.selected_tests(unittest.TestSuite(cases), "ui")
        self.assertEqual(selected, cases[:-1])
        self.assertEqual(R.domain(cases[-2]), "core")

    def test_invalid_shard_count_is_an_error(self):
        for count in (0, -1, 9):
            with self.assertRaises(ValueError): R.partition(self.cases(), count)


if __name__ == "__main__":
    unittest.main()
