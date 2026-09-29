from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from pool_service.models import Employee, OneCImportBatch, OneCMonthlyProfit, OneCReportPeriodState, Organization, OrganizationAccess
from pool_service.reward_models import OneCAuthorIdentity, RewardParticipation, RewardSchemeVersion
from pool_service.reward_views import _percent_value
from pool_service.services.rewards import (
    add_documentation_participant,
    calculate_month,
    cancel_pending_participation,
    confirm_participation,
    create_manual_participation,
    create_scheme_version,
    ensure_test_scheme,
    reward_document_options,
    reward_profit_rows,
    resolve_documentation_placeholder,
    scheme_for_month,
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

    def add_retail_row(
        self,
        *,
        recorder,
        recorder_type,
        line,
        source_date,
        nomenclature_guid,
        quantity,
        revenue,
        cost,
        document_number=None,
        document_guid=None,
        report_recorder=None,
    ):
        source_data = {
            "source": "odata",
            "recorder": recorder,
            "recorder_type": recorder_type,
            "organization_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "source_date": source_date.isoformat(),
            "nomenclature_guid": nomenclature_guid,
            "document_guid": document_guid,
            "document_type": (
                "Document_ЧекККМ" if document_guid else recorder_type
            ),
        }
        if document_number:
            source_data.update({
                "document_number": document_number,
                "document_date": source_date.isoformat(),
            })
        if report_recorder:
            source_data.update({
                "document_group_recorder": report_recorder,
                "document_group_recorder_type": "Document_ОтчетОРозничныхПродажах",
                "document_group_key": (
                    f"odata-document:{self.org.id}:"
                    f"Document_ОтчетОРозничныхПродажах:{report_recorder}"
                ),
                "document_display": "Отчёт о розничных продажах",
            })
        return OneCMonthlyProfit.objects.create(
            import_batch=self.batch,
            organization=self.org,
            period_month=self.month,
            source_recorder=recorder,
            source_row_number=line,
            manager_name="",
            customer_name="Без контрагента",
            document_name=(
                "Отчёт о розничных продажах"
                if recorder_type == "Document_ОтчетОРозничныхПродажах"
                else "Чек ККМ"
            ),
            nomenclature="Розничный товар",
            nomenclature_type="Товар",
            quantity=Decimal(str(quantity)),
            revenue=Decimal(str(revenue)),
            cost=Decimal(str(cost)),
            gross_profit=Decimal(str(revenue)) - Decimal(str(cost)),
            analytical_gross_profit=Decimal(str(revenue)) - Decimal(str(cost)),
            cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
            source_data=source_data,
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


    def test_rewards_page_get_tolerates_irregular_persisted_source_metadata(self):
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.org,
            role="owner",
        )
        row = self.add_row(1, "1000.00")
        row.source_data = ["legacy", "metadata"]
        row.save(update_fields=["source_data"])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("finance_employee_rewards"),
            {"month": "2026-09"},
        )

        self.assertEqual(response.status_code, 200)

    def test_rewards_page_get_normalizes_non_string_source_labels_and_author_id(self):
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.org,
            role="owner",
        )
        row = self.add_row(1, "1000.00")
        row.source_data = {
            "source": "odata",
            "recorder": "11111111-1111-4111-8111-111111111111",
            "recorder_type": "Document_РасходнаяНакладная",
            "document_display": 12345,
            "author_guid": 67890,
        }
        row.save(update_fields=["source_data"])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("finance_employee_rewards"),
            {"month": "2026-09"},
        )

        self.assertEqual(response.status_code, 200)

    def test_rewards_page_shows_saved_author_name_before_technical_guid(self):
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.org,
            role="owner",
        )
        author_guid = "22222222-2222-4222-8222-222222222222"
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": author_guid,
            "author_name": "Автор из 1С",
        }
        row.save(update_fields=["source_data"])
        identity = OneCAuthorIdentity.objects.create(
            organization=self.org,
            onec_user_id=author_guid,
            raw_name="",
            status=OneCAuthorIdentity.STATUS_NEEDS_MAPPING,
        )
        RewardParticipation.objects.create(
            organization=self.org,
            employee=None,
            author_identity=identity,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            status=RewardParticipation.STATUS_REQUIRED,
            share=Decimal("1.000000"),
            period_month=self.month,
            scope_key="author-display",
            source_document_key=(
                "odata-source:%s:Document_РасходнаяНакладная:"
                "11111111-1111-4111-8111-111111111111" % self.org.id
            ),
            source_document_type="Document_РасходнаяНакладная",
            assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("finance_employee_rewards"),
            {"month": "2026-09"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Автор из 1С")
        self.assertContains(response, author_guid)

    def test_sync_authors_backfills_missing_name_from_explicit_1c_lookup(self):
        author_guid = "22222222-2222-4222-8222-222222222222"
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": author_guid,
        }
        row.save(update_fields=["source_data"])
        OneCAuthorIdentity.objects.create(
            organization=self.org,
            onec_user_id=author_guid,
            raw_name="",
            status=OneCAuthorIdentity.STATUS_NEEDS_MAPPING,
        )

        with patch(
            "pool_service.services.rewards.read_odata_author_names",
            return_value={author_guid: "Автор из 1С"},
        ) as lookup:
            result = sync_author_proposals(self.org, self.user, self.month)

        lookup.assert_called_once_with({author_guid})
        identity = OneCAuthorIdentity.objects.get(
            organization=self.org,
            onec_user_id=author_guid,
        )
        self.assertEqual(identity.raw_name, "Автор из 1С")
        self.assertEqual(result["names_updated"], 1)

    def test_rewards_page_get_does_not_query_live_1c_for_author_names(self):
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.org,
            role="owner",
        )
        author_guid = "22222222-2222-4222-8222-222222222222"
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": author_guid,
        }
        row.save(update_fields=["source_data"])
        identity = OneCAuthorIdentity.objects.create(
            organization=self.org,
            onec_user_id=author_guid,
            raw_name="",
            status=OneCAuthorIdentity.STATUS_NEEDS_MAPPING,
        )
        RewardParticipation.objects.create(
            organization=self.org,
            employee=None,
            author_identity=identity,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            status=RewardParticipation.STATUS_REQUIRED,
            share=Decimal("1.000000"),
            period_month=self.month,
            scope_key="author-no-live-get",
            source_document_key=(
                "odata-source:%s:Document_РасходнаяНакладная:"
                "11111111-1111-4111-8111-111111111111" % self.org.id
            ),
            source_document_type="Document_РасходнаяНакладная",
            assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        with patch(
            "pool_service.services.rewards.read_odata_author_names",
            side_effect=AssertionError("GET must not call live 1C"),
        ) as lookup:
            response = self.client.get(
                reverse("finance_employee_rewards"),
                {"month": "2026-09"},
            )

        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()

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


    def test_manual_path_rejects_documentation_role(self):
        self.add_row(1, "1000.00")
        document = reward_document_options(self.org, self.month)[0]
        with self.assertRaisesMessage(Exception, "недоступна для ручного"):
            create_manual_participation(
                self.org, self.user, self.month,
                document_key=document["scope_key"],
                employee=self.e1,
                role=RewardParticipation.ROLE_DOCUMENTATION,
                share=Decimal("1"),
            )

    def test_missing_author_placeholder_can_be_assigned(self):
        self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        placeholder = RewardParticipation.objects.get(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )
        resolve_documentation_placeholder(
            placeholder, self.user, employee=self.e1
        )
        placeholder.refresh_from_db()
        self.assertEqual(placeholder.employee, self.e1)
        self.assertEqual(placeholder.status, RewardParticipation.STATUS_CONFIRMED)

    def test_changed_author_retires_previous_proposal(self):
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        old = RewardParticipation.objects.get(
            author_identity__onec_user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        row.source_data = {
            **row.source_data,
            "author_guid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "author_name": "Автор B",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        old.refresh_from_db()
        self.assertEqual(old.status, RewardParticipation.STATUS_NOT_APPLICABLE)
        self.assertTrue(
            RewardParticipation.objects.filter(
                author_identity__onec_user_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                period_month=self.month,
            ).exists()
        )

    def test_backdated_scheme_cannot_cross_later_closed_month(self):
        from pool_service.reward_models import RewardMonthClose
        march = date(2026, 11, 1)
        RewardMonthClose.objects.create(
            organization=self.org,
            period_month=march,
            scheme_version=self.scheme,
            snapshot={},
            source_hash="1" * 64,
            closed_by=self.user,
        )
        with self.assertRaisesMessage(Exception, "закрытый месяц"):
            create_scheme_version(
                self.org,
                self.user,
                effective_from=date(2026, 10, 1),
                values={"sale_rate": "0.11"},
            )

    def test_confirm_reloads_participation_after_lock(self):
        row = self.add_row(1, "1000.00", kind="Работа")
        self.participation(
            self.e1, RewardParticipation.ROLE_WORK, "0.500000",
            lines=[row.source_identity], scope="stale-confirm",
        )
        pending = RewardParticipation.objects.create(
            organization=self.org,
            employee=self.e2,
            role=RewardParticipation.ROLE_WORK,
            status=RewardParticipation.STATUS_PENDING,
            share=Decimal("0.400000"),
            period_month=self.month,
            scope_key="stale-confirm",
            source_document_key="odata-source:%s:Document_РасходнаяНакладная:11111111-1111-4111-8111-111111111111" % self.org.id,
            source_document_type="Document_РасходнаяНакладная",
            scope_line_identities=[row.source_identity],
            assignment_source=RewardParticipation.SOURCE_MANUAL,
            created_by=self.user,
        )
        stale = RewardParticipation.objects.get(pk=pending.pk)
        RewardParticipation.objects.filter(pk=pending.pk).update(share=Decimal("0.600000"))
        with self.assertRaisesMessage(Exception, "превышают 100%"):
            confirm_participation(stale, self.user)


    def test_direct_expense_does_not_create_documentation_placeholder(self):
        row = self.add_row(1, "-500.00")
        row.source_data = {
            **row.source_data,
            "row_kind": "direct_order_expense",
            "author_guid": "",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        self.assertFalse(
            RewardParticipation.objects.filter(
                organization=self.org,
                period_month=self.month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
            ).exists()
        )

    def test_overlapping_manual_line_scopes_are_rejected(self):
        a = self.add_row(1, "1000.00", kind="Работа")
        b = self.add_row(2, "2000.00", kind="Работа")
        document = reward_document_options(self.org, self.month)[0]
        create_manual_participation(
            self.org, self.user, self.month,
            document_key=document["scope_key"],
            employee=self.e1,
            role=RewardParticipation.ROLE_WORK,
            share=Decimal("1"),
            line_identities=[a.source_identity],
        )
        with self.assertRaisesMessage(Exception, "Пересекающиеся наборы"):
            create_manual_participation(
                self.org, self.user, self.month,
                document_key=document["scope_key"],
                employee=self.e2,
                role=RewardParticipation.ROLE_WORK,
                share=Decimal("1"),
                line_identities=[a.source_identity, b.source_identity],
            )

    def test_author_removed_retires_old_proposal(self):
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        old = RewardParticipation.objects.get(
            author_identity__onec_user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        row.source_data = {**row.source_data, "author_guid": "", "author_name": ""}
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        old.refresh_from_db()
        self.assertEqual(old.status, RewardParticipation.STATUS_NOT_APPLICABLE)
        self.assertTrue(
            RewardParticipation.objects.filter(
                organization=self.org,
                period_month=self.month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
                author_identity__isnull=True,
                status=RewardParticipation.STATUS_REQUIRED,
            ).exists()
        )

    def test_calculation_flags_stale_author_sync(self):
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
        row.save(update_fields=["source_data"])
        data = calculate_month(self.org, self.month)
        self.assertTrue(any(
            item["kind"] == "author_sync_stale" for item in data["issues"]
        ))


    def test_month_close_adjustment_is_not_documentation_unit(self):
        row = self.add_row(1, "-500.00")
        row.source_data = {
            **row.source_data,
            "recorder_type": "Document_ЗакрытиеМесяца",
            "author_guid": "",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        self.assertFalse(
            RewardParticipation.objects.filter(
                organization=self.org,
                period_month=self.month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
            ).exists()
        )
        data = calculate_month(self.org, self.month)
        self.assertFalse(any(
            issue["kind"] == "missing_documentation_role"
            for issue in data["issues"]
        ))

    def test_retired_unmapped_author_does_not_block_month(self):
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        old = RewardParticipation.objects.get(
            author_identity__onec_user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        old.status = RewardParticipation.STATUS_NOT_APPLICABLE
        old.save(update_fields=["status"])
        data = calculate_month(self.org, self.month)
        self.assertFalse(any(
            issue["kind"] == "unmapped_author"
            for issue in data["issues"]
        ))

    def test_present_author_retires_manually_resolved_identityless_placeholder(self):
        row = self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        placeholder = RewardParticipation.objects.get(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )
        resolve_documentation_placeholder(
            placeholder, self.user, employee=self.e1
        )
        placeholder.refresh_from_db()
        self.assertEqual(placeholder.status, RewardParticipation.STATUS_CONFIRMED)

        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        placeholder.refresh_from_db()
        self.assertEqual(
            placeholder.status,
            RewardParticipation.STATUS_NOT_APPLICABLE,
        )


    def test_removed_document_retires_generated_author_proposal(self):
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        proposal = RewardParticipation.objects.get(
            author_identity__onec_user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        row.delete()
        sync_author_proposals(self.org, self.user, self.month)
        proposal.refresh_from_db()
        self.assertEqual(
            proposal.status,
            RewardParticipation.STATUS_NOT_APPLICABLE,
        )

    def test_removed_document_is_flagged_stale_before_author_sync(self):
        row = self.add_row(1, "1000.00")
        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        row.delete()
        data = calculate_month(self.org, self.month)
        self.assertTrue(any(
            issue["kind"] == "author_sync_stale"
            for issue in data["issues"]
        ))

    def test_selected_line_moved_to_another_order_is_blocking(self):
        row = self.add_row(1, "1000.00", kind="Работа")
        document = reward_document_options(self.org, self.month)[0]
        item = create_manual_participation(
            self.org, self.user, self.month,
            document_key=document["scope_key"],
            employee=self.e1,
            role=RewardParticipation.ROLE_WORK,
            share=Decimal("1"),
            line_identities=[row.source_identity],
        )
        confirm_participation(item, self.user)
        row.source_data = {
            **row.source_data,
            "resolved_order_guid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        }
        row.save(update_fields=["source_data"])
        data = calculate_month(self.org, self.month)
        self.assertTrue(any(
            issue["kind"] == "moved_selected_lines"
            for issue in data["issues"]
        ))
        self.assertFalse(any(
            detail["scope_key"] == item.scope_key for detail in data["details"]
        ))


    def test_percent_input_accepts_comma_and_rejects_empty(self):
        self.assertEqual(_percent_value("10,5", "Продажа"), Decimal("0.105"))
        with self.assertRaisesMessage(Exception, "числовое значение"):
            _percent_value("", "Продажа")


    def test_all_lines_sale_scope_is_blocked_when_document_gains_order(self):
        row = self.add_row(1, "1000.00")
        document = reward_document_options(self.org, self.month)[0]
        item = create_manual_participation(
            self.org, self.user, self.month,
            document_key=document["scope_key"],
            employee=self.e1,
            role=RewardParticipation.ROLE_SALE,
            share=Decimal("1"),
            line_identities=[],
        )
        confirm_participation(item, self.user)
        row.source_data = {
            **row.source_data,
            "resolved_order_guid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        }
        row.save(update_fields=["source_data"])
        data = calculate_month(self.org, self.month)
        self.assertTrue(any(
            issue["kind"] == "moved_all_lines_scope"
            for issue in data["issues"]
        ))
        self.assertFalse(any(
            detail["scope_key"] == item.scope_key
            for detail in data["details"]
        ))


    def test_resync_reuses_resolved_missing_author_placeholder(self):
        self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        placeholder = RewardParticipation.objects.get(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )
        resolve_documentation_placeholder(
            placeholder, self.user, employee=self.e1
        )
        confirm_participation(placeholder, self.user)
        before_ids = list(
            RewardParticipation.objects.filter(
                organization=self.org,
                period_month=self.month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
                author_identity__isnull=True,
            ).values_list("id", flat=True)
        )

        sync_author_proposals(self.org, self.user, self.month)

        after = RewardParticipation.objects.filter(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )
        self.assertEqual(list(after.values_list("id", flat=True)), before_ids)
        self.assertEqual(after.get().status, RewardParticipation.STATUS_CONFIRMED)

    def test_ensure_test_scheme_backfills_before_future_version_with_unique_number(self):
        RewardSchemeVersion.objects.all().delete()
        future = RewardSchemeVersion.objects.create(
            organization=self.org,
            name="Тестовая схема №1",
            version=1,
            effective_from=date(2026, 11, 1),
            created_by=self.user,
        )

        backfilled = ensure_test_scheme(
            self.org, self.user, date(2026, 9, 1)
        )

        self.assertEqual(backfilled.version, 2)
        self.assertEqual(backfilled.effective_from, date(2026, 9, 1))
        self.assertEqual(backfilled.effective_to, date(2026, 10, 31))
        future.refresh_from_db()
        self.assertEqual(future.version, 1)
        self.assertEqual(future.effective_from, date(2026, 11, 1))


    def test_author_reappears_then_disappears_reactivates_placeholder(self):
        row = self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        placeholder = RewardParticipation.objects.get(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )

        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        placeholder.refresh_from_db()
        self.assertEqual(
            placeholder.status,
            RewardParticipation.STATUS_NOT_APPLICABLE,
        )

        row.source_data = {
            **row.source_data,
            "author_guid": "",
            "author_name": "",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)

        placeholder.refresh_from_db()
        self.assertEqual(
            placeholder.status,
            RewardParticipation.STATUS_REQUIRED,
        )
        self.assertIsNone(placeholder.employee_id)
        self.assertEqual(
            RewardParticipation.objects.filter(
                organization=self.org,
                period_month=self.month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
                author_identity__isnull=True,
            ).count(),
            1,
        )
        self.assertTrue(
            placeholder.changes.filter(
                reason="Автор_Key снова отсутствует после повторной синхронизации"
            ).exists()
        )


    def test_manually_dismissed_placeholder_reactivates_after_author_cycle(self):
        row = self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        placeholder = RewardParticipation.objects.get(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            author_identity__isnull=True,
        )
        resolve_documentation_placeholder(
            placeholder,
            self.user,
            not_applicable=True,
        )
        placeholder.refresh_from_db()
        self.assertEqual(
            placeholder.status,
            RewardParticipation.STATUS_NOT_APPLICABLE,
        )

        row.source_data = {
            **row.source_data,
            "author_guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "author_name": "Автор A",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)
        placeholder.refresh_from_db()
        self.assertEqual(
            placeholder.changes.order_by("-id").values_list("reason", flat=True).first(),
            "Источник 1С после синхронизации предоставил Автор_Key",
        )

        row.source_data = {
            **row.source_data,
            "author_guid": "",
            "author_name": "",
        }
        row.save(update_fields=["source_data"])
        sync_author_proposals(self.org, self.user, self.month)

        placeholder.refresh_from_db()
        self.assertEqual(
            placeholder.status,
            RewardParticipation.STATUS_REQUIRED,
        )
        self.assertIsNone(placeholder.employee_id)
        self.assertTrue(
            placeholder.changes.filter(
                reason="Автор_Key снова отсутствует после повторной синхронизации"
            ).exists()
        )


    def test_non_finite_fixed_reward_is_validation_error(self):
        with self.assertRaisesMessage(Exception, "конечными числами"):
            create_scheme_version(
                self.org,
                self.user,
                effective_from=date(2026, 10, 1),
                values={"documentation_retail_fixed": "NaN"},
            )

    def test_manual_assignment_is_confirmed_immediately_and_overflow_is_rejected(self):
        row = self.add_row(1, "1000.00", kind="Работа")
        document = reward_document_options(self.org, self.month)[0]
        item = create_manual_participation(
            self.org,
            self.user,
            self.month,
            document_key=document["scope_key"],
            employee=self.e1,
            role=RewardParticipation.ROLE_WORK,
            share=Decimal("1"),
            line_identities=[row.source_identity],
        )
        self.assertEqual(item.status, RewardParticipation.STATUS_CONFIRMED)
        self.assertEqual(item.confirmed_by, self.user)
        self.assertIsNotNone(item.confirmed_at)
        self.assertEqual(
            item.changes.get().after["status"],
            RewardParticipation.STATUS_CONFIRMED,
        )
        with self.assertRaisesMessage(Exception, "не может превышать 100%"):
            create_manual_participation(
                self.org,
                self.user,
                self.month,
                document_key=document["scope_key"],
                employee=self.e2,
                role=RewardParticipation.ROLE_WORK,
                share=Decimal("1"),
                line_identities=[row.source_identity],
            )

    def test_month_close_only_document_is_not_reward_order(self):
        row = self.add_row(1, "-500.00")
        row.source_data = {
            **row.source_data,
            "recorder_type": "Document_ЗакрытиеМесяца",
            "author_guid": "",
        }
        row.save(update_fields=["source_data"])
        self.assertEqual(reward_document_options(self.org, self.month), [])
        kinds = {item["kind"] for item in calculate_month(self.org, self.month)["issues"]}
        self.assertNotIn("missing_sale_role", kinds)
        self.assertNotIn("missing_work_role", kinds)
        self.assertNotIn("missing_documentation_role", kinds)

    def test_retail_report_cost_is_folded_into_individual_check(self):
        check_guid = "22222222-2222-4222-8222-222222222222"
        report_guid = "33333333-3333-4333-8333-333333333333"
        item_guid = "44444444-4444-4444-8444-444444444444"
        self.add_retail_row(
            recorder=check_guid,
            recorder_type="Document_ЧекККМ",
            line=1,
            source_date=date(2026, 9, 9),
            nomenclature_guid=item_guid,
            quantity="1",
            revenue="2800.00",
            cost="0.00",
            document_number="НФНФ-001057",
            report_recorder=report_guid,
        )
        self.add_retail_row(
            recorder=report_guid,
            recorder_type="Document_ОтчетОРозничныхПродажах",
            line=2,
            source_date=date(2026, 9, 9),
            nomenclature_guid=item_guid,
            quantity="0",
            revenue="0.00",
            cost="1651.00",
            report_recorder=report_guid,
        )

        rows = reward_profit_rows(self.org, self.month)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_recorder.hex, check_guid.replace("-", ""))
        self.assertEqual(rows[0].analytical_cost, Decimal("1651.00"))
        self.assertEqual(rows[0].displayed_gross_profit, Decimal("1149.00"))

        documents = reward_document_options(self.org, self.month)
        self.assertEqual(len(documents), 1)
        self.assertEqual(
            documents[0]["label"],
            "Чек ККМ №НФНФ-001057 от 09.09.2026",
        )
        self.assertEqual(documents[0]["revenue"], "2800.00")
        self.assertEqual(documents[0]["cost"], "1651.00")
        self.assertEqual(documents[0]["gross_profit"], "1149.00")

    def test_retail_check_gets_fixed_plus_separate_gp_percent(self):
        check_guid = "22222222-2222-4222-8222-222222222222"
        report_guid = "33333333-3333-4333-8333-333333333333"
        item_guid = "44444444-4444-4444-8444-444444444444"
        self.add_retail_row(
            recorder=check_guid,
            recorder_type="Document_ЧекККМ",
            line=1,
            source_date=date(2026, 9, 9),
            nomenclature_guid=item_guid,
            quantity="1",
            revenue="2800.00",
            cost="0.00",
            document_number="НФНФ-001057",
            report_recorder=report_guid,
        )
        self.add_retail_row(
            recorder=report_guid,
            recorder_type="Document_ОтчетОРозничныхПродажах",
            line=2,
            source_date=date(2026, 9, 9),
            nomenclature_guid=item_guid,
            quantity="0",
            revenue="0.00",
            cost="1651.00",
            report_recorder=report_guid,
        )
        key = (
            f"odata-source:{self.org.id}:Document_ЧекККМ:{check_guid}"
        )
        RewardParticipation.objects.create(
            organization=self.org,
            employee=self.e1,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            status=RewardParticipation.STATUS_CONFIRMED,
            share=Decimal("1.000000"),
            period_month=self.month,
            scope_key=key,
            source_document_key=key,
            source_document_type="Document_ЧекККМ",
            source_document_guid=check_guid,
            source_document_number="НФНФ-001057",
            source_document_date=date(2026, 9, 9),
            assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
            created_by=self.user,
            confirmed_by=self.user,
        )

        data = calculate_month(self.org, self.month)
        employee = next(
            item for item in data["employees"]
            if item["employee_id"] == self.e1.id
        )
        self.assertEqual(employee["documentation_reward"], "50.00")
        self.assertEqual(employee["retail_reward"], "11.49")
        self.assertEqual(employee["total"], "61.49")
        self.assertFalse(any(
            issue["kind"] == "missing_sale_role"
            for issue in data["issues"]
        ))

    def test_retail_return_reduces_original_check_gp_and_percent(self):
        check_guid = "22222222-2222-4222-8222-222222222222"
        sale_report = "33333333-3333-4333-8333-333333333333"
        return_guid = "55555555-5555-4555-8555-555555555555"
        return_report = "66666666-6666-4666-8666-666666666666"
        item_guid = "44444444-4444-4444-8444-444444444444"
        self.add_retail_row(
            recorder=check_guid,
            recorder_type="Document_ЧекККМ",
            line=1,
            source_date=date(2026, 9, 10),
            nomenclature_guid=item_guid,
            quantity="2",
            revenue="1000.00",
            cost="0.00",
            document_number="НФНФ-001062",
            report_recorder=sale_report,
        )
        self.add_retail_row(
            recorder=sale_report,
            recorder_type="Document_ОтчетОРозничныхПродажах",
            line=2,
            source_date=date(2026, 9, 10),
            nomenclature_guid=item_guid,
            quantity="0",
            revenue="0.00",
            cost="400.00",
            report_recorder=sale_report,
        )
        self.add_retail_row(
            recorder=return_guid,
            recorder_type="Document_ЧекККМВозврат",
            line=3,
            source_date=date(2026, 9, 14),
            nomenclature_guid=item_guid,
            quantity="-1",
            revenue="-500.00",
            cost="0.00",
            document_guid=check_guid,
        )
        self.add_retail_row(
            recorder=return_report,
            recorder_type="Document_ОтчетОРозничныхПродажах",
            line=4,
            source_date=date(2026, 9, 14),
            nomenclature_guid=item_guid,
            quantity="0",
            revenue="0.00",
            cost="-200.00",
            report_recorder=return_report,
        )
        # The return-day report has no positive check, so mirror the confirmed
        # report link used by import grouping with a zero-value check row.
        shadow_check = self.add_retail_row(
            recorder="77777777-7777-4777-8777-777777777777",
            recorder_type="Document_ЧекККМ",
            line=5,
            source_date=date(2026, 9, 14),
            nomenclature_guid="88888888-8888-4888-8888-888888888888",
            quantity="1",
            revenue="0.00",
            cost="0.00",
            document_number="НФНФ-000000",
            report_recorder=return_report,
        )
        shadow_check.delete()

        # Return-only retail days are still allocated using the single daily
        # report, even without a positive check row.
        rows = reward_profit_rows(self.org, self.month)
        original_scope = (
            f"odata-source:{self.org.id}:Document_ЧекККМ:{check_guid}"
        )
        scope_rows = [
            row for row in rows
            if __import__(
                "pool_service.services.rewards",
                fromlist=["_row_business_scope_key"],
            )._row_business_scope_key(row) == original_scope
        ]
        self.assertEqual(
            sum((Decimal(row.revenue or 0) for row in scope_rows), Decimal("0")),
            Decimal("500.00"),
        )

    def test_same_open_month_can_create_new_test_scheme_version(self):
        updated = create_scheme_version(
            self.org,
            self.user,
            effective_from=self.month,
            values={
                "documentation_retail_fixed": "50.00",
                "retail_check_rate": "0.02",
                "documentation_document_fixed": "200.00",
                "sale_rate": "0.07",
                "project_rate": "0.05",
                "work_rate": "0.30",
                "client_manager_rate": "0.01",
            },
        )
        self.assertEqual(updated.effective_from, self.month)
        self.assertEqual(updated.version, self.scheme.version + 1)
        self.assertEqual(updated.retail_check_rate, Decimal("0.020000"))
        self.assertEqual(
            scheme_for_month(self.org, self.month).id,
            updated.id,
        )

    def test_co_documenter_rejected_for_inactive_source(self):
        row = self.add_row(1, "1000.00")
        sync_author_proposals(self.org, self.user, self.month)
        proposal = RewardParticipation.objects.filter(
            organization=self.org,
            period_month=self.month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
        ).first()
        if proposal.employee_id is None:
            proposal.employee = self.e1
            proposal.status = RewardParticipation.STATUS_PENDING
            proposal.save(update_fields=["employee", "status"])
        row.delete()
        sync_author_proposals(self.org, self.user, self.month)
        proposal.refresh_from_db()
        with self.assertRaisesMessage(Exception, "активному назначению"):
            add_documentation_participant(
                proposal, self.e2, Decimal("0.5"), self.user
            )
