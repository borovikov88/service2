from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import Q
from django.urls import reverse

from pool_service.models import (
    AccountableTransaction,
    CashOperation,
    Client,
    Expense,
    ExpenseCategory,
    ExpensePeriod,
    OrganizationAccess,
)
from pool_service.services.notifications import notify_users


DEFAULT_EXPENSE_CATEGORIES = [
    "Материалы",
    "Работы",
    "Транспорт",
    "Инструмент",
    "Прочее",
]
LEGACY_EXPENSE_CATEGORIES = ["Доставка", "Вода"]

FINANCE_ACCESS_ROLES = {"owner", "admin", "manager", "service", "installer", "accountant"}
FINANCE_MANAGE_ROLES = {"owner", "admin", "accountant"}
FINANCE_CASH_ACCESS_ROLES = {"owner", "admin", "manager", "accountant"}
FINANCE_ADVANCE_ISSUE_ROLES = {"owner", "admin", "manager", "accountant"}
FINANCE_CLOSE_ROLES = {"owner", "admin", "accountant"}
MANAGEMENT_FINANCE_ROLES = {"owner", "admin", "accountant"}


def organization_roles(user, organization):
    if not user or not user.is_authenticated or not organization:
        return set()
    return set(
        OrganizationAccess.objects.filter(
            user=user,
            organization=organization,
        ).values_list("role", flat=True)
    )


def can_access_finance(user, organization):
    if user.is_superuser:
        return bool(organization)
    return bool(organization_roles(user, organization) & FINANCE_ACCESS_ROLES)


def can_manage_finance(user, organization):
    if user.is_superuser:
        return bool(organization)
    return bool(organization_roles(user, organization) & FINANCE_MANAGE_ROLES)


def can_issue_accountable_transaction(user, organization):
    if user.is_superuser:
        return bool(organization)
    return bool(organization_roles(user, organization) & FINANCE_ADVANCE_ISSUE_ROLES)


def can_close_finance_period(user, organization):
    if user.is_superuser:
        return bool(organization)
    return bool(organization_roles(user, organization) & FINANCE_CLOSE_ROLES)


def can_access_cash(user, organization):
    if user.is_superuser:
        return bool(organization)
    return bool(organization_roles(user, organization) & FINANCE_CASH_ACCESS_ROLES)


def can_manage_cash(user, organization):
    return can_manage_finance(user, organization)


def _has_management_finance_role(user, organization):
    if user.is_superuser:
        return bool(organization)
    return bool(organization_roles(user, organization) & MANAGEMENT_FINANCE_ROLES)


def can_view_cashflow(user, organization):
    return _has_management_finance_role(user, organization)


def can_import_cashflow(user, organization):
    return _has_management_finance_role(user, organization)


def can_manage_cashflow_classification(user, organization):
    return _has_management_finance_role(user, organization)


def can_view_payroll_summary(user, organization):
    return _has_management_finance_role(user, organization)


def can_view_payroll_personal(user, organization):
    return _has_management_finance_role(user, organization)


def can_import_payroll(user, organization):
    return _has_management_finance_role(user, organization)


def can_manage_employee_mapping(user, organization):
    return _has_management_finance_role(user, organization)


def can_review_card_transfer_payment(user, payment):
    if not payment:
        return False
    return can_manage_finance(user, payment.organization)


def can_view_card_transfer_payment(user, payment):
    if not payment:
        return False
    return can_access_cash(user, payment.organization)


def can_review_expense(user, expense):
    if not can_manage_finance(user, expense.organization):
        return False
    roles = organization_roles(user, expense.organization)
    if roles & FINANCE_CLOSE_ROLES or user.is_superuser:
        return True
    return user.id not in {expense.employee_id, expense.created_by_id}


def can_view_expense(user, expense):
    if can_manage_finance(user, expense.organization):
        return True
    if not can_access_finance(user, expense.organization):
        return False
    return user.id in {expense.employee_id, expense.created_by_id}


def can_edit_expense(user, expense):
    if expense.status not in {Expense.STATUS_PENDING, Expense.STATUS_REJECTED}:
        return False
    if can_manage_finance(user, expense.organization):
        return True
    return can_access_finance(user, expense.organization) and user.id == expense.employee_id


