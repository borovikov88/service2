import shutil
import tempfile
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    AccountableTransaction,
    AccountableTransactionChange,
    CashCount,
    CashOperation,
    CashOperationChange,
    Client,
    Expense,
    ExpenseCategory,
    ExpensePeriod,
    ExpenseReceipt,
    Organization,
    OrganizationAccess,
)
from pool_service.services.finance import accountable_balance, company_cash_balance, ensure_default_categories, format_money, kkm_cash_balance, manager_cash_balance


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    VAPID_PUBLIC_KEY="",
    VAPID_PRIVATE_KEY="",
)
class FinanceTests(TestCase):
    def setUp(self):
        self.private_directory = tempfile.mkdtemp(prefix="rovik-finance-tests-")
        self.receipt_storage = ExpenseReceipt._meta.get_field("file").storage
        self.original_storage_location = self.receipt_storage._location
        self.receipt_storage._location = self.private_directory
        self.receipt_storage.__dict__.pop("base_location", None)
        self.receipt_storage.__dict__.pop("location", None)

        self.organization = Organization.objects.create(
            name="Finance organization",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user(username="finance-owner", password="pass", first_name="Owner")
        self.admin = User.objects.create_user(username="finance-admin", password="pass", first_name="Admin")
        self.manager = User.objects.create_user(username="finance-manager", password="pass", first_name="Manager")
        self.service = User.objects.create_user(
            username="finance-service",
            password="pass",
            first_name="Service",
            last_name="Employee",
        )
        self.installer = User.objects.create_user(username="finance-installer", password="pass", first_name="Installer")
        self.accountant = User.objects.create_user(username="finance-accountant", password="pass", first_name="Accountant")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        OrganizationAccess.objects.create(user=self.admin, organization=self.organization, role="admin")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.service, organization=self.organization, role="service")
        OrganizationAccess.objects.create(user=self.installer, organization=self.organization, role="installer")
        OrganizationAccess.objects.create(user=self.accountant, organization=self.organization, role="accountant")
        ensure_default_categories(self.organization)
        self.category = ExpenseCategory.objects.get(organization=self.organization, name="Материалы")

    def tearDown(self):
        self.receipt_storage._location = self.original_storage_location
        self.receipt_storage.__dict__.pop("base_location", None)
        self.receipt_storage.__dict__.pop("location", None)
        shutil.rmtree(self.private_directory, ignore_errors=True)

    def receipt(self, name="receipt.jpg"):
        buffer = BytesIO()
        Image.new("RGB", (40, 40), "white").save(buffer, format="JPEG")
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")

    def expense_payload(self, **overrides):
        payload = {
            "request_id": str(uuid.uuid4()),
            "source": Expense.SOURCE_ACCOUNTABLE,
            "employee": self.service.id,
            "category": self.category.id,
            "amount": "2500.00",
            "spent_on": date.today().isoformat(),
            "destination_type": Expense.DESTINATION_CLIENT,
            "destination_query": "Новый клиент",
            "client_id": "",
            "pool": "",
            "vendor": "Поставщик",
            "description": "Материалы для объекта",
            "receipts": self.receipt(),
        }
        payload.update(overrides)
        return payload

    def create_expense(self, user=None, **overrides):
        self.client.force_login(user or self.service)
        return self.client.post(reverse("finance_expense_create"), self.expense_payload(**overrides))

    def test_installer_has_finance_access(self):
        self.client.force_login(self.installer)

        response = self.client.get(reverse("finance_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Добавить расход")
        self.assertIn(("installer", "Монтажник"), OrganizationAccess.ROLE_CHOICES)

    def test_default_expense_categories_are_limited_to_current_list(self):
        ExpenseCategory.objects.create(organization=self.organization, name="Вода", sort_order=30)
        ExpenseCategory.objects.create(organization=self.organization, name="Доставка", sort_order=31)

        ensure_default_categories(self.organization)

        active_categories = list(
            ExpenseCategory.objects.filter(organization=self.organization, is_active=True).values_list("name", flat=True)
        )
        self.assertEqual(active_categories, ["Материалы", "Работы", "Транспорт", "Инструмент", "Прочее"])
        self.assertFalse(
            ExpenseCategory.objects.filter(
                organization=self.organization,
                name__in=["Вода", "Доставка"],
                is_active=True,
            ).exists()
        )

    def test_finance_forms_show_employee_names_without_contacts(self):
        unnamed = User.objects.create_user(username="9236540444", password="pass")
        OrganizationAccess.objects.create(
            user=unnamed,
            organization=self.organization,
            role="service",
        )
        self.client.force_login(self.manager)

        transaction_response = self.client.get(reverse("finance_transaction_create"))
        expense_response = self.client.get(reverse("finance_expense_create"))
        transaction_labels = [label for _, label in transaction_response.context["form"].fields["employee"].choices]
        expense_labels = [label for _, label in expense_response.context["form"].fields["employee"].choices]

        self.assertIn("Service Employee", transaction_labels)
        self.assertIn("Service Employee", expense_labels)
        self.assertIn("Manager", transaction_labels)
        self.assertIn("Manager", expense_labels)
        self.assertIn("Имя не указано", transaction_labels)
        self.assertNotIn(self.service.username, transaction_labels)
        self.assertNotIn(unnamed.username, transaction_labels)

    def test_finance_create_forms_support_desktop_modal(self):
        self.client.force_login(self.manager)

        dashboard = self.client.get(reverse("finance_dashboard"))
        transaction = self.client.get(reverse("finance_transaction_create"), {"modal": "1"})
        income = self.client.get(reverse("finance_income_create"), {"modal": "1"})
        expense = self.client.get(reverse("finance_expense_create"), {"modal": "1"})

        self.assertContains(dashboard, 'id="financeFormModal"')
        self.assertContains(dashboard, 'class="btn btn-primary" data-finance-modal', count=1)
        self.assertContains(dashboard, 'class="btn btn-outline-primary" data-finance-modal', count=2)
        self.assertContains(dashboard, 'class="btn btn-outline-success" data-finance-modal', count=1)
        for response in (transaction, income, expense):
            self.assertTrue(response.context["finance_modal"])
            self.assertTrue(response.context["hide_header"])
            self.assertTrue(response.context["hide_bottom_nav"])
            self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_new_expense_does_not_offer_client_object(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("finance_expense_create"))

        self.assertContains(response, 'data-photo-picker="1"')
        self.assertNotContains(response, 'capture="environment"')
        self.assertContains(response, 'mode: "file"')
        self.assertNotIn("pool", response.context["form"].fields)
        self.assertNotContains(response, "Объект клиента")
        self.assertNotContains(response, "data-pool-select")

    def test_accountant_has_full_finance_access(self):
        self.client.force_login(self.accountant)

        dashboard = self.client.get(reverse("finance_dashboard"))
        report = self.client.get(reverse("finance_report"))
        transaction = self.client.post(
            reverse("finance_transaction_create"),
            {
                "employee": self.service.id,
                "transaction_type": AccountableTransaction.TYPE_ISSUE,
                "amount": "10000.00",
                "occurred_on": date.today().isoformat(),
                "note": "Подотчёт",
            },
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(transaction.status_code, 302)
        self.assertIn(("accountant", "Бухгалтер"), OrganizationAccess.ROLE_CHOICES)

        self.create_expense()
        expense = Expense.objects.get()
        self.client.force_login(self.accountant)
        review = self.client.post(
            reverse("finance_expense_review", kwargs={"expense_uuid": expense.uuid}),
            {"decision": Expense.STATUS_APPROVED, "review_comment": ""},
        )
        close_period = self.client.post(
            f"{reverse('finance_period_close')}?month={date.today():%Y-%m}"
        )

        expense.refresh_from_db()
        self.assertEqual(review.status_code, 302)
        self.assertEqual(expense.status, Expense.STATUS_APPROVED)
        self.assertEqual(close_period.status_code, 302)
        self.assertTrue(ExpensePeriod.objects.get(organization=self.organization).is_closed)

    def test_expense_without_receipt_requires_explicit_skip(self):
        self.client.force_login(self.service)
        payload = self.expense_payload()
        payload.pop("receipts")

        blocked = self.client.post(reverse("finance_expense_create"), payload)
        payload["receipt_missing_confirmed"] = "1"
        skipped = self.client.post(reverse("finance_expense_create"), payload)

        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "Добавьте фотографию/PDF чека или нажмите")
        self.assertEqual(skipped.status_code, 302)
        expense = Expense.objects.get()
        self.assertTrue(expense.receipt_missing_confirmed)
        self.assertEqual(expense.receipts.count(), 0)

    def test_client_payment_is_approved_on_create_with_client(self):
        self.client.force_login(self.service)

        created = self.client.post(
            reverse("finance_income_create"),
            {
                "employee": self.service.id,
                "destination_query": "Клиент Иванов",
                "amount": "1234564.00",
                "occurred_on": date.today().isoformat(),
                "note": "Клиент оплатил сервиснику",
            },
        )
        movement = AccountableTransaction.objects.get(transaction_type=AccountableTransaction.TYPE_CLIENT_PAYMENT)
        balance = accountable_balance(self.organization, self.service)

        self.client.force_login(self.manager)
        manager_review = self.client.post(
            reverse("finance_transaction_review", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED, "review_comment": ""},
        )
        movement.refresh_from_db()

        self.assertEqual(created.status_code, 302)
        self.assertEqual(movement.status, AccountableTransaction.STATUS_APPROVED)
        self.assertEqual(movement.client.name, "Клиент Иванов")
        self.assertEqual(manager_review.status_code, 403)
        self.assertEqual(balance["operational_balance"], 1234564)
        self.assertEqual(format_money(movement.amount), "1\u00a0234\u00a0564,00\u00a0₽")
        self.assertTrue(
            movement.changes.filter(action=AccountableTransactionChange.ACTION_CREATED).exists()
        )

    def test_client_payment_edit_and_delete_require_close_role_approval(self):
        client = Client.objects.create(organization=self.organization, name="Клиент А", client_type="private")
        movement = AccountableTransaction.objects.create(
            organization=self.organization,
            employee=self.service,
            created_by=self.service,
            transaction_type=AccountableTransaction.TYPE_CLIENT_PAYMENT,
            client=client,
            amount="1000.00",
            occurred_on=date.today(),
            status=AccountableTransaction.STATUS_APPROVED,
            reviewed_by=self.service,
            reviewed_at=timezone.now(),
        )
        self.client.force_login(self.service)

        edit_requested = self.client.post(
            reverse("finance_income_edit", kwargs={"transaction_id": movement.id}),
            {
                "employee": self.service.id,
                "client_id": client.id,
                "destination_query": client.name,
                "amount": "2000.00",
                "occurred_on": date.today().isoformat(),
                "note": "Исправленная сумма",
            },
        )
        movement.refresh_from_db()
        before_review = accountable_balance(self.organization, self.service)

        self.client.force_login(self.accountant)
        edit_reviewed = self.client.post(
            reverse("finance_transaction_review", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED, "review_comment": "Ок"},
        )
        movement.refresh_from_db()
        after_edit = accountable_balance(self.organization, self.service)

        self.client.force_login(self.service)
        delete_requested = self.client.post(
            reverse("finance_income_delete", kwargs={"transaction_id": movement.id}),
            {"reason": "Дубль"},
        )
        movement.refresh_from_db()
        before_delete_review = accountable_balance(self.organization, self.service)

        self.client.force_login(self.accountant)
        delete_reviewed = self.client.post(
            reverse("finance_transaction_review", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED, "review_comment": "Удалить"},
        )
        movement.refresh_from_db()
        after_delete = accountable_balance(self.organization, self.service)

        self.assertEqual(edit_requested.status_code, 302)
        self.assertEqual(before_review["operational_balance"], 1000)
        self.assertEqual(edit_reviewed.status_code, 302)
        self.assertEqual(movement.amount, 2000)
        self.assertEqual(after_edit["operational_balance"], 2000)
        self.assertEqual(delete_requested.status_code, 302)
        self.assertEqual(before_delete_review["operational_balance"], 2000)
        self.assertEqual(delete_reviewed.status_code, 302)
        self.assertTrue(movement.is_voided)
        self.assertEqual(after_delete["operational_balance"], 0)
        self.assertTrue(
            movement.changes.filter(action=AccountableTransactionChange.ACTION_UPDATED).exists()
        )
        self.assertTrue(
            movement.changes.filter(action=AccountableTransactionChange.ACTION_DELETED).exists()
        )

    def test_accountable_rows_open_employee_detail_with_history(self):
        AccountableTransaction.objects.create(
            organization=self.organization,
            employee=self.service,
            created_by=self.owner,
            transaction_type=AccountableTransaction.TYPE_ISSUE,
            amount="500.00",
            occurred_on=date.today(),
        )
        self.client.force_login(self.owner)

        dashboard = self.client.get(reverse("finance_dashboard"))
        detail = self.client.get(reverse("finance_employee_detail", kwargs={"employee_id": self.service.id}))

        self.assertContains(
            dashboard,
            f'data-row-href="{reverse("finance_employee_detail", kwargs={"employee_id": self.service.id})}"',
        )
        self.assertContains(detail, "Движения подотчёта")
        self.assertContains(detail, "История правок и удалений")

    def test_accountant_only_user_is_restricted_to_finance(self):
        self.client.force_login(self.accountant)
        finance_url = reverse("finance_dashboard")

        for route_name in ("pool_list", "readings_all", "clients_list", "crm_index", "users"):
            response = self.client.get(reverse(route_name))
            self.assertRedirects(response, finance_url, fetch_redirect_response=False)

        forbidden_post = self.client.post(reverse("client_create"), {})
        dashboard = self.client.get(finance_url)

        self.assertEqual(forbidden_post.status_code, 403)
        self.assertTrue(dashboard.context["finance_only_user"])
        self.assertNotContains(dashboard, f'href="{reverse("crm_index")}"')
        self.assertNotContains(dashboard, 'href="/readings/all"')
        self.assertNotContains(dashboard, 'href="/pools')
        self.assertNotContains(dashboard, f'href="{reverse("clients_list")}"')

    def test_accountant_with_operational_role_keeps_operational_access(self):
        OrganizationAccess.objects.create(
            user=self.accountant,
            organization=self.organization,
            role="service",
        )
        self.client.force_login(self.accountant)

        response = self.client.get(reverse("pool_list"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["finance_only_user"])

    def test_authenticated_home_redirects_by_role(self):
        cases = [
            (self.service, reverse("readings_all")),
            (self.installer, reverse("finance_dashboard")),
            (self.manager, reverse("finance_kkm_cash_dashboard")),
            (self.accountant, reverse("finance_dashboard")),
        ]

        for user, expected_url in cases:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("home"))
                self.assertRedirects(response, expected_url, fetch_redirect_response=False)
                self.client.logout()

    def test_issue_expense_and_approval_update_balance_without_double_counting(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("finance_transaction_create"),
            {
                "employee": self.service.id,
                "transaction_type": AccountableTransaction.TYPE_ISSUE,
                "amount": "10000.00",
                "occurred_on": date.today().isoformat(),
                "note": "Материалы",
            },
        )
        self.assertEqual(response.status_code, 302)
        movement = AccountableTransaction.objects.get(transaction_type=AccountableTransaction.TYPE_ISSUE)
        self.assertEqual(movement.status, AccountableTransaction.STATUS_PENDING)
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], 0)

        self.client.force_login(self.service)
        response = self.client.post(
            reverse("finance_transaction_confirm", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED},
        )
        self.assertEqual(response.status_code, 302)
        movement.refresh_from_db()
        self.assertEqual(movement.status, AccountableTransaction.STATUS_APPROVED)
        self.assertEqual(movement.reviewed_by, self.service)

        response = self.create_expense(
            source=Expense.SOURCE_COMPANY_CASH,
            employee=self.manager.id,
        )

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.source, Expense.SOURCE_ACCOUNTABLE)
        self.assertEqual(expense.employee, self.service)
        self.assertEqual(expense.status, Expense.STATUS_PENDING)
        pending_balance = accountable_balance(self.organization, self.service)
        self.assertEqual(pending_balance["confirmed_balance"], 10000)
        self.assertEqual(pending_balance["operational_balance"], 7500)

        self.client.force_login(self.accountant)
        response = self.client.post(
            reverse("finance_expense_review", kwargs={"expense_uuid": expense.uuid}),
            {"decision": Expense.STATUS_APPROVED, "review_comment": ""},
        )

        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.STATUS_APPROVED)
        approved_balance = accountable_balance(self.organization, self.service)
        self.assertEqual(approved_balance["confirmed_balance"], 7500)
        self.assertEqual(approved_balance["operational_balance"], 7500)

    def test_manager_can_issue_advance_without_viewing_employee_accountables(self):
        AccountableTransaction.objects.create(
            organization=self.organization,
            employee=self.service,
            created_by=self.owner,
            transaction_type=AccountableTransaction.TYPE_ISSUE,
            amount="500.00",
            occurred_on=date.today(),
        )
        self.client.force_login(self.manager)

        dashboard = self.client.get(reverse("finance_dashboard"))
        detail = self.client.get(reverse("finance_employee_detail", kwargs={"employee_id": self.service.id}))
        report = self.client.get(reverse("finance_report"))
        transaction_form = self.client.get(reverse("finance_transaction_create"))
        transaction_post = self.client.post(
            reverse("finance_transaction_create"),
            {
                "employee": self.service.id,
                "transaction_type": AccountableTransaction.TYPE_ISSUE,
                "amount": "1000.00",
                "occurred_on": date.today().isoformat(),
                "note": "manager advance",
            },
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, reverse("finance_transaction_create"))
        self.assertNotContains(
            dashboard,
            f'data-row-href="{reverse("finance_employee_detail", kwargs={"employee_id": self.service.id})}"',
            html=False,
        )
        self.assertEqual(detail.status_code, 403)
        self.assertEqual(report.status_code, 403)
        self.assertEqual(transaction_form.status_code, 200)
        self.assertEqual(transaction_post.status_code, 302)
        self.assertEqual(
            AccountableTransaction.objects.order_by("-id").first().status,
            AccountableTransaction.STATUS_PENDING,
        )

    def test_advance_issue_requires_recipient_confirmation(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("finance_transaction_create"),
            {
                "employee": self.service.id,
                "transaction_type": AccountableTransaction.TYPE_ISSUE,
                "amount": "1500.00",
                "occurred_on": date.today().isoformat(),
                "note": "parts",
            },
        )
        self.assertEqual(response.status_code, 302)
        movement = AccountableTransaction.objects.get(amount="1500.00")
        self.assertEqual(movement.status, AccountableTransaction.STATUS_PENDING)
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], 0)

        self.client.force_login(self.accountant)
        forbidden = self.client.post(
            reverse("finance_transaction_confirm", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED},
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.service)
        detail = self.client.get(reverse("finance_employee_detail", kwargs={"employee_id": self.service.id}))
        self.assertContains(detail, "Подтвердить")
        confirmed = self.client.post(
            reverse("finance_transaction_confirm", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED},
        )

        self.assertEqual(confirmed.status_code, 302)
        movement.refresh_from_db()
        self.assertEqual(movement.status, AccountableTransaction.STATUS_APPROVED)
        self.assertEqual(movement.reviewed_by, self.service)
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], 1500)
        self.assertTrue(movement.changes.filter(action=AccountableTransactionChange.ACTION_UPDATED).exists())

    def test_manager_cash_transfer_reaches_company_after_review(self):
        self.client.force_login(self.manager)
        dashboard = self.client.get(reverse("finance_kkm_cash_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Касса ККМ")

        income_response = self.client.post(
            reverse("finance_cash_income_create"),
            {
                "amount": "10000.00",
                "occurred_on": date.today().isoformat(),
                "note": "выручка за день",
            },
        )
        self.assertEqual(income_response.status_code, 302)
        income = CashOperation.objects.get(operation_type=CashOperation.TYPE_MANAGER_INCOME)
        self.assertEqual(income.manager, self.manager)
        self.assertEqual(income.created_by, self.manager)
        self.assertEqual(income.status, CashOperation.STATUS_APPROVED)
        self.assertEqual(income.reviewed_by, self.manager)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["balance"], 10000)
        self.assertEqual(kkm_cash_balance(self.organization)["balance"], 10000)
        self.assertTrue(income.changes.filter(action=CashOperationChange.ACTION_APPROVED).exists())
        edit_after_review = self.client.get(reverse("finance_cash_operation_edit", kwargs={"operation_id": income.id}))
        self.assertEqual(edit_after_review.status_code, 302)

        transfer_response = self.client.post(
            reverse("finance_cash_transfer_create"),
            {
                "receiver": self.accountant.id,
                "amount": "7000.00",
                "occurred_on": date.today().isoformat(),
                "note": "сдача выручки",
            },
        )
        self.assertEqual(transfer_response.status_code, 302)
        transfer = CashOperation.objects.get(operation_type=CashOperation.TYPE_TRANSFER_TO_COMPANY)
        self.assertEqual(transfer.receiver, self.accountant)
        self.assertEqual(transfer.status, CashOperation.STATUS_PENDING)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["balance"], 10000)
        self.assertEqual(company_cash_balance(self.organization)["balance"], 0)

        self.client.force_login(self.owner)
        company_dashboard = self.client.get(reverse("finance_company_cash_dashboard"))
        self.assertContains(company_dashboard, "Касса организации")
        review_transfer = self.client.post(
            reverse("finance_cash_operation_review", kwargs={"operation_id": transfer.id}),
            {"decision": CashOperation.STATUS_APPROVED},
        )

        self.assertEqual(review_transfer.status_code, 302)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, CashOperation.STATUS_APPROVED)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["balance"], 3000)
        self.assertEqual(company_cash_balance(self.organization)["balance"], 7000)

    def test_manager_cashbox_can_issue_accountable_after_employee_confirmation(self):
        CashOperation.objects.create(
            organization=self.organization,
            manager=self.manager,
            created_by=self.manager,
            operation_type=CashOperation.TYPE_MANAGER_INCOME,
            amount="10000.00",
            occurred_on=date.today(),
            status=CashOperation.STATUS_APPROVED,
            reviewed_by=self.accountant,
            reviewed_at=timezone.now(),
        )

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("finance_cash_accountable_issue_create"),
            {
            "employee": self.service.id,
                "amount": "14000.00",
                "occurred_on": date.today().isoformat(),
                "note": "Материалы с объекта",
            },
        )

        self.assertEqual(response.status_code, 302)
        movement = AccountableTransaction.objects.get(transaction_type=AccountableTransaction.TYPE_ISSUE)
        operation = CashOperation.objects.get(operation_type=CashOperation.TYPE_ACCOUNTABLE_ISSUE)
        self.assertEqual(movement.employee, self.service)
        self.assertEqual(movement.status, AccountableTransaction.STATUS_PENDING)
        self.assertEqual(operation.accountable_transaction, movement)
        self.assertEqual(operation.status, CashOperation.STATUS_PENDING)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["balance"], 10000)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["available_balance"], -4000)
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], 0)

        self.client.force_login(self.accountant)
        forbidden = self.client.post(
            reverse("finance_cash_operation_review", kwargs={"operation_id": operation.id}),
            {"decision": CashOperation.STATUS_APPROVED},
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.service)
        confirmed = self.client.post(
            reverse("finance_transaction_confirm", kwargs={"transaction_id": movement.id}),
            {"decision": AccountableTransaction.STATUS_APPROVED},
        )

        self.assertEqual(confirmed.status_code, 302)
        movement.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(movement.status, AccountableTransaction.STATUS_APPROVED)
        self.assertEqual(operation.status, CashOperation.STATUS_APPROVED)
        self.assertEqual(operation.reviewed_by, self.service)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["balance"], -4000)
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], 14000)

    def test_kkm_cash_expense_does_not_affect_accountable_balance(self):
        CashOperation.objects.create(
            organization=self.organization,
            manager=self.manager,
            created_by=self.manager,
            reviewed_by=self.manager,
            reviewed_at=timezone.now(),
            operation_type=CashOperation.TYPE_MANAGER_INCOME,
            amount="5000.00",
            occurred_on=date.today(),
            status=CashOperation.STATUS_APPROVED,
        )

        self.client.force_login(self.manager)
        dashboard = self.client.get(reverse("finance_kkm_cash_dashboard"))
        self.assertContains(dashboard, f"{reverse('finance_expense_create')}?source=kkm_cash")
        response = self.client.post(
            f"{reverse('finance_expense_create')}?source=kkm_cash",
            self.expense_payload(
                source=Expense.SOURCE_KKM_CASH,
                employee=self.manager.id,
                amount="1200.00",
                destination_type=Expense.DESTINATION_OFFICE,
                destination_query="",
            ),
        )

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get(source=Expense.SOURCE_KKM_CASH)
        self.assertEqual(expense.employee, self.manager)
        self.assertEqual(expense.status, Expense.STATUS_PENDING)
        self.assertEqual(accountable_balance(self.organization, self.manager)["operational_balance"], Decimal("0.00"))
        self.assertEqual(kkm_cash_balance(self.organization)["balance"], Decimal("5000.00"))
        self.assertEqual(kkm_cash_balance(self.organization)["pending_kkm_expense"], Decimal("1200.00"))
        self.assertEqual(kkm_cash_balance(self.organization)["available_balance"], Decimal("3800.00"))

        self.client.force_login(self.owner)
        review_response = self.client.post(
            reverse("finance_expense_review", kwargs={"expense_uuid": expense.uuid}),
            {"decision": Expense.STATUS_APPROVED, "review_comment": ""},
        )

        self.assertEqual(review_response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.STATUS_APPROVED)
        self.assertEqual(kkm_cash_balance(self.organization)["balance"], Decimal("3800.00"))
        self.assertEqual(accountable_balance(self.organization, self.manager)["operational_balance"], Decimal("0.00"))

    def test_admin_can_temporarily_delete_kkm_cash_operation_with_related_records(self):
        movement = AccountableTransaction.objects.create(
            organization=self.organization,
            employee=self.service,
            created_by=self.manager,
            transaction_type=AccountableTransaction.TYPE_ISSUE,
            amount="2500.00",
            occurred_on=date.today(),
            status=AccountableTransaction.STATUS_PENDING,
        )
        AccountableTransactionChange.objects.create(
            transaction=movement,
            actor=self.manager,
            action=AccountableTransactionChange.ACTION_CREATED,
        )
        operation = CashOperation.objects.create(
            organization=self.organization,
            manager=self.manager,
            created_by=self.manager,
            operation_type=CashOperation.TYPE_ACCOUNTABLE_ISSUE,
            accountable_transaction=movement,
            amount="2500.00",
            occurred_on=date.today(),
            status=CashOperation.STATUS_PENDING,
        )
        CashOperationChange.objects.create(
            operation=operation,
            actor=self.manager,
            action=CashOperationChange.ACTION_CREATED,
        )

        self.client.force_login(self.owner)
        forbidden = self.client.post(reverse("finance_cash_operation_delete", kwargs={"operation_id": operation.id}))
        self.assertEqual(forbidden.status_code, 403)
        self.assertTrue(CashOperation.objects.filter(id=operation.id).exists())

        self.client.force_login(self.admin)
        dashboard = self.client.get(reverse("finance_kkm_cash_dashboard"))
        self.assertContains(dashboard, reverse("finance_cash_operation_delete", kwargs={"operation_id": operation.id}))
        response = self.client.post(reverse("finance_cash_operation_delete", kwargs={"operation_id": operation.id}))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CashOperation.objects.filter(id=operation.id).exists())
        self.assertFalse(CashOperationChange.objects.filter(operation_id=operation.id).exists())
        self.assertFalse(AccountableTransaction.objects.filter(id=movement.id).exists())
        self.assertFalse(AccountableTransactionChange.objects.filter(transaction_id=movement.id).exists())

    def test_service_cannot_access_kkm_cashbox(self):
        self.client.force_login(self.service)
        old_dashboard = self.client.get(reverse("finance_cash_dashboard"))
        dashboard = self.client.get(reverse("finance_kkm_cash_dashboard"))
        company_dashboard = self.client.get(reverse("finance_company_cash_dashboard"))
        income = self.client.get(reverse("finance_cash_income_create"))
        accountable_issue = self.client.get(reverse("finance_cash_accountable_issue_create"))
        transfer = self.client.get(reverse("finance_cash_transfer_create"))
        kkm_count = self.client.get(reverse("finance_cash_count_create", kwargs={"cashbox_type": CashCount.CASHBOX_KKM}))

        self.assertEqual(old_dashboard.status_code, 302)
        self.assertEqual(dashboard.status_code, 403)
        self.assertEqual(company_dashboard.status_code, 403)
        self.assertEqual(income.status_code, 403)
        self.assertEqual(accountable_issue.status_code, 403)
        self.assertEqual(transfer.status_code, 403)
        self.assertEqual(kkm_count.status_code, 403)

    def test_accountable_return_to_kkm_requires_manager_confirmation(self):
        AccountableTransaction.objects.create(
            organization=self.organization,
            employee=self.service,
            created_by=self.owner,
            transaction_type=AccountableTransaction.TYPE_ISSUE,
            amount="5000.00",
            occurred_on=date.today(),
            status=AccountableTransaction.STATUS_APPROVED,
            reviewed_by=self.owner,
            reviewed_at=timezone.now(),
        )

        self.client.force_login(self.service)
        response = self.client.post(
            reverse("finance_accountable_return_create"),
            {
                "manager": self.manager.id,
                "amount": "1200.00",
                "occurred_on": date.today().isoformat(),
                "note": "Возврат остатка",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("finance_dashboard"))
        movement = AccountableTransaction.objects.get(transaction_type=AccountableTransaction.TYPE_RETURN)
        operation = CashOperation.objects.get(operation_type=CashOperation.TYPE_ACCOUNTABLE_RETURN)
        self.assertEqual(movement.employee, self.service)
        self.assertEqual(movement.status, AccountableTransaction.STATUS_PENDING)
        self.assertEqual(operation.manager, self.manager)
        self.assertEqual(operation.accountable_transaction, movement)
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["pending_accountable_return"], Decimal("1200.00"))
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], Decimal("5000.00"))

        self.client.force_login(self.accountant)
        forbidden = self.client.post(
            reverse("finance_cash_operation_review", kwargs={"operation_id": operation.id}),
            {"decision": CashOperation.STATUS_APPROVED},
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.manager)
        approved = self.client.post(
            reverse("finance_cash_operation_review", kwargs={"operation_id": operation.id}),
            {"decision": CashOperation.STATUS_APPROVED},
        )

        self.assertEqual(approved.status_code, 302)
        movement.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(movement.status, AccountableTransaction.STATUS_APPROVED)
        self.assertEqual(operation.status, CashOperation.STATUS_APPROVED)
        self.assertEqual(accountable_balance(self.organization, self.service)["operational_balance"], Decimal("3800.00"))
        self.assertEqual(manager_cash_balance(self.organization, self.manager)["balance"], Decimal("1200.00"))
        self.assertTrue(operation.changes.filter(action=CashOperationChange.ACTION_APPROVED).exists())

    def test_cash_count_stores_denominations_and_total(self):
        self.client.force_login(self.owner)
        company_response = self.client.post(
            reverse("finance_cash_count_create", kwargs={"cashbox_type": CashCount.CASHBOX_COMPANY}),
            {
                "occurred_on": date.today().isoformat(),
                "note": "Пересчёт компании",
                "bill_5000": "1",
                "coin_5": "4",
            },
        )
        self.assertEqual(company_response.status_code, 302)
        company_count = CashCount.objects.get(cashbox_type=CashCount.CASHBOX_COMPANY)
        self.assertIsNone(company_count.manager)
        self.assertEqual(company_count.total, Decimal("5020.00"))
        self.assertEqual(company_count.denominations["bill_5000"], 1)

        self.client.force_login(self.manager)
        kkm_response = self.client.post(
            reverse("finance_cash_count_create", kwargs={"cashbox_type": CashCount.CASHBOX_KKM}),
            {
                "occurred_on": date.today().isoformat(),
                "note": "Пересчёт ККМ",
                "bill_100": "3",
                "coin_2": "2",
                "manual_amount": "11000.00",
            },
        )
        self.assertEqual(kkm_response.status_code, 302)
        kkm_count = CashCount.objects.get(cashbox_type=CashCount.CASHBOX_KKM)
        self.assertIsNone(kkm_count.manager)
        self.assertEqual(kkm_count.total, Decimal("11304.00"))
        self.assertEqual(kkm_count.denominations["manual_amount"], "11000.00")
        self.assertEqual(kkm_count.denominations["expected_balance"], "0.00")
        self.assertEqual(kkm_count.denominations["difference"], "11304.00")
        self.assertEqual(kkm_cash_balance(self.organization)["balance"], Decimal("0.00"))
        self.assertFalse(CashOperation.objects.filter(operation_type=CashOperation.TYPE_CASH_COUNT_INCOME).exists())

    def test_cash_count_mismatch_actions_prefill_amount(self):
        self.client.force_login(self.manager)

        count_response = self.client.get(
            reverse("finance_cash_count_create", kwargs={"cashbox_type": CashCount.CASHBOX_KKM}),
            {"modal": "1"},
        )
        self.assertContains(count_response, 'id="cashCountMismatchModal"')
        self.assertContains(count_response, 'data-mismatch-issue')
        self.assertContains(count_response, 'data-mismatch-transfer')
        self.assertContains(count_response, 'data-mismatch-income')
        self.assertContains(count_response, "issueButton.hidden = currentDiff >= 0;")
        self.assertContains(count_response, "transferButton.hidden = currentDiff >= 0;")
        self.assertContains(count_response, "incomeButton.hidden = currentDiff <= 0;")

        for url_name in (
            "finance_cash_income_create",
            "finance_cash_transfer_create",
            "finance_cash_accountable_issue_create",
        ):
            response = self.client.get(reverse(url_name), {"amount": "123.45", "modal": "1"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["form"].initial["amount"], "123.45")
            self.assertTrue(response.context["finance_modal"])

    def test_new_client_is_created_once_from_expense(self):
        first_request_id = str(uuid.uuid4())
        response = self.create_expense(request_id=first_request_id, destination_query="Клиент Альфа")
        self.assertEqual(response.status_code, 302)

        response = self.create_expense(destination_query="клиент альфа")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Client.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(Expense.objects.filter(client__name="Клиент Альфа").count(), 2)

    def test_office_expense_has_no_client(self):
        response = self.create_expense(
            destination_type=Expense.DESTINATION_OFFICE,
            destination_query="Не должен сохраниться",
        )

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertIsNone(expense.client)
        self.assertEqual(expense.destination_name, "Офисные расходы")

    def test_manager_cannot_review_own_expense_but_owner_can(self):
        response = self.create_expense(
            user=self.manager,
            source=Expense.SOURCE_COMPANY_CASH,
            employee=self.manager.id,
            destination_type=Expense.DESTINATION_OFFICE,
            destination_query="",
        )
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()

        response = self.client.post(
            reverse("finance_expense_review", kwargs={"expense_uuid": expense.uuid}),
            {"decision": Expense.STATUS_APPROVED, "review_comment": ""},
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("finance_expense_review", kwargs={"expense_uuid": expense.uuid}),
            {"decision": Expense.STATUS_APPROVED, "review_comment": ""},
        )
        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.STATUS_APPROVED)

    def test_only_expense_creator_can_delete_expense(self):
        self.create_expense()
        expense = Expense.objects.get()
        receipt = expense.receipts.get()
        receipt_name = receipt.file.name

        self.client.force_login(self.manager)
        forbidden = self.client.post(reverse("finance_expense_delete", kwargs={"expense_uuid": expense.uuid}))
        self.client.force_login(self.service)
        deleted = self.client.post(reverse("finance_expense_delete", kwargs={"expense_uuid": expense.uuid}))

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(ExpenseReceipt.objects.count(), 0)
        self.assertFalse(self.receipt_storage.exists(receipt_name))

    def test_receipts_are_protected_between_organizations(self):
        self.create_expense()
        receipt = ExpenseReceipt.objects.get()
        detail = self.client.get(reverse("finance_expense_detail", kwargs={"expense_uuid": receipt.expense.uuid}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, receipt.original_name)
        other_organization = Organization.objects.create(
            name="Other finance organization",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_user = User.objects.create_user(username="other-finance-user", password="pass")
        OrganizationAccess.objects.create(user=other_user, organization=other_organization, role="manager")
        self.client.force_login(other_user)

        response = self.client.get(reverse("finance_receipt_download", kwargs={"receipt_id": receipt.id}))

        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.service)
        response = self.client.get(reverse("finance_receipt_download", kwargs={"receipt_id": receipt.id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_invalid_receipt_is_rejected(self):
        invalid_file = SimpleUploadedFile("receipt.jpg", b"not-an-image", content_type="image/jpeg")

        response = self.create_expense(receipts=invalid_file)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "не является корректным изображением")
        self.assertEqual(Expense.objects.count(), 0)

    def test_offline_request_is_idempotent(self):
        request_id = str(uuid.uuid4())
        self.client.force_login(self.service)
        first = self.client.post(
            reverse("finance_expense_create"),
            self.expense_payload(request_id=request_id),
            HTTP_X_FINANCE_OFFLINE="1",
            HTTP_ACCEPT="application/json",
        )
        second = self.client.post(
            reverse("finance_expense_create"),
            self.expense_payload(request_id=request_id),
            HTTP_X_FINANCE_OFFLINE="1",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertJSONEqual(first.content, {"ok": True, "expense_uuid": request_id, "detail_url": f"/finance/expenses/{request_id}/"})
        self.assertEqual(Expense.objects.filter(uuid=request_id).count(), 1)

    def test_report_counts_only_actual_approved_expenses(self):
        AccountableTransaction.objects.create(
            organization=self.organization,
            employee=self.service,
            transaction_type=AccountableTransaction.TYPE_ISSUE,
            amount="10000.00",
            occurred_on=date.today(),
            created_by=self.manager,
        )
        self.create_expense()
        employee_expense = Expense.objects.get()
        self.client.force_login(self.accountant)
        self.client.post(
            reverse("finance_expense_review", kwargs={"expense_uuid": employee_expense.uuid}),
            {"decision": Expense.STATUS_APPROVED, "review_comment": ""},
        )
        self.create_expense(
            user=self.owner,
            amount="500.00",
            source=Expense.SOURCE_COMPANY_CASH,
            employee=self.owner.id,
            destination_type=Expense.DESTINATION_OFFICE,
            destination_query="",
        )
        self.client.force_login(self.accountant)

        response = self.client.get(reverse("finance_report"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-expense-bulk-root')
        self.assertContains(response, 'data-expense-bulk-clear')
        self.assertContains(response, 'class="date-cell"')
        self.assertContains(response, 'expense-employee-cell__last')
        self.assertContains(response, 'expense-purpose-cell__main')
        self.assertContains(response, 'data-expense-checkbox')
        self.assertContains(response, 'expense-inline-icon')
        self.assertContains(response, 'expense-status-icon')
        self.assertContains(response, 'value="pending"')
        self.assertContains(response, reverse("finance_expense_bulk_review"))
        self.assertEqual(response.context["approved_total"], 3000)
        self.assertEqual(response.context["office_total"], 500)
        export = self.client.get(reverse("finance_report_export"))
        self.assertEqual(export.status_code, 200)
        self.assertEqual(
            export["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        invalid_filter = self.client.get(reverse("finance_report"), {"employee": "invalid"})
        self.assertEqual(invalid_filter.status_code, 200)

    def test_report_bulk_review_updates_selected_pending_expenses(self):
        self.create_expense()
        expense = Expense.objects.get()
        self.client.force_login(self.accountant)

        response = self.client.post(
            reverse("finance_expense_bulk_review"),
            {
                "expense_ids": [str(expense.uuid)],
                "decision": Expense.STATUS_APPROVED,
                "review_comment": "bulk ok",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.STATUS_APPROVED)
        self.assertEqual(expense.review_comment, "bulk ok")

        response = self.client.post(
            reverse("finance_expense_bulk_review"),
            {
                "expense_ids": [str(expense.uuid)],
                "decision": Expense.STATUS_PENDING,
                "review_comment": "back to review",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.STATUS_PENDING)
        self.assertIsNone(expense.reviewed_by)

    def test_closed_month_blocks_new_expenses(self):
        self.client.force_login(self.owner)
        month = date.today().strftime("%Y-%m")
        response = self.client.post(f"{reverse('finance_period_close')}?month={month}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ExpensePeriod.objects.get(organization=self.organization).is_closed)

        response = self.create_expense()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Этот месяц закрыт")
        self.assertEqual(Expense.objects.count(), 0)
