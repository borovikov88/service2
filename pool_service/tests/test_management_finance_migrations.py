from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ManagementFinanceMigrationTests(TransactionTestCase):
    migrate_from = [("pool_service", "0093_management_finance_foundation")]
    migrate_to = [("pool_service", "0094_mysql_identity_unique_constraints")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        Organization = old_apps.get_model("pool_service", "Organization")
        Identity = old_apps.get_model("pool_service", "EmployeeOneCIdentity")
        organization = Organization.objects.create(name="Migration organization")
        for suffix in ("one", "two"):
            Identity.objects.create(
                organization=organization,
                raw_name=f"Identity {suffix}",
                normalized_name=f"identity {suffix}",
                source_identity_key="",
                onec_employee_id="  " if suffix == "one" else "",
                personnel_number="",
                status="not_found",
            )

    def tearDown(self):
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
        super().tearDown()

    def test_0093_to_0094_normalizes_nulls_and_creates_physical_unique_indexes(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        apps = self.executor.loader.project_state(self.migrate_to).apps
        Identity = apps.get_model("pool_service", "EmployeeOneCIdentity")

        self.assertEqual(Identity.objects.filter(onec_employee_id__isnull=True).count(), 2)
        self.assertEqual(Identity.objects.filter(personnel_number__isnull=True).count(), 2)
        self.assertEqual(Identity.objects.filter(source_identity_key__isnull=True).count(), 2)

        expected = {
            "unique_employee_user_per_org",
            "unique_onec_employee_id_per_org",
            "unique_personnel_number_per_org",
            "unique_source_employee_per_org",
            "unique_cashflow_article_per_org",
            "unique_cashflow_batch_row_month",
            "unique_payroll_batch_row_month",
        }
        found = set()
        with connection.cursor() as cursor:
            for table in (
                "pool_service_employee",
                "pool_service_employeeonecidentity",
                "pool_service_cashflowarticlemapping",
                "pool_service_cashflowrow",
                "pool_service_payrollrow",
            ):
                constraints = connection.introspection.get_constraints(cursor, table)
                found.update(
                    name for name, details in constraints.items()
                    if details.get("unique")
                )
        self.assertTrue(expected.issubset(found), expected - found)


@skipUnless(connection.vendor == "mysql", "MySQL-specific skipped-constraint regression")
class ManagementFinanceMySQLConflictMigrationTests(TransactionTestCase):
    migrate_from = [("pool_service", "0093_management_finance_foundation")]
    migrate_to = [("pool_service", "0094_mysql_identity_unique_constraints")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        Organization = old_apps.get_model("pool_service", "Organization")
        Identity = old_apps.get_model("pool_service", "EmployeeOneCIdentity")
        organization = Organization.objects.create(name="Conflict organization")
        for suffix in ("one", "two"):
            Identity.objects.create(
                organization=organization,
                raw_name=f"Conflict {suffix}",
                normalized_name=f"conflict {suffix}",
                onec_employee_id="duplicate-id",
                status="not_found",
            )

    def tearDown(self):
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        old_apps.get_model("pool_service", "EmployeeOneCIdentity").objects.all().delete()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_duplicate_non_null_identity_fails_closed(self):
        with self.assertRaisesRegex(
            RuntimeError, "unique_onec_employee_id_per_org"
        ):
            MigrationExecutor(connection).migrate(self.migrate_to)