def can_review_accountable_transaction(user, movement):
    if user.is_superuser:
        return True
    roles = organization_roles(user, movement.organization)
    if not roles & FINANCE_CLOSE_ROLES:
        return False
    return True


def can_confirm_accountable_issue(user, movement):
    return (
        movement
        and movement.transaction_type == AccountableTransaction.TYPE_ISSUE
        and movement.status == AccountableTransaction.STATUS_PENDING
        and not movement.is_voided
        and not movement.pending_action
        and can_access_finance(user, movement.organization)
        and user.id == movement.employee_id
    )


def finance_staff(organization):
    return (
        User.objects.filter(
            organizationaccess__organization=organization,
            organizationaccess__role__in=FINANCE_ACCESS_ROLES,
            is_active=True,
        )
        .distinct()
        .order_by("last_name", "first_name", "username")
    )


def manager_staff(organization):
    return (
        User.objects.filter(
            organizationaccess__organization=organization,
            organizationaccess__role="manager",
            is_active=True,
        )
        .distinct()
        .order_by("last_name", "first_name", "username")
    )


def user_display_name(user):
    return user.get_full_name().strip() or "Имя не указано"


def format_money(value, *, currency=True):
    amount = Decimal(value or 0)
    formatted = f"{amount:,.2f}".replace(",", "\u00a0").replace(".", ",")
    return f"{formatted}\u00a0₽" if currency else formatted


def find_client_by_name(organization, name):
    normalized_name = " ".join((name or "").split()).casefold()
    if not normalized_name:
        return None
    for client in Client.objects.filter(organization=organization).only("id", "name", "organization_id"):
        if " ".join(client.name.split()).casefold() == normalized_name:
            return client
    return None


def ensure_default_categories(organization):
    categories = []
    for sort_order, name in enumerate(DEFAULT_EXPENSE_CATEGORIES, start=1):
        category, _ = ExpenseCategory.objects.get_or_create(
            organization=organization,
            name=name,
            defaults={"sort_order": sort_order},
        )
        changed_fields = []
        if category.sort_order != sort_order:
            category.sort_order = sort_order
            changed_fields.append("sort_order")
        if not category.is_active:
            category.is_active = True
            changed_fields.append("is_active")
        if changed_fields:
            category.save(update_fields=changed_fields)
        categories.append(category)
    ExpenseCategory.objects.filter(
        organization=organization,
        name__in=LEGACY_EXPENSE_CATEGORIES,
        is_active=True,
    ).update(is_active=False)
    return categories


def month_bounds(month_value):
    if isinstance(month_value, str):
        year_value, month_number = [int(part) for part in month_value.split("-", 1)]
    else:
        year_value, month_number = month_value.year, month_value.month
    start = date(year_value, month_number, 1)
    end = date(year_value, month_number, monthrange(year_value, month_number)[1])
    return start, end


def period_is_closed(organization, value):
    month = date(value.year, value.month, 1)
    return ExpensePeriod.objects.filter(
        organization=organization,
        month=month,
        closed_at__isnull=False,
    ).exists()


def transaction_effect(transaction_type, amount):
    if transaction_type in {
        AccountableTransaction.TYPE_ISSUE,
        AccountableTransaction.TYPE_ADJUSTMENT_IN,
        AccountableTransaction.TYPE_CLIENT_PAYMENT,
    }:
        return amount
    return -amount


