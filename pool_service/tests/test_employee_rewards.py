from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from pool_service.models import Employee, OneCImportBatch, OneCMonthlyProfit, OneCReportPeriodState, Organization
from pool_service.reward_models import RewardParticipation, RewardSchemeVersion
from pool_service.services.rewards import calculate_month, confirm_participation


class EmployeeRewardCalculationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test rewards org")
        self.user = User.objects.create_user("owner-test-rewards")
        self.e1 = Employee.objects.create(organization=self.org, display_name="Первый")
        self.e2 = Employee.objects.create(organization=self.org, display_name="Второй")
        self.month = date(2026, 9, 1)
        self.batch = OneCImportBatch.objects.create(
            organization=self.org,
            import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename="synthetic.json",
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.user,
            parser_version="test",
        )
        OneCReportPeriodState.objects.create(
            organization=self.org,
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            period_month=self.month,
            active_batch=self.batch,
            updated_by=self.user,
        )
        self.scheme = RewardSchemeVersion.objects.create(
            organization=self.org, effective_from=self.month, created_by=self.user,
        )

    def add_row(self, line, gp, *, revenue=None, cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL, doc="doc-a", kind="Услуга"):
        revenue = Decimal(revenue if revenue is not None else gp)
        return OneCMonthlyProfit.objects.create(
            import_batch=self.batch, organization=self.org, period_month=self.month,
            source_recorder="11111111-1111-4111-8111-111111111111",
            source_row_number=line, manager_name="", customer_name="Клиент",
            document_name="Документ", nomenclature=f"Строка {line}", nomenclature_type=kind,
            revenue=revenue, cost=revenue-Decimal(gp), gross_profit=Decimal(gp),
            analytical_gross_profit=Decimal(gp) if cost_source != OneCMonthlyProfit.COST_SOURCE_UNDEFINED else None,
            cost_source=cost_source,
            source_data={"source":"odata","recorder":"11111111-1111-4111-8111-111111111111","recorder_type":"Document_РасходнаяНакладная","document_group_key":doc},
        )

    def participation(self, employee, role, share, *, lines=None, scope="scope-a"):
        return RewardParticipation.objects.create(
            organization=self.org, employee=employee, role=role,
            status=RewardParticipation.STATUS_CONFIRMED, share=Decimal(share),
            period_month=self.month, scope_key=scope,
            source_document_key="odata-source:%s:Document_РасходнаяНакладная:11111111-1111-4111-8111-111111111111" % self.org.id,
            source_document_type="Document_РасходнаяНакладная",
            scope_line_identities=lines or [], customer_name="Клиент",
            assignment_source=RewardParticipation.SOURCE_MANUAL, created_by=self.user,
        )

    def test_two_workers_split_one_fund_without_increasing_it(self):
        row = self.add_row(1, "30000.00")
        self.participation(self.e1, RewardParticipation.ROLE_WORK, "0.600000", lines=[row.source_identity])
        self.participation(self.e2, RewardParticipation.ROLE_WORK, "0.400000", lines=[row.source_identity])
        data = calculate_month(self.org, self.month)
        amounts = {x["employee"]: Decimal(x["amount"]) for x in data["details"]}
        self.assertEqual(amounts, {"Первый": Decimal("7200.00"), "Второй": Decimal("4800.00")})
        self.assertEqual(sum(amounts.values()), Decimal("12000.00"))

    def test_loss_rows_reduce_percentage_base_before_flooring(self):
        r1 = self.add_row(1, "10000.00")
        r2 = self.add_row(2, "-4000.00")
        self.participation(self.e1, RewardParticipation.ROLE_SALE, "1.000000", lines=[r1.source_identity, r2.source_identity])
        detail = calculate_month(self.org, self.month)["details"][0]
        self.assertEqual(Decimal(detail["base"]), Decimal("6000.00"))
        self.assertEqual(Decimal(detail["amount"]), Decimal("600.00"))

    def test_negative_total_base_creates_zero_percentage_reward(self):
        r1 = self.add_row(1, "1000.00")
        r2 = self.add_row(2, "-2000.00")
        self.participation(self.e1, RewardParticipation.ROLE_SALE, "1.000000", lines=[r1.source_identity, r2.source_identity])
        detail = calculate_month(self.org, self.month)["details"][0]
        self.assertEqual(Decimal(detail["base"]), Decimal("-1000.00"))
        self.assertEqual(Decimal(detail["amount"]), Decimal("0.00"))

    def test_unknown_cost_is_visible_and_not_treated_as_zero(self):
        row = self.add_row(1, "1000.00", cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED)
        self.participation(self.e1, RewardParticipation.ROLE_WORK, "1.000000", lines=[row.source_identity])
        data = calculate_month(self.org, self.month)
        self.assertEqual(data["details"], [])
        self.assertTrue(any(x["kind"] == "missing_cost" for x in data["issues"]))

    def test_employee_can_have_multiple_roles(self):
        row = self.add_row(1, "10000.00")
        self.participation(self.e1, RewardParticipation.ROLE_SALE, "1.000000", lines=[row.source_identity], scope="sale")
        self.participation(self.e1, RewardParticipation.ROLE_PROJECT, "1.000000", lines=[row.source_identity], scope="project")
        data = calculate_month(self.org, self.month)
        totals = data["employees"][0]
        self.assertEqual(Decimal(totals["sale_reward"]), Decimal("1000.00"))
        self.assertEqual(Decimal(totals["project_reward"]), Decimal("500.00"))
        self.assertEqual(Decimal(totals["total"]), Decimal("1500.00"))

    def test_unallocated_share_is_not_given_to_remaining_worker(self):
        row = self.add_row(1, "30000.00")
        self.participation(self.e1, RewardParticipation.ROLE_WORK, "0.600000", lines=[row.source_identity])
        data = calculate_month(self.org, self.month)
        self.assertEqual(Decimal(data["details"][0]["amount"]), Decimal("7200.00"))
        self.assertTrue(any(x["kind"] == "unallocated" for x in data["issues"]))

    def test_confirm_participation_writes_history(self):
        row = self.add_row(1, "1000.00")
        item = RewardParticipation.objects.create(
            organization=self.org, employee=self.e1, role=RewardParticipation.ROLE_SALE,
            status=RewardParticipation.STATUS_PENDING, share=Decimal("1.000000"),
            period_month=self.month, scope_key="audit",
            source_document_key="odata-source:%s:Document_РасходнаяНакладная:11111111-1111-4111-8111-111111111111" % self.org.id,
            source_document_type="Document_РасходнаяНакладная",
            scope_line_identities=[row.source_identity], assignment_source=RewardParticipation.SOURCE_MANUAL,
            created_by=self.user,
        )
        confirm_participation(item, self.user)
        item.refresh_from_db()
        self.assertEqual(item.status, RewardParticipation.STATUS_CONFIRMED)
        change = item.changes.get()
        self.assertEqual(change.before["status"], RewardParticipation.STATUS_PENDING)
        self.assertEqual(change.after["status"], RewardParticipation.STATUS_CONFIRMED)
