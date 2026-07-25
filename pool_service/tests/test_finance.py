import shutil
import tempfile
import uuid
from datetime import date, timedelta
from io import BytesIO

from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    AccountableTransaction,
    Client,
    Expense,
    ExpenseCategory,
    ExpensePeriod,
    ExpenseReceipt,
    Organization,
    OrganizationAccess,
)
from pool_service.services.finance import accountable_balance, ensure_default_categories


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
        self.manager = User.objects.create_user(username="finance-manager", password="pass", first_name="Manager")
        self.service = User.objects.create_user(username="finance-service", password="pass", first_name="Service")
        self.installer = User.objects.create_user(username="finance-installer", password="pass", first_name="Installer")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.service, organization=self.organization, role="service")
        OrganizationAccess.objects.create(user=self.installer, organization=self.organization, role="installer")
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

        self.client.force_login(self.manager)
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
        self.client.force_login(self.manager)
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
        self.client.force_login(self.manager)

        response = self.client.get(reverse("finance_report"))

        self.assertEqual(response.status_code, 200)
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