def accountable_balance(organization, employee, through=None):
    transactions = AccountableTransaction.objects.filter(
        organization=organization,
        employee=employee,
        is_voided=False,
        status=AccountableTransaction.STATUS_APPROVED,
    )
    expenses = Expense.objects.filter(
        organization=organization,
        employee=employee,
        source=Expense.SOURCE_ACCOUNTABLE,
    )
    if through:
        transactions = transactions.filter(occurred_on__lte=through)
        expenses = expenses.filter(spent_on__lte=through)

    funds = Decimal("0.00")
    issued = Decimal("0.00")
    returned = Decimal("0.00")
    for transaction in transactions.only("transaction_type", "amount"):
        funds += transaction_effect(transaction.transaction_type, transaction.amount)
        if transaction.transaction_type == AccountableTransaction.TYPE_ISSUE:
            issued += transaction.amount
        elif transaction.transaction_type == AccountableTransaction.TYPE_CLIENT_PAYMENT:
            issued += transaction.amount
        elif transaction.transaction_type == AccountableTransaction.TYPE_RETURN:
            returned += transaction.amount

    approved = Decimal("0.00")
    pending = Decimal("0.00")
    for expense in expenses.only("status", "amount"):
        if expense.status == Expense.STATUS_APPROVED:
            approved += expense.amount
        elif expense.status == Expense.STATUS_PENDING:
            pending += expense.amount

    return {
        "issued": issued,
        "returned": returned,
        "approved": approved,
        "pending": pending,
        "confirmed_balance": funds - approved,
        "operational_balance": funds - approved - pending,
    }


def accountable_rows(organization, through=None, users=None):
    employees = users if users is not None else finance_staff(organization)
    rows = []
    for employee in employees:
        balance = accountable_balance(organization, employee, through=through)
        if not any(balance.values()) and users is None:
            continue
        rows.append({"employee": employee, **balance})
    return rows


def cash_operation_effect(operation_type, amount):
    if operation_type in {
        CashOperation.TYPE_MANAGER_INCOME,
        CashOperation.TYPE_ACCOUNTABLE_RETURN,
        CashOperation.TYPE_CASH_COUNT_INCOME,
    }:
        return amount
    if operation_type in {
        CashOperation.TYPE_TRANSFER_TO_COMPANY,
        CashOperation.TYPE_ACCOUNTABLE_ISSUE,
        CashOperation.TYPE_CASH_COUNT_WRITE_OFF,
    }:
        return -amount
    return Decimal("0.00")


def _cash_balance_from_operations(operations):
    balance = Decimal("0.00")
    pending_income = Decimal("0.00")
    pending_transfer = Decimal("0.00")
    pending_accountable_issue = Decimal("0.00")
    pending_accountable_return = Decimal("0.00")
    approved_income = Decimal("0.00")
    approved_transfer = Decimal("0.00")
    approved_accountable_issue = Decimal("0.00")
    approved_accountable_return = Decimal("0.00")
    for operation in operations.only("operation_type", "amount", "status"):
        if operation.status == CashOperation.STATUS_APPROVED:
            balance += cash_operation_effect(operation.operation_type, operation.amount)
            if operation.operation_type == CashOperation.TYPE_MANAGER_INCOME:
                approved_income += operation.amount
            elif operation.operation_type == CashOperation.TYPE_TRANSFER_TO_COMPANY:
                approved_transfer += operation.amount
            elif operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_ISSUE:
                approved_accountable_issue += operation.amount
            elif operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN:
                approved_accountable_return += operation.amount
            elif operation.operation_type == CashOperation.TYPE_CASH_COUNT_INCOME:
                approved_income += operation.amount
            elif operation.operation_type == CashOperation.TYPE_CASH_COUNT_WRITE_OFF:
                approved_transfer += operation.amount
        elif operation.status == CashOperation.STATUS_PENDING:
            if operation.operation_type == CashOperation.TYPE_MANAGER_INCOME:
                balance += operation.amount
                approved_income += operation.amount
                pending_income += operation.amount
            elif operation.operation_type == CashOperation.TYPE_TRANSFER_TO_COMPANY:
                pending_transfer += operation.amount
            elif operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_ISSUE:
                pending_accountable_issue += operation.amount
            elif operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN:
                pending_accountable_return += operation.amount

    return {
        "balance": balance,
        "approved_income": approved_income,
        "approved_transfer": approved_transfer,
        "approved_accountable_issue": approved_accountable_issue,
        "approved_accountable_return": approved_accountable_return,
        "pending_income": pending_income,
        "pending_transfer": pending_transfer,
        "pending_accountable_issue": pending_accountable_issue,
        "pending_accountable_return": pending_accountable_return,
        "reserved_total": pending_transfer + pending_accountable_issue,
        "available_balance": balance - pending_transfer - pending_accountable_issue,
        "pending_total": pending_income + pending_transfer + pending_accountable_issue + pending_accountable_return,
    }


