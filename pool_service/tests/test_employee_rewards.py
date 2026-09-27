from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from pool_service.models import Employee, OneCImportBatch, OneCMonthlyProfit, OneCReportPeriodState, Organization
from pool_service.reward_models import OneCAuthorIdentity, RewardParticipation, RewardSchemeVersion
from pool_service.services.rewards import (
    add_documentation_participant,
    calculate_month,
    confirm_participation,
    create_manual_participation,
    create_scheme_version,
    reward_document_options,
    sync_author_proposals,
    update_participation_share,
)


class EmployeeRewardCalculationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test rewards org")
        self.user = User.objects.create_superuser("owner-test-rewards", "owner@example.test", "test")
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

    def test_manual_work_scopes_can_use_different_lines(self):
        first = self.add_row(1, "10000.00", kind="Работа")
        second = self.add_row(2, "20000.00", kind="Работа")
        document = reward_document_options(self.org, self.month)[0]
        a = create_manual_participation(
            self.org, self.user, self.month, document_key=document["scope_key"],
            employee=self.e1, role=RewardParticipation.ROLE_WORK, share=Decimal("1"),
            line_identities=[first.source_identity],
        )
        b = create_manual_participation(
            self.org, self.user, self.month, document_key=document["scope_key"],
            employee=self.e2, role=RewardParticipation.ROLE_WORK, share=Decimal("1"),
            line_identities=[second.source_identity],
        )
        confirm_participation(a, self.user)
        confirm_participation(b, self.user)
        data = calculate_month(self.org, self.month)
        amounts = {item["employee"]: Decimal(item["amount"]) for item in data["details"]}
        self.assertEqual(amounts["Первый"], Decimal("4000.00"))
        self.assertEqual(amounts["Второй"], Decimal("8000.00"))

    def test_project_or_work_requires_selected_lines(self):
        self.add_row(1, "1000.00", kind="Работа")
        document = reward_document_options(self.org, self.month)[0]
        with self.assertRaisesMessage(Exception, "нужно выбрать конкретные"):
            create_manual_participation(
                self.org, self.user, self.month, document_key=document["scope_key"],
                employee=self.e1, role=RewardParticipation.ROLE_WORK, share=Decimal("1"),
                line_identities=[],
            )

    def test_confirm_rejects_share_over_100_percent(self):
        row = self.add_row(1, "10000.00", kind="Работа")
        a = self.participation(self.e1, RewardParticipation.ROLE_WORK, "0.700000", lines=[row.source_identity], scope="same")
        pending = RewardParticipation.objects.create(
            organization=self.org, employee=self.e2, role=RewardParticipation.ROLE_WORK,
            status=RewardParticipation.STATUS_PENDING, share=Decimal("0.400000"),
            period_month=self.month, scope_key="same",
            source_document_key=a.source_document_key,
            source_document_type=a.source_document_type,
            scope_line_identities=[row.source_identity], assignment_source=RewardParticipation.SOURCE_MANUAL,
            created_by=self.user,
        )
        with self.assertRaisesMessage(Exception, "превышают 100%"):
            confirm_participation(pending, self.user)

    def test_documentation_fixed_does_not_require_known_cost(self):
        row = self.add_row(1, "1000.00", cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED)
        self.participation(self.e1, RewardParticipation.ROLE_DOCUMENTATION, "1.000000", lines=[row.source_identity], scope="doc-fixed")
        detail = calculate_month(self.org, self.month)["details"][0]
        self.assertEqual(Decimal(detail["amount"]), Decimal("200.00"))

    def test_work_role_rejects_goods_line(self):
        goods = self.add_row(1, "1000.00", kind="Товар")
        document = reward_document_options(self.org, self.month)[0]
        with self.assertRaisesMessage(Exception, "только работы и услуги"):
            create_manual_participation(
                self.org, self.user, self.month, document_key=document["scope_key"],
                employee=self.e1, role=RewardParticipation.ROLE_WORK, share=Decimal("1"),
                line_identities=[goods.source_identity],
            )

    def test_calculated_cost_is_labelled_in_detail(self):
        row = self.add_row(1, "1000.00", cost_source=OneCMonthlyProfit.COST_SOURCE_CALCULATED)
        self.participation(self.e1, RewardParticipation.ROLE_SALE, "1.000000", lines=[row.source_identity])
        detail = calculate_month(self.org, self.month)["details"][0]
        self.assertEqual(detail["base_quality"], "Расчётная ВП")

    def test_missing_roles_are_visible_before_assignments(self):
        self.add_row(1, "1000.00", kind="Работа")
        data = calculate_month(self.org, self.month)
        kinds = {item["kind"] for item in data["issues"]}
        self.assertIn("missing_sale_role", kinds)
        self.assertIn("missing_documentation_role", kinds)
        self.assertIn("missing_work_role", kinds)

    def test_month_unknown_cost_is_visible_even_without_participant(self):
        self.add_row(1, "1000.00", cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED)
        data = calculate_month(self.org, self.month)
        self.assertTrue(any(item["kind"] == "month_missing_cost" for item in data["issues"]))

    def test_confirmed_share_can_be_reduced_with_audit(self):
        row = self.add_row(1, "30000.00", kind="Работа")
        item = self.participation(self.e1, RewardParticipation.ROLE_WORK, "1.000000", lines=[row.source_identity], scope="shared-doc")
        update_participation_share(item, self.user, Decimal("0.600000"))
        item.refresh_from_db()
        self.assertEqual(item.share, Decimal("0.600000"))
        self.assertTrue(item.changes.filter(reason="Изменение доли участия").exists())


    def test_scheme_rejects_negative_or_over_100_percent_values(self):
        next_month = date(2026, 10, 1)
        with self.assertRaisesMessage(Exception, "Фиксированная сумма"):
            create_scheme_version(
                self.org, self.user, effective_from=next_month,
                values={"documentation_retail_fixed": "-1"},
            )
        with self.assertRaisesMessage(Exception, "Процентная ставка"):
            create_scheme_version(
                self.org, self.user, effective_from=next_month,
                values={"sale_rate": "1.01"},
            )

    def test_missing_selected_line_after_reimport_is_blocking_issue(self):
        row = self.add_row(1, "1000.00", kind="Работа")
        missing_identity = row.source_identity.replace(":1", ":999999")
        self.participation(
            self.e1,
            RewardParticipation.ROLE_WORK,
            "1.000000",
            lines=[row.source_identity, missing_identity],
            scope="missing-line",
        )
        data = calculate_month(self.org, self.month)
        self.assertTrue(any(item["kind"] == "missing_selected_lines" for item in data["issues"]))
        self.assertFalse(any(item["scope_key"] == "missing-line" for item in data["details"]))

    def test_closed_month_rejects_co_documenter(self):
        from pool_service.reward_models import RewardMonthClose
        item = RewardParticipation.objects.create(
            organization=self.org, employee=self.e1, role=RewardParticipation.ROLE_DOCUMENTATION,
            status=RewardParticipation.STATUS_CONFIRMED, share=Decimal("0.500000"),
            period_month=self.month, scope_key="closed-doc", source_document_key="closed-doc",
            source_document_type="Document_РасходнаяНакладная",
            assignment_source=RewardParticipation.SOURCE_MANUAL, created_by=self.user,
        )
        RewardMonthClose.objects.create(
            organization=self.org, period_month=self.month, scheme_version=self.scheme,
            snapshot={}, source_hash="0" * 64, closed_by=self.user,
        )
        with self.assertRaisesMessage(Exception, "после закрытия месяца"):
            add_documentation_participant(item, self.e2, Decimal("0.500000"), self.user)


    def test_invalid_scheme_does_not_mutate_previous_version(self):
        with self.assertRaisesMessage(Exception, "Процентная ставка"):
            create_scheme_version(
                self.org,
                self.user,
                effective_from=date(2026, 10, 1),
                values={"sale_rate": "1.01"},
            )
        self.scheme.refresh_from_db()
        self.assertIsNone(self.scheme.effective_to)


    def test_resync_with_author_clears_missing_author_placeholder(self):
        row = self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        placeholder = RewardParticipation.objects.get(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )
        self.assertEqual(placeholder.status, RewardParticipation.STATUS_REQUIRED)

        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Исторический пользователь",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)

        placeholder.refresh_from_db()
        self.assertEqual(placeholder.status, RewardParticipation.STATUS_NOT_APPLICABLE)
        self.assertTrue(
            RewardParticipation.objects.filter(
                organization=self.org,
                period_month=self.month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
                author_identity__onec_user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            ).exists()
        )
