from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from pool_service.finance_imports.odata_profit import ProfitRow
from pool_service.finance_imports.odata_profit_drafts import _enrich_rows
from pool_service.models import (
    Employee,
    EmployeeOneCUserIdentity,
    EmployeeRewardAssignment,
    EmployeeRewardRule,
    EmployeeRewardScheme,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)
from pool_service.services.employee_rewards import (
    close_reward_month,
    create_or_update_assignment,
    reward_dashboard_data,
    seed_author_paperwork_proposals,
)


class EmployeeRewardTestCase(TestCase):
    period = date(2026, 9, 1)

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Reward Org")
        self.owner = User.objects.create_user(username="reward-owner", password="x")
        OrganizationAccess.objects.create(
            user=self.owner, organization=self.organization, role="owner"
        )
        self.employee_user = User.objects.create_user(username="reward-employee", password="x")
        OrganizationAccess.objects.create(
            user=self.employee_user, organization=self.organization, role="service"
        )
        self.employee1 = Employee.objects.create(
            organization=self.organization,
            user=self.employee_user,
            display_name="Employee One",
            first_name="One",
            last_name="Employee",
        )
        self.employee2 = Employee.objects.create(
            organization=self.organization,
            display_name="Employee Two",
            first_name="Two",
            last_name="Employee",
        )
        self.scheme = EmployeeRewardScheme.objects.create(
            organization=self.organization,
            name="Тестовая схема №1",
            version=1,
            effective_from=date(2026, 1, 1),
            created_by=self.owner,
        )
        self.rules = {}
        for role, unit, kind, fixed, rate in [
            (EmployeeRewardRule.ROLE_CLIENT_MANAGER, EmployeeRewardRule.UNIT_CLIENT_MANAGER, EmployeeRewardRule.KIND_INFORMATION, None, None),
            (EmployeeRewardRule.ROLE_PAPERWORK, EmployeeRewardRule.UNIT_PAPERWORK_RETAIL, EmployeeRewardRule.KIND_FIXED, Decimal("50"), None),
            (EmployeeRewardRule.ROLE_PAPERWORK, EmployeeRewardRule.UNIT_PAPERWORK_PACKAGE, EmployeeRewardRule.KIND_FIXED, Decimal("200"), None),
            (EmployeeRewardRule.ROLE_SALE, EmployeeRewardRule.UNIT_SALE, EmployeeRewardRule.KIND_PERCENT, None, Decimal("10")),
            (EmployeeRewardRule.ROLE_PROJECT, EmployeeRewardRule.UNIT_PROJECT, EmployeeRewardRule.KIND_PERCENT, None, Decimal("5")),
            (EmployeeRewardRule.ROLE_WORK, EmployeeRewardRule.UNIT_WORK, EmployeeRewardRule.KIND_PERCENT, None, Decimal("40")),
        ]:
            rule = EmployeeRewardRule.objects.create(
                scheme=self.scheme,
                role=role,
                unit_kind=unit,
                calculation_kind=kind,
                fixed_amount=fixed,
                rate_percent=rate,
            )
            self.rules[(role, unit)] = rule
        self.batch = self._new_batch("1" * 64)
        OneCReportPeriodState.objects.create(
            organization=self.organization,
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            period_month=self.period,
            active_batch=self.batch,
            updated_by=self.owner,
        )

    def _new_batch(self, digest):
        return OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename="test.json",
            stored_file="test/reward.json",
            file_sha256=digest,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.owner,
            parser_version="odata-2",
            period_first=self.period,
            period_last=self.period,
        )

    def _row(
        self,
        *,
        batch=None,
        recorder=None,
        line=1,
        document_type="Document_РасходнаяНакладная",
        document_guid=None,
        order_guid=None,
        nomenclature="Монтаж",
        nomenclature_type="Услуга",
        revenue=Decimal("50000"),
        cost=Decimal("20000"),
        cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
        author_guid=None,
        author_name=None,
    ):
        recorder = recorder or uuid4()
        document_guid = document_guid or recorder
        data = {
            "source": "odata",
            "recorder": str(recorder),
            "recorder_type": document_type,
            "line_number": line,
            "source_date": "2026-09-15",
            "document_guid": str(document_guid),
            "document_type": document_type,
            "reward_document_type": document_type,
            "reward_document_guid": str(document_guid),
            "reward_document_display": "Документ тест",
        }
        if order_guid:
            data["resolved_order_guid"] = str(order_guid)
        if author_guid:
            data["author_user_guid"] = str(author_guid)
            data["author_user_name"] = author_name or "1C User"
        gp = None if cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED else revenue - cost
        return OneCMonthlyProfit.objects.create(
            import_batch=batch or self.batch,
            organization=self.organization,
            period_month=self.period,
            source_recorder=recorder,
            source_identity="will-be-normalized",
            source_row_number=line,
            manager_name="Responsible Person",
            customer_name="Customer",
            document_name="Документ тест",
            nomenclature=nomenclature,
            nomenclature_type=nomenclature_type,
            quantity=Decimal("1"),
            revenue=revenue,
            cost=cost,
            gross_profit=gp,
            cost_source=cost_source,
            analytical_gross_profit=gp,
            source_data=data,
        )

    def _confirm_assignment(self, employee, role, row, share, lines=None):
        return create_or_update_assignment(
            organization=self.organization,
            period_month=self.period,
            document_type=row.source_data["reward_document_type"],
            document_guid=row.source_data["reward_document_guid"],
            role=role,
            employee=employee,
            share_percent=share,
            actor=self.owner,
            line_identities=lines,
            confirm=True,
            basis_note="test",
        )

    def test_work_fund_is_split_60_40_without_increasing_pool(self):
        row = self._row()
        self._confirm_assignment(self.employee1, EmployeeRewardRule.ROLE_WORK, row, 60)
        self._confirm_assignment(self.employee2, EmployeeRewardRule.ROLE_WORK, row, 40)

        data = reward_dashboard_data(self.organization, self.period)
        by_id = {item["employee_id"]: item for item in data["rows"]}

        self.assertEqual(by_id[self.employee1.id]["work_amount"], Decimal("7200.00"))
        self.assertEqual(by_id[self.employee2.id]["work_amount"], Decimal("4800.00"))
        self.assertEqual(
            by_id[self.employee1.id]["work_amount"] + by_id[self.employee2.id]["work_amount"],
            Decimal("12000.00"),
        )

    def test_unknown_cost_is_not_treated_as_zero(self):
        row = self._row(
            revenue=Decimal("10000"),
            cost=Decimal("0"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED,
        )
        self._confirm_assignment(self.employee1, EmployeeRewardRule.ROLE_SALE, row, 100)

        data = reward_dashboard_data(self.organization, self.period)
        employee = next(item for item in data["rows"] if item["employee_id"] == self.employee1.id)

        self.assertEqual(employee["sale_amount"], Decimal("0.00"))
        self.assertTrue(any(issue["kind"] == "missing_basis" for issue in data["issues"]))
        self.assertIsNone(employee["details"][0]["amount"])

    def test_one_employee_can_receive_multiple_confirmed_roles(self):
        row = self._row()
        self._confirm_assignment(self.employee1, EmployeeRewardRule.ROLE_SALE, row, 100)
        self._confirm_assignment(self.employee1, EmployeeRewardRule.ROLE_WORK, row, 100)

        data = reward_dashboard_data(self.organization, self.period)
        employee = next(item for item in data["rows"] if item["employee_id"] == self.employee1.id)

        self.assertEqual(employee["sale_amount"], Decimal("3000.00"))
        self.assertEqual(employee["work_amount"], Decimal("12000.00"))
        self.assertEqual(employee["total"], Decimal("15000.00"))

    def test_project_requires_explicit_positions(self):
        row = self._row()
        with self.assertRaisesMessage(Exception, "Проект / расчёт"):
            self._confirm_assignment(
                self.employee1,
                EmployeeRewardRule.ROLE_PROJECT,
                row,
                100,
                lines=[],
            )
        assignment = self._confirm_assignment(
            self.employee1,
            EmployeeRewardRule.ROLE_PROJECT,
            row,
            100,
            lines=[row.source_identity],
        )
        self.assertEqual(list(assignment.lines.values_list("source_identity", flat=True)), [row.source_identity])

    def test_payment_or_order_linked_check_does_not_seed_second_fixed_reward(self):
        author_guid = uuid4()
        EmployeeOneCUserIdentity.objects.create(
            organization=self.organization,
            onec_user_id=author_guid,
            display_name="Mapped 1C User",
            employee=self.employee1,
            status=EmployeeOneCUserIdentity.STATUS_CONFIRMED,
        )
        realization = self._row(
            line=1,
            document_type="Document_РасходнаяНакладная",
            author_guid=author_guid,
        )
        check = self._row(
            line=2,
            recorder=uuid4(),
            document_type="Document_ЧекККМ",
            order_guid=uuid4(),
            author_guid=author_guid,
            revenue=Decimal("1000"),
            cost=Decimal("500"),
        )
        created = seed_author_paperwork_proposals(
            self.organization, [realization, check], proposed_by=self.owner
        )

        self.assertEqual(created, 1)
        self.assertEqual(
            EmployeeRewardAssignment.objects.filter(
                organization=self.organization,
                role=EmployeeRewardRule.ROLE_PAPERWORK,
            ).count(),
            1,
        )
        self.assertEqual(
            EmployeeRewardAssignment.objects.get().source_document_type,
            "Document_РасходнаяНакладная",
        )

    def test_assignment_survives_active_import_replacement(self):
        document_guid = uuid4()
        row = self._row(document_guid=document_guid)
        assignment = self._confirm_assignment(
            self.employee1, EmployeeRewardRule.ROLE_SALE, row, 100
        )

        new_batch = self._new_batch("2" * 64)
        replacement = self._row(
            batch=new_batch,
            recorder=row.source_recorder,
            document_guid=document_guid,
            revenue=Decimal("60000"),
            cost=Decimal("20000"),
        )
        state = OneCReportPeriodState.objects.get(
            organization=self.organization,
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            period_month=self.period,
        )
        state.active_batch = new_batch
        state.save(update_fields=["active_batch"])

        data = reward_dashboard_data(self.organization, self.period)
        employee = next(item for item in data["rows"] if item["employee_id"] == self.employee1.id)
        self.assertEqual(assignment.source_document_guid, document_guid)
        self.assertEqual(replacement.source_identity, row.source_identity)
        self.assertEqual(employee["sale_amount"], Decimal("4000.00"))

    def test_closed_month_uses_snapshot_after_rule_change(self):
        row = self._row()
        self._confirm_assignment(self.employee1, EmployeeRewardRule.ROLE_SALE, row, 100)
        before = reward_dashboard_data(self.organization, self.period)
        before_total = next(item for item in before["rows"] if item["employee_id"] == self.employee1.id)["total"]
        close_reward_month(self.organization, self.period, self.owner)

        rule = self.rules[(EmployeeRewardRule.ROLE_SALE, EmployeeRewardRule.UNIT_SALE)]
        rule.rate_percent = Decimal("99")
        rule.save(update_fields=["rate_percent"])

        after = reward_dashboard_data(self.organization, self.period)
        after_total = next(item for item in after["rows"] if item["employee_id"] == self.employee1.id)["total"]
        self.assertTrue(after["is_closed"])
        self.assertEqual(before_total, after_total)
        self.assertEqual(after_total, Decimal("3000.00"))

    def test_employee_can_only_open_own_reward_detail_not_general_table(self):
        self.client.force_login(self.employee_user)
        general = self.client.get(reverse("finance_rewards"))
        own = self.client.get(
            reverse("finance_reward_employee_detail", args=[self.employee1.id]),
            {"month": "2026-09"},
        )
        other = self.client.get(
            reverse("finance_reward_employee_detail", args=[self.employee2.id]),
            {"month": "2026-09"},
        )
        self.assertEqual(general.status_code, 403)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(other.status_code, 403)


class OneCAuthorEnrichmentTests(TestCase):
    def test_author_is_taken_from_document_user_not_responsible_employee(self):
        organization_id = 7
        recorder = str(uuid4())
        author_guid = str(uuid4())
        responsible_guid = str(uuid4())
        nomenclature_guid = str(uuid4())
        customer_guid = str(uuid4())
        row = ProfitRow(
            recorder=recorder,
            recorder_type="Document_РасходнаяНакладная",
            line_number=1,
            period=datetime(2026, 9, 15, tzinfo=timezone.utc),
            source_period="2026-09-15T00:00:00Z",
            source_date=date(2026, 9, 15),
            organization_guid=str(uuid4()),
            nomenclature_guid=nomenclature_guid,
            customer_guid=customer_guid,
            responsible_guid=responsible_guid,
            document_guid=recorder,
            document_type="Document_РасходнаяНакладная",
            quantity=Decimal("1"),
            revenue=Decimal("1000"),
            vat=Decimal("0"),
            cost=Decimal("400"),
            order_guid=None,
        )
        references = {
            "nomenclature": {
                nomenclature_guid: {
                    "description": "Test item",
                    "article": "",
                    "nomenclature_type": "Товар",
                }
            },
            "customer": {customer_guid: {"description": "Test customer"}},
            "responsible": {responsible_guid: {"description": "Different Responsible"}},
            "author_user": {author_guid: {"description": "Actual Document Author"}},
        }
        documents = {
            ("Document_РасходнаяНакладная", recorder): {
                "number": "T-1",
                "date": date(2026, 9, 15),
                "author_user_guid": author_guid,
            }
        }

        normalized = _enrich_rows(
            [row],
            references,
            documents,
            organization_id,
            {row.organization_guid},
        )
        source = normalized[0]["source_data"]

        self.assertEqual(normalized[0]["manager_name"], "Different Responsible")
        self.assertEqual(source["author_user_guid"], author_guid)
        self.assertEqual(source["author_user_name"], "Actual Document Author")
        self.assertNotEqual(source["author_user_guid"], responsible_guid)