def kkm_cash_balance(organization, through=None):
    operations = CashOperation.objects.filter(organization=organization)
    expenses = Expense.objects.filter(
        organization=organization,
        source=Expense.SOURCE_KKM_CASH,
    )
    if through:
        operations = operations.filter(occurred_on__lte=through)
        expenses = expenses.filter(spent_on__lte=through)
    balance = _cash_balance_from_operations(operations)
    approved_expenses = sum(
        (item.amount for item in expenses.filter(status=Expense.STATUS_APPROVED).only("amount")),
        Decimal("0.00"),
    )
    pending_expenses = sum(
        (item.amount for item in expenses.filter(status=Expense.STATUS_PENDING).only("amount")),
        Decimal("0.00"),
    )
    balance["balance"] -= approved_expenses
    balance["approved_transfer"] += approved_expenses
    balance["pending_total"] += pending_expenses
    balance["reserved_total"] += pending_expenses
    balance["available_balance"] -= approved_expenses + pending_expenses
    balance["approved_kkm_expense"] = approved_expenses
    balance["pending_kkm_expense"] = pending_expenses
    return balance


def manager_cash_balance(organization, manager, through=None):
    operations = CashOperation.objects.filter(
        organization=organization,
        manager=manager,
    )
    if through:
        operations = operations.filter(occurred_on__lte=through)
    return _cash_balance_from_operations(operations)


def manager_cash_rows(organization, through=None, users=None):
    managers = users if users is not None else manager_staff(organization)
    rows = []
    for manager in managers:
        balance = manager_cash_balance(organization, manager, through=through)
        if not any(balance.values()) and users is None:
            continue
        rows.append({"manager": manager, **balance})
    return rows


def company_cash_balance(organization, through=None):
    transfers = CashOperation.objects.filter(
        organization=organization,
        operation_type=CashOperation.TYPE_TRANSFER_TO_COMPANY,
        status=CashOperation.STATUS_APPROVED,
    )
    expenses = Expense.objects.filter(
        organization=organization,
        source=Expense.SOURCE_COMPANY_CASH,
        status=Expense.STATUS_APPROVED,
    )
    pending_transfers = CashOperation.objects.filter(
        organization=organization,
        operation_type=CashOperation.TYPE_TRANSFER_TO_COMPANY,
        status=CashOperation.STATUS_PENDING,
    )
    if through:
        transfers = transfers.filter(occurred_on__lte=through)
        expenses = expenses.filter(spent_on__lte=through)
        pending_transfers = pending_transfers.filter(occurred_on__lte=through)

    income = sum((item.amount for item in transfers.only("amount")), Decimal("0.00"))
    spent = sum((item.amount for item in expenses.only("amount")), Decimal("0.00"))
    pending = sum((item.amount for item in pending_transfers.only("amount")), Decimal("0.00"))
    return {
        "income": income,
        "spent": spent,
        "pending_transfer": pending,
        "balance": income - spent,
    }


