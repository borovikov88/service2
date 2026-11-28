"""Run standard Django discovery in disjoint, class-preserving CI shards."""
import argparse
from collections import Counter, defaultdict
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]

BROWSERS = {
    "shared": (
        "test_avito_owner_dashboard_browser", "test_sqlite_live_server_safety",
        "test_crm_column_browser", "test_crm_mail_rows_browser", "test_contacts_browser", "test_owner_call_privacy_browser", "test_crm_role_browser"),
    "commercial": (
        "test_crm_commercial_card_browser", "test_crm_failure_browser", "test_crm_object_need_browser",
        "test_crm_lead_kanban_browser", "test_crm_service_binding_browser", "test_crm_quote_browser",
        "test_crm_handoff_browser", "test_crm_construction_browser", "test_crm_recurring_browser"),
    "release": (
        "test_contact_creation_browser", "test_crm_lead_customer_completion_browser",
        "test_crm_onec_finance_browser", "test_crm_assignment_browser"),
    "material": ("test_crm_material_browser",),
}
BROWSER_FLAGS = (
    "AVITO_OWNER_BROWSER_TEST", "CONTACTS_BROWSER_TEST", "CRM_CARD_BROWSER_TEST",
    "CRM_COLUMN_BROWSER_TEST", "CRM_NEEDS_BROWSER_TEST", "CRM_LEADS_BROWSER_TEST",
    "CRM_MATERIAL_BROWSER_TEST", "CRM_ROLE_BROWSER_TEST",
)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def domain(test):
    name = type(test).__module__.rsplit(".", 1)[-1]
    if name.startswith("test_avito"):
        return "avito"
    if name == "test_onec_crm_integration" or name == "tests_communications" or name.startswith((
            "test_crm", "test_contact", "test_client", "test_conversation", "test_call",
            "test_owner_call", "test_staff", "test_organization", "test_task_read")):
        return "crm"
    if name.startswith(("test_finance", "test_onec", "test_odata", "test_management_finance", "test_payroll")):
        return "finance"
    # New/unclassified tests and the legacy mixed modules are always included.
    return "core"


def class_weight(cls):
    module = cls.__module__.rsplit(".", 1)[-1]
    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):
        source = ""
    if "migration" in module or "MigrationExecutor" in source:
        return 40
    django_bases = {base.__name__ for base in cls.__mro__ if base.__module__ == "django.test.testcases"}
    return 4 if "TransactionTestCase" in django_bases and "TestCase" not in django_bases else 1


def partition(tests, count):
    if not 1 <= count <= 8:
        raise ValueError("Invalid shard count")
    classes = defaultdict(list)
    for test in tests:
        classes[type(test)].append(test)
    loads, allocation = [0] * count, {}
    # Largest groups first. Keep setUpClass/tearDownClass and migration fixtures
    # on one worker; each worker has an independent disposable database.
    weighted = [(len(cases) * class_weight(cls), cls) for cls, cases in classes.items()]
    for weight, cls in sorted(weighted, key=lambda pair: (-pair[0], pair[1].__module__, pair[1].__qualname__)):
        target = min(range(count), key=lambda index: (loads[index], index))
        allocation[cls] = target
        loads[target] += weight
    shards = [[test for test in tests if allocation[type(test)] == index] for index in range(count)]
    if Counter(test.id() for shard in shards for test in shard) != Counter(test.id() for test in tests):
        raise ValueError("CI partition lost or duplicated tests")
    return shards


def selected_tests(suite, mode):
    tests = list(flatten(suite))
    if mode == "full":
        return tests
    if mode == "ui":
        return [test for test in tests if domain(test) in ("core", "crm", "avito")]
    raise ValueError("Unexpected server test mode")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("server", "mysql", "browser"))
    parser.add_argument("--settings", required=True)
    parser.add_argument("--mode", choices=("full", "ui"), default="full")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--group", choices=tuple(BROWSERS))
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards or not 1 <= args.shards <= 8:
        parser.error("Invalid shard")
    if args.kind == "mysql":
        if (args.settings != "service_site.settings" or os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("DB_NAME") != "service2_test" or os.environ.get("DB_HOST") != "127.0.0.1"
                or os.environ.get("DB_ENGINE") != "django.db.backends.mysql"):
            parser.error("MySQL CI requires the explicitly configured disposable service")
    elif args.settings != "service_site.test_settings":
        parser.error("SQLite CI requires isolated test settings")
    if args.kind == "browser":
        if not args.group:
            parser.error("Browser group is required")
        for flag in BROWSER_FLAGS:
            os.environ[flag] = "1"
    else:
        # Server shards preserve the normal suite's browser opt-in behaviour.
        for flag in BROWSER_FLAGS:
            os.environ.pop(flag, None)
    os.environ["DJANGO_SETTINGS_MODULE"] = args.settings
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import django
    django.setup()
    from django.test.runner import DiscoverRunner

    class ShardedRunner(DiscoverRunner):
        def build_suite(self, *build_args, **kwargs):
            suite = super().build_suite(*build_args, **kwargs)
            if args.kind == "browser":
                return suite
            tests = selected_tests(suite, args.mode)
            shards = partition(tests, args.shards)
            summary = {
                "kind": args.kind, "mode": args.mode, "total": len(tests),
                "shard_counts": [len(shard) for shard in shards],
                "inventory_sha256": hashlib.sha256("\n".join(sorted(t.id() for t in tests)).encode()).hexdigest(),
            }
            print("CI_TEST_INVENTORY " + json.dumps(summary, sort_keys=True))
            if not tests or any(not shard for shard in shards):
                raise ValueError("Unexpected empty CI test selection")
            return self.test_suite(shards[args.shard])

    if args.kind == "mysql":
        labels = json.loads((ROOT / ".github/mysql_test_labels.json").read_text(encoding="utf-8"))
    elif args.kind == "browser":
        labels = ["pool_service.tests." + name for name in BROWSERS[args.group]]
    else:
        labels = []  # Standard full Django discovery, including new test files.
    runner = ShardedRunner(verbosity=1, interactive=False)
    if args.inventory_only:
        runner.build_suite(labels)
    else:
        sys.exit(bool(runner.run_tests(labels)))


if __name__ == "__main__":
    main()