def report_employee_rows(organization, start, end):
    rows = []
    for employee in finance_staff(organization):
        opening_day = date.fromordinal(start.toordinal() - 1)
        opening = accountable_balance(organization, employee, through=opening_day)
        closing = accountable_balance(organization, employee, through=end)
        transactions = AccountableTransaction.objects.filter(
            organization=organization,
            employee=employee,
            occurred_on__range=(start, end),
            is_voided=False,
            status=AccountableTransaction.STATUS_APPROVED,
        )
        expenses = Expense.objects.filter(
            organization=organization,
            employee=employee,
            source=Expense.SOURCE_ACCOUNTABLE,
            spent_on__range=(start, end),
        )
        issued = sum(
            (item.amount for item in transactions if item.transaction_type == AccountableTransaction.TYPE_ISSUE),
            Decimal("0.00"),
        )
        client_payments = sum(
            (item.amount for item in transactions if item.transaction_type == AccountableTransaction.TYPE_CLIENT_PAYMENT),
            Decimal("0.00"),
        )
        returned = sum(
            (item.amount for item in transactions if item.transaction_type == AccountableTransaction.TYPE_RETURN),
            Decimal("0.00"),
        )
        approved = sum(
            (item.amount for item in expenses if item.status == Expense.STATUS_APPROVED),
            Decimal("0.00"),
        )
        pending = sum(
            (item.amount for item in expenses if item.status == Expense.STATUS_PENDING),
            Decimal("0.00"),
        )
        if not any(
            [
                opening["operational_balance"],
                issued,
                client_payments,
                returned,
                approved,
                pending,
                closing["operational_balance"],
            ]
        ):
            continue
        rows.append(
            {
                "employee": employee,
                "opening": opening["operational_balance"],
                "issued": issued + client_payments,
                "returned": returned,
                "approved": approved,
                "pending": pending,
                "closing": closing["operational_balance"],
            }
        )
    return rows


def report_expenses(organization, start, end, filters=None):
    filters = filters or {}
    queryset = (
        Expense.objects.filter(
            organization=organization,
            spent_on__range=(start, end),
        )
        .select_related("employee", "category", "client", "pool", "reviewed_by", "posted_to_1c_by")
        .prefetch_related("receipts")
    )
    if filters.get("employee"):
        queryset = queryset.filter(employee_id=filters["employee"])
    if filters.get("category"):
        queryset = queryset.filter(category_id=filters["category"])
    if filters.get("client"):
        queryset = queryset.filter(client_id=filters["client"])
    if filters.get("destination"):
        queryset = queryset.filter(destination_type=filters["destination"])
    if filters.get("source"):
        queryset = queryset.filter(source=filters["source"])
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    search = (filters.get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(destination_name__icontains=search)
            | Q(vendor__icontains=search)
            | Q(description__icontains=search)
        )
    return queryset.order_by("-spent_on", "-id")


def notify_advance(transaction):
    if transaction.employee_id == transaction.created_by_id:
        return []
    title = "Выдан подотчёт" if transaction.transaction_type == AccountableTransaction.TYPE_ISSUE else transaction.get_transaction_type_display()
    message = f"{transaction.get_transaction_type_display()}: {format_money(transaction.amount)}"
    return notify_users(
        [transaction.employee],
        title=title,
        message=message,
        kind="finance",
        action_url=reverse("finance_dashboard"),
        organization=transaction.organization,
    )


def finance_reviewers(organization, exclude_user=None):
    queryset = User.objects.filter(
        organizationaccess__organization=organization,
        organizationaccess__role__in=FINANCE_MANAGE_ROLES,
        is_active=True,
    ).distinct()
    if exclude_user:
        queryset = queryset.exclude(id=exclude_user.id)
    return queryset


def notify_expense_submitted(expense):
    return notify_users(
        finance_reviewers(expense.organization, exclude_user=expense.created_by),
        title="Новый расход на проверке",
        message=f"{user_display_name(expense.employee)}: {format_money(expense.amount)} — {expense.destination_name}",
        kind="finance",
        action_url=reverse("finance_expense_detail", kwargs={"expense_uuid": expense.uuid}),
        organization=expense.organization,
        client=expense.client,
    )


def notify_expense_reviewed(expense):
    if expense.status == Expense.STATUS_APPROVED:
        title = "Расход подтверждён"
        message = f"Расход {format_money(expense.amount)} подтверждён."
        level = "info"
    else:
        title = "Расход отклонён"
        message = expense.review_comment or f"Расход {format_money(expense.amount)} отклонён."
        level = "warning"
    recipients = {expense.employee, expense.created_by}
    if expense.reviewed_by in recipients:
        recipients.remove(expense.reviewed_by)
    return notify_users(
        recipients,
        title=title,
        message=message,
        kind="finance",
        level=level,
        action_url=reverse("finance_expense_detail", kwargs={"expense_uuid": expense.uuid}),
        organization=expense.organization,
        client=expense.client,
    )
