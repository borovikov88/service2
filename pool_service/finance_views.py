from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import logging

import tablib
from PIL import Image, ImageOps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Sum
from django.core.paginator import Paginator
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from pool_service.finance_forms import (
    AccountableTransactionForm,
    AccountableReturnRequestForm,
    CASH_DENOMINATIONS,
    CardTransferPaymentForm,
    CashCountForm,
    ClientPaymentForm,
    ExpenseForm,
    ExpenseReviewForm,
    ManagerCashAccountableIssueForm,
    ManagerCashIncomeForm,
    ManagerCashTransferForm,
    MonthlyProfitUploadForm,
)
from pool_service.models import (
    AccountableTransaction,
    AccountableTransactionChange,
    CardTransferAttachment,
    CardTransferPayment,
    CardTransferPaymentChange,
    CashCount,
    CashOperation,
    CashOperationChange,
    Client,
    Expense,
    ExpenseCategory,
    ExpenseChange,
    ExpensePeriod,
    ExpenseReceipt,
    OneCImportBatch,
    OneCMonthlyProfit,
)
from pool_service.finance_imports.services import (
    DuplicateImportError,
    calculate_profitability,
    cancel_monthly_profit,
    confirm_monthly_profit,
    create_monthly_profit_preview,
)
from pool_service.services.finance import (
    accountable_balance,
    accountable_rows,
    can_access_cash,
    can_access_finance,
    can_close_finance_period,
    can_confirm_accountable_issue,
    can_edit_expense,
    can_issue_accountable_transaction,
    can_manage_cash,
    can_manage_finance,
    can_review_card_transfer_payment,
    can_review_accountable_transaction,
    can_review_expense,
    can_view_card_transfer_payment,
    can_view_expense,
    ensure_default_categories,
    finance_staff,
    find_client_by_name,
    company_cash_balance,
    kkm_cash_balance,
    month_bounds,
    notify_advance,
    notify_expense_reviewed,
    notify_expense_submitted,
    organization_roles,
    period_is_closed,
    report_employee_rows,
    report_expenses,
    user_display_name,
)
from pool_service.services.permissions import is_org_access_blocked, organization_for_user

logger = logging.getLogger(__name__)


def _organization_for_finance(request):
    return organization_for_user(request.user)


def _onec_import_guard(request):
    return _finance_guard(request, manage=True)


def _finance_guard(request, *, manage=False, close=False, issue=False):
    organization = _organization_for_finance(request)
    if not organization:
        return None, HttpResponseForbidden("Организация не найдена.")
    allowed = can_access_finance(request.user, organization)
    if manage:
        allowed = can_manage_finance(request.user, organization)
    if issue:
        allowed = can_issue_accountable_transaction(request.user, organization)
    if close:
        allowed = can_close_finance_period(request.user, organization)
    if not allowed:
        return organization, HttpResponseForbidden("Недостаточно прав.")
    if is_org_access_blocked(request.user):
        messages.error(request, "Доступ организации к сервису приостановлен.")
        return organization, redirect("billing")
    return organization, None


def _cash_guard(request, *, manage=False):
    organization = _organization_for_finance(request)
    if not organization:
        return None, HttpResponseForbidden("Организация не найдена.")
    allowed = can_manage_cash(request.user, organization) if manage else can_access_cash(request.user, organization)
    if not allowed:
        return organization, HttpResponseForbidden("Недостаточно прав.")
    if is_org_access_blocked(request.user):
        messages.error(request, "Доступ организации к сервису приостановлен.")
        return organization, redirect("billing")
    return organization, None


def _is_offline_request(request):
    return request.headers.get("X-Finance-Offline") == "1"


def _finance_modal_context(request):
    is_modal = request.GET.get("modal") == "1"
    return {
        "finance_modal": is_modal,
        "hide_header": is_modal,
        "hide_bottom_nav": is_modal,
    }


def _post_success(request, expense, message):
    if _is_offline_request(request):
        return JsonResponse(
            {
                "ok": True,
                "expense_uuid": str(expense.uuid),
                "detail_url": reverse("finance_expense_detail", kwargs={"expense_uuid": expense.uuid}),
            }
        )
    messages.success(request, message)
    return redirect("finance_expense_detail", expense_uuid=expense.uuid)


def _prepare_receipt(uploaded_file):
    content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
    if not content_type.startswith("image/"):
        uploaded_file.seek(0)
        return uploaded_file, content_type
    uploaded_file.seek(0)
    image = Image.open(uploaded_file)
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=84, optimize=True, progressive=True)
    stem = Path(uploaded_file.name).stem[:180] or "receipt"
    return ContentFile(buffer.getvalue(), name=f"{stem}.jpg"), "image/jpeg"


def _save_receipts(expense, files, user):
    for uploaded_file in files:
        original_name = Path(uploaded_file.name).name[:255]
        stored_file, content_type = _prepare_receipt(uploaded_file)
        ExpenseReceipt.objects.create(
            expense=expense,
            file=stored_file,
            original_name=original_name,
            content_type=content_type,
            size=stored_file.size,
            uploaded_by=user,
        )


def _save_card_transfer_attachments(payment, files, user):
    for uploaded_file in files:
        original_name = Path(uploaded_file.name).name[:255]
        stored_file, content_type = _prepare_receipt(uploaded_file)
        CardTransferAttachment.objects.create(
            payment=payment,
            file=stored_file,
            original_name=original_name,
            content_type=content_type,
            size=stored_file.size,
            uploaded_by=user,
        )


def _expense_for_user(request, expense_uuid):
    expense = get_object_or_404(
        Expense.objects.select_related(
            "organization",
            "employee",
            "created_by",
            "reviewed_by",
            "category",
            "client",
            "pool",
        ).prefetch_related("receipts", "changes__actor"),
        uuid=expense_uuid,
    )
    if not can_view_expense(request.user, expense):
        return None
    return expense


def _client_options(organization):
    options = []
    clients = Client.objects.filter(organization=organization).order_by("name")
    for client in clients:
        details = client.phone or client.inn or ""
        label = f"{client.name} — {details}" if details else client.name
        options.append({"id": client.id, "name": client.name, "label": label})
    return options


def _resolve_income_client(organization, form):
    client = form.resolved_client
    if form.new_client_name:
        client = find_client_by_name(organization, form.new_client_name)
        if not client:
            client = Client.objects.create(
                organization=organization,
                name=form.new_client_name,
                client_type="private",
            )
    return client


def _income_payload(form, client):
    return {
        "employee_id": form.cleaned_data["employee"].id,
        "client_id": client.id if client else None,
        "amount": str(form.cleaned_data["amount"]),
        "occurred_on": form.cleaned_data["occurred_on"].isoformat(),
        "note": (form.cleaned_data.get("note") or "").strip(),
    }


def _apply_income_payload(movement, payload):
    movement.employee_id = payload["employee_id"]
    movement.client_id = payload.get("client_id")
    movement.amount = Decimal(payload["amount"])
    movement.occurred_on = date.fromisoformat(payload["occurred_on"])
    movement.note = payload.get("note", "")


def _clear_pending_movement(movement):
    movement.pending_action = ""
    movement.pending_payload = {}
    movement.pending_requested_by = None
    movement.pending_requested_at = None


def _movement_for_user(request, transaction_id):
    movement = get_object_or_404(
        AccountableTransaction.objects.select_related(
            "organization",
            "employee",
            "client",
            "created_by",
            "pending_requested_by",
        ),
        id=transaction_id,
    )
    if can_manage_finance(request.user, movement.organization):
        return movement
    if can_access_finance(request.user, movement.organization) and request.user.id in {
        movement.employee_id,
        movement.created_by_id,
    }:
        return movement
    return None


def _can_request_income_change(user, movement):
    return (
        movement
        and movement.transaction_type == AccountableTransaction.TYPE_CLIENT_PAYMENT
        and movement.status == AccountableTransaction.STATUS_APPROVED
        and not movement.is_voided
        and not movement.pending_action
        and can_access_finance(user, movement.organization)
        and user.id in {movement.employee_id, movement.created_by_id}
        and not period_is_closed(movement.organization, movement.occurred_on)
    )


def _decorate_accountable_movements(user, movements):
    for movement in movements:
        movement.can_request_change = _can_request_income_change(user, movement)
        movement.can_confirm_issue = can_confirm_accountable_issue(user, movement)
    return movements


def _cash_reviewers_can_review(user, operation):
    if not can_manage_cash(user, operation.organization):
        return False
    return user.id not in {operation.manager_id, operation.created_by_id}


def _can_view_cash_operation(user, operation):
    return can_access_cash(user, operation.organization)


def _can_edit_cash_operation(user, operation):
    if (
        not operation
        or operation.status != CashOperation.STATUS_PENDING
        or period_is_closed(operation.organization, operation.occurred_on)
    ):
        return False
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_ISSUE:
        if not operation.accountable_transaction_id or operation.accountable_transaction.status != AccountableTransaction.STATUS_PENDING:
            return False
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN:
        if not operation.accountable_transaction_id or operation.accountable_transaction.status != AccountableTransaction.STATUS_PENDING:
            return False
    return can_access_cash(user, operation.organization) and user.id in {operation.manager_id, operation.created_by_id}


def _can_delete_cash_operation(user, operation):
    if not operation:
        return False
    return user.is_superuser or "admin" in organization_roles(user, operation.organization)


def _cash_operation_for_user(request, operation_id):
    operation = get_object_or_404(
        CashOperation.objects.select_related(
            "organization",
            "manager",
            "receiver",
            "created_by",
            "reviewed_by",
            "accountable_transaction__employee",
            "accountable_transaction__created_by",
        ).prefetch_related("changes__actor"),
        id=operation_id,
    )
    if not _can_view_cash_operation(request.user, operation):
        return None
    return operation


def _create_cash_operation_change(operation, actor, action, note="", payload=None):
    CashOperationChange.objects.create(
        operation=operation,
        actor=actor,
        action=action,
        note=note,
        payload=payload or {},
    )


def _cash_operation_form(operation, data=None):
    if operation.operation_type == CashOperation.TYPE_MANAGER_INCOME:
        return ManagerCashIncomeForm(data, organization=operation.organization, instance=operation)
    if operation.operation_type == CashOperation.TYPE_TRANSFER_TO_COMPANY:
        return ManagerCashTransferForm(data, organization=operation.organization, instance=operation)
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_ISSUE:
        return ManagerCashAccountableIssueForm(
            data,
            organization=operation.organization,
            instance=operation.accountable_transaction,
        )
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN:
        return AccountableReturnRequestForm(
            data,
            organization=operation.organization,
            instance=operation.accountable_transaction,
            initial={"manager": operation.manager_id},
        )
    raise ValueError("Unknown cash operation type")


def _render_cash_dashboard(request, section):
    organization, denied = _cash_guard(request, manage=(section == "company"))
    if denied:
        return denied
    manage = can_manage_cash(request.user, organization)
    roles = organization_roles(request.user, organization)
    if section not in {"company", "kkm"}:
        return redirect("finance_kkm_cash_dashboard" if can_access_cash(request.user, organization) else "finance_dashboard")

    operations = []
    manager_rows = []
    company_balance = None
    kkm_balance = None
    company_counts = []
    kkm_counts = []
    if section == "company":
        company_balance = company_cash_balance(organization)
        company_counts = (
            CashCount.objects.filter(organization=organization, cashbox_type=CashCount.CASHBOX_COMPANY)
            .select_related("counted_by")
            .order_by("-occurred_on", "-id")[:20]
        )
    else:
        can_delete_cash_operations = request.user.is_superuser or "admin" in roles
        kkm_balance = kkm_cash_balance(organization)
        operations = list(
            CashOperation.objects.filter(organization=organization)
            .select_related(
                "manager",
                "receiver",
                "created_by",
                "reviewed_by",
                "accountable_transaction__employee",
            )[:100]
        )
        for operation in operations:
            operation.can_edit = _can_edit_cash_operation(request.user, operation)
            operation.can_delete = can_delete_cash_operations
        kkm_counts = (
            CashCount.objects.filter(organization=organization, cashbox_type=CashCount.CASHBOX_KKM)
            .select_related("counted_by")
            .order_by("-occurred_on", "-id")[:20]
        )

    return render(
        request,
        "pool_service/finance/cash_dashboard.html",
        {
            "organization": organization,
            "cashbox_section": section,
            "can_manage_cash": manage,
            "can_count_kkm": manage or "manager" in roles,
            "can_create_manager_cash": "manager" in roles,
            "can_return_accountable": can_access_cash(request.user, organization),
            "can_delete_cash_operations": request.user.is_superuser or "admin" in roles,
            "company_balance": company_balance,
            "kkm_balance": kkm_balance,
            "manager_rows": manager_rows,
            "operations": operations,
            "company_counts": company_counts,
            "kkm_counts": kkm_counts,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
@xframe_options_sameorigin
def finance_dashboard(request):
    organization, denied = _finance_guard(request)
    if denied:
        return denied
    ensure_default_categories(organization)
    manage = can_manage_finance(request.user, organization)
    can_issue_accountable = can_issue_accountable_transaction(request.user, organization)
    today = date.today()
    month_start, month_end = month_bounds(today)
    expenses = (
        Expense.objects.filter(organization=organization)
        .select_related("employee", "category", "client", "reviewed_by")
        .prefetch_related("receipts")
    )
    transactions = AccountableTransaction.objects.filter(
        organization=organization,
        is_voided=False,
    ).select_related("employee", "created_by", "client", "pending_requested_by", "reviewed_by")
    if manage:
        balance_rows = accountable_rows(organization)
    else:
        expenses = expenses.filter(employee=request.user)
        transactions = transactions.filter(employee=request.user)
        balance_rows = accountable_rows(organization, users=[request.user])
    month_expenses = expenses.filter(spent_on__range=(month_start, month_end))
    approved_total = sum(
        (item.amount for item in month_expenses if item.status == Expense.STATUS_APPROVED),
        Decimal("0.00"),
    )
    pending_total = sum(
        (item.amount for item in month_expenses if item.status == Expense.STATUS_PENDING),
        Decimal("0.00"),
    )
    company_cash_total = sum(
        (
            item.amount
            for item in month_expenses
            if item.status == Expense.STATUS_APPROVED and item.source == Expense.SOURCE_COMPANY_CASH
        ),
        Decimal("0.00"),
    )
    latest_transactions = _decorate_accountable_movements(request.user, list(transactions[:15]))
    return render(
        request,
        "pool_service/finance/dashboard.html",
        {
            "organization": organization,
            "can_manage_finance": manage,
            "can_issue_accountable": can_issue_accountable,
            "can_close_finance": can_close_finance_period(request.user, organization),
            "balance_rows": balance_rows,
            "latest_expenses": expenses[:20],
            "latest_transactions": latest_transactions,
            "approved_total": approved_total,
            "pending_total": pending_total,
            "company_cash_total": company_cash_total,
            "month_label": today.strftime("%m.%Y"),
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
def finance_cash_dashboard(request):
    return redirect("finance_kkm_cash_dashboard")


@login_required
def finance_company_cash_dashboard(request):
    return _render_cash_dashboard(request, "company")


@login_required
def finance_kkm_cash_dashboard(request):
    return _render_cash_dashboard(request, "kkm")


@login_required
def finance_card_transfer_dashboard(request):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    can_review = can_manage_finance(request.user, organization)
    payments = (
        CardTransferPayment.objects.filter(organization=organization)
        .select_related("client", "created_by", "reviewed_by")
        .prefetch_related("attachments")
    )
    client_id = (request.GET.get("client") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()
    selected_client_id = ""
    if client_id.isdigit():
        payments = payments.filter(client_id=int(client_id))
        selected_client_id = client_id
    parsed_date_from = None
    parsed_date_to = None
    if date_from:
        try:
            parsed_date_from = date.fromisoformat(date_from)
            payments = payments.filter(paid_on__gte=parsed_date_from)
        except ValueError:
            messages.error(request, "Некорректная дата начала.")
    if date_to:
        try:
            parsed_date_to = date.fromisoformat(date_to)
            payments = payments.filter(paid_on__lte=parsed_date_to)
        except ValueError:
            messages.error(request, "Некорректная дата окончания.")
    payments = list(payments[:200])
    for payment in payments:
        payment.can_review = can_review_card_transfer_payment(request.user, payment) and payment.status == CardTransferPayment.STATUS_PENDING
    return render(
        request,
        "pool_service/finance/card_transfer_dashboard.html",
        {
            "payments": payments,
            "clients": Client.objects.filter(organization=organization).order_by("name"),
            "selected_client_id": selected_client_id,
            "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
            "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
            "can_review_transfers": can_review,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
@xframe_options_sameorigin
def finance_card_transfer_create(request):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    form = CardTransferPaymentForm(
        request.POST or None,
        request.FILES or None,
        organization=organization,
        initial={"paid_on": date.today()},
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            client = form.resolved_client
            if form.new_client_name:
                client = find_client_by_name(organization, form.new_client_name)
                if not client:
                    client = Client.objects.create(
                        organization=organization,
                        name=form.new_client_name,
                        client_type="private",
                    )
            payment = form.save(commit=False)
            payment.organization = organization
            payment.client = client
            payment.created_by = request.user
            payment.status = CardTransferPayment.STATUS_PENDING
            payment.receipt_missing_confirmed = bool(form.cleaned_data.get("receipt_missing_confirmed")) and not bool(
                form.cleaned_data["attachments"]
            )
            payment.full_clean()
            payment.save()
            if form.cleaned_data["attachments"]:
                _save_card_transfer_attachments(payment, form.cleaned_data["attachments"], request.user)
            CardTransferPaymentChange.objects.create(
                payment=payment,
                actor=request.user,
                action=CardTransferPaymentChange.ACTION_CREATED,
            )
        messages.success(request, "Оплата переводом добавлена и отправлена на подтверждение.")
        if request.GET.get("modal") == "1":
            return HttpResponse(
                "<script>window.parent.postMessage({type:'finance-modal-close',reload:true}, window.location.origin);</script>"
            )
        return redirect("finance_card_transfer_dashboard")
    context = {
        "form": form,
        "client_options": _client_options(organization),
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/card_transfer_form.html", context)


@login_required
def finance_card_transfer_detail(request, payment_id):
    payment = get_object_or_404(
        CardTransferPayment.objects.select_related(
            "organization",
            "client",
            "created_by",
            "reviewed_by",
        ).prefetch_related("attachments", "changes__actor"),
        id=payment_id,
    )
    if not can_view_card_transfer_payment(request.user, payment):
        return HttpResponseForbidden("Недостаточно прав.")
    return render(
        request,
        "pool_service/finance/card_transfer_detail.html",
        {
            "payment": payment,
            "can_review": can_review_card_transfer_payment(request.user, payment)
            and payment.status == CardTransferPayment.STATUS_PENDING,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@require_POST
@login_required
def finance_card_transfer_review(request, payment_id):
    payment = get_object_or_404(
        CardTransferPayment.objects.select_related("organization", "created_by", "reviewed_by"),
        id=payment_id,
    )
    if not can_review_card_transfer_payment(request.user, payment):
        return HttpResponseForbidden("Недостаточно прав.")
    if payment.status != CardTransferPayment.STATUS_PENDING:
        messages.error(request, "Эта оплата уже рассмотрена.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_card_transfer_dashboard"))
    if period_is_closed(payment.organization, payment.paid_on):
        messages.error(request, "Месяц закрыт.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_card_transfer_dashboard"))
    decision = (request.POST.get("decision") or "").strip()
    if decision not in {CardTransferPayment.STATUS_APPROVED, CardTransferPayment.STATUS_REJECTED}:
        messages.error(request, "Выберите решение.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_card_transfer_dashboard"))
    review_comment = (request.POST.get("review_comment") or "").strip()
    with transaction.atomic():
        payment.status = decision
        payment.reviewed_by = request.user
        payment.reviewed_at = timezone.now()
        payment.review_comment = review_comment
        payment.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment", "updated_at"])
        CardTransferPaymentChange.objects.create(
            payment=payment,
            actor=request.user,
            action=(
                CardTransferPaymentChange.ACTION_APPROVED
                if decision == CardTransferPayment.STATUS_APPROVED
                else CardTransferPaymentChange.ACTION_REJECTED
            ),
            note=review_comment,
        )
    messages.success(request, "Решение по перечислению сохранено.")
    return redirect(request.META.get("HTTP_REFERER") or reverse("finance_card_transfer_dashboard"))


@login_required
def finance_card_transfer_attachment_download(request, attachment_id):
    attachment = get_object_or_404(
        CardTransferAttachment.objects.select_related(
            "payment__organization",
            "payment__client",
            "payment__created_by",
        ),
        id=attachment_id,
    )
    if not can_view_card_transfer_payment(request.user, attachment.payment):
        return HttpResponseForbidden("Недостаточно прав.")
    response = FileResponse(
        attachment.file.open("rb"),
        content_type=attachment.content_type or "application/octet-stream",
        as_attachment=False,
        filename=attachment.original_name,
    )
    response["Cache-Control"] = "private, no-store"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


@login_required
@xframe_options_sameorigin
def finance_cash_income_create(request):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    if "manager" not in organization_roles(request.user, organization):
        return HttpResponseForbidden("Поступление в ККМ может добавить только менеджер.")
    form = ManagerCashIncomeForm(
        request.POST or None,
        organization=organization,
        initial={"occurred_on": date.today(), "amount": request.GET.get("amount") or None},
    )
    if request.method == "POST" and form.is_valid():
        operation = form.save(commit=False)
        operation.organization = organization
        operation.manager = request.user
        operation.created_by = request.user
        operation.operation_type = CashOperation.TYPE_MANAGER_INCOME
        operation.status = CashOperation.STATUS_APPROVED
        operation.reviewed_by = request.user
        operation.reviewed_at = timezone.now()
        operation.full_clean()
        operation.save()
        _create_cash_operation_change(
            operation,
            request.user,
            CashOperationChange.ACTION_CREATED,
            payload={
                "operation_type": operation.operation_type,
                "amount": str(operation.amount),
                "occurred_on": operation.occurred_on.isoformat(),
                "note": operation.note,
            },
        )
        _create_cash_operation_change(
            operation,
            request.user,
            CashOperationChange.ACTION_APPROVED,
            payload={"decision": CashOperation.STATUS_APPROVED},
        )
        messages.success(request, "Поступление в кассу ККМ сохранено.")
        return redirect("finance_kkm_cash_dashboard")
    context = {
        "form": form,
        "title": "Поступление в кассу ККМ",
        "subtitle": "Деньги, которые получил менеджер",
        "submit_label": "Сохранить поступление",
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/cash_form.html", context)


@login_required
@xframe_options_sameorigin
def finance_cash_transfer_create(request):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    if "manager" not in organization_roles(request.user, organization):
        return HttpResponseForbidden("Сдать выручку может только менеджер.")
    form = ManagerCashTransferForm(
        request.POST or None,
        organization=organization,
        initial={"occurred_on": date.today(), "amount": request.GET.get("amount") or None},
    )
    if request.method == "POST" and form.is_valid():
        operation = form.save(commit=False)
        operation.organization = organization
        operation.manager = request.user
        operation.created_by = request.user
        operation.operation_type = CashOperation.TYPE_TRANSFER_TO_COMPANY
        operation.status = CashOperation.STATUS_PENDING
        operation.full_clean()
        operation.save()
        _create_cash_operation_change(
            operation,
            request.user,
            CashOperationChange.ACTION_CREATED,
            payload={
                "receiver_id": operation.receiver_id,
                "operation_type": operation.operation_type,
                "amount": str(operation.amount),
                "occurred_on": operation.occurred_on.isoformat(),
                "note": operation.note,
            },
        )
        messages.success(request, "Сдача выручки отправлена на подтверждение.")
        return redirect("finance_kkm_cash_dashboard")
    context = {
        "form": form,
        "title": "Сдать выручку",
        "subtitle": "После подтверждения деньги перейдут из ККМ в кассу компании",
        "submit_label": "Отправить на подтверждение",
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/cash_form.html", context)


@login_required
@xframe_options_sameorigin
def finance_cash_accountable_issue_create(request):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    if "manager" not in organization_roles(request.user, organization):
        return HttpResponseForbidden("Выдать подотчёт из ККМ может только менеджер.")
    balance = kkm_cash_balance(organization)
    form = ManagerCashAccountableIssueForm(
        request.POST or None,
        organization=organization,
        initial={"occurred_on": date.today(), "amount": request.GET.get("amount") or None},
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            movement = form.save(commit=False)
            movement.organization = organization
            movement.created_by = request.user
            movement.transaction_type = AccountableTransaction.TYPE_ISSUE
            requires_employee_confirmation = movement.employee_id != request.user.id
            movement.status = (
                AccountableTransaction.STATUS_PENDING
                if requires_employee_confirmation
                else AccountableTransaction.STATUS_APPROVED
            )
            if not requires_employee_confirmation:
                movement.reviewed_by = request.user
                movement.reviewed_at = timezone.now()
            movement.full_clean()
            movement.save()
            AccountableTransactionChange.objects.create(
                transaction=movement,
                actor=request.user,
                action=AccountableTransactionChange.ACTION_CREATED,
                payload={
                    "source": "manager_cashbox",
                    "employee_id": movement.employee_id,
                    "amount": str(movement.amount),
                    "occurred_on": movement.occurred_on.isoformat(),
                    "note": movement.note,
                    "status": movement.status,
                },
            )
            operation = CashOperation.objects.create(
                organization=organization,
                manager=request.user,
                created_by=request.user,
                operation_type=CashOperation.TYPE_ACCOUNTABLE_ISSUE,
                accountable_transaction=movement,
                amount=movement.amount,
                occurred_on=movement.occurred_on,
                note=movement.note,
                status=(
                    CashOperation.STATUS_PENDING
                    if requires_employee_confirmation
                    else CashOperation.STATUS_APPROVED
                ),
                reviewed_by=None if requires_employee_confirmation else request.user,
                reviewed_at=None if requires_employee_confirmation else timezone.now(),
            )
            _create_cash_operation_change(
                operation,
                request.user,
                CashOperationChange.ACTION_CREATED,
                payload={
                    "accountable_transaction_id": movement.id,
                    "employee_id": movement.employee_id,
                    "operation_type": operation.operation_type,
                    "amount": str(operation.amount),
                    "occurred_on": operation.occurred_on.isoformat(),
                    "note": operation.note,
                },
            )
            if not requires_employee_confirmation:
                AccountableTransactionChange.objects.create(
                    transaction=movement,
                    actor=request.user,
                    action=AccountableTransactionChange.ACTION_UPDATED,
                    note="Автоматически подтверждено при выдаче самому себе",
                    payload={"decision": AccountableTransaction.STATUS_APPROVED},
                )
                _create_cash_operation_change(
                    operation,
                    request.user,
                    CashOperationChange.ACTION_APPROVED,
                    note="Автоматически подтверждено при выдаче самому себе",
                    payload={"decision": CashOperation.STATUS_APPROVED, "accountable_transaction_id": movement.id},
                )
        if requires_employee_confirmation:
            notify_advance(movement)
            messages.success(request, "Выдача из ККМ отправлена сотруднику на подтверждение.")
        else:
            messages.success(request, "Выдача из ККМ сохранена без подтверждения.")
        return redirect("finance_kkm_cash_dashboard")
    context = {
        "form": form,
        "title": "Выдать подотчёт из ККМ",
        "subtitle": f"Текущий общий остаток ККМ: {balance['balance']}",
        "submit_label": "Отправить на подтверждение",
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/cash_form.html", context)


@login_required
@xframe_options_sameorigin
def finance_accountable_return_create(request):
    organization, denied = _finance_guard(request)
    if denied:
        return denied
    form = AccountableReturnRequestForm(
        request.POST or None,
        organization=organization,
        initial={"occurred_on": date.today()},
    )
    if request.method == "POST" and form.is_valid():
        manager = form.cleaned_data["manager"]
        with transaction.atomic():
            movement = form.save(commit=False)
            movement.organization = organization
            movement.employee = request.user
            movement.created_by = request.user
            movement.transaction_type = AccountableTransaction.TYPE_RETURN
            requires_manager_confirmation = manager.id != request.user.id
            movement.status = (
                AccountableTransaction.STATUS_PENDING
                if requires_manager_confirmation
                else AccountableTransaction.STATUS_APPROVED
            )
            if not requires_manager_confirmation:
                movement.reviewed_by = request.user
                movement.reviewed_at = timezone.now()
            movement.full_clean()
            movement.save()
            AccountableTransactionChange.objects.create(
                transaction=movement,
                actor=request.user,
                action=AccountableTransactionChange.ACTION_CREATED,
                payload={
                    "source": "manager_cashbox_return",
                    "manager_id": manager.id,
                    "amount": str(movement.amount),
                    "occurred_on": movement.occurred_on.isoformat(),
                    "note": movement.note,
                    "status": movement.status,
                },
            )
            operation = CashOperation.objects.create(
                organization=organization,
                manager=manager,
                created_by=request.user,
                operation_type=CashOperation.TYPE_ACCOUNTABLE_RETURN,
                accountable_transaction=movement,
                amount=movement.amount,
                occurred_on=movement.occurred_on,
                note=movement.note,
                status=(
                    CashOperation.STATUS_PENDING
                    if requires_manager_confirmation
                    else CashOperation.STATUS_APPROVED
                ),
                reviewed_by=None if requires_manager_confirmation else request.user,
                reviewed_at=None if requires_manager_confirmation else timezone.now(),
            )
            _create_cash_operation_change(
                operation,
                request.user,
                CashOperationChange.ACTION_CREATED,
                payload={
                    "accountable_transaction_id": movement.id,
                    "employee_id": request.user.id,
                    "manager_id": manager.id,
                    "operation_type": operation.operation_type,
                    "amount": str(operation.amount),
                    "occurred_on": operation.occurred_on.isoformat(),
                    "note": operation.note,
                },
            )
            if not requires_manager_confirmation:
                AccountableTransactionChange.objects.create(
                    transaction=movement,
                    actor=request.user,
                    action=AccountableTransactionChange.ACTION_UPDATED,
                    note="Автоматически подтверждено при возврате самому себе",
                    payload={"decision": AccountableTransaction.STATUS_APPROVED},
                )
                _create_cash_operation_change(
                    operation,
                    request.user,
                    CashOperationChange.ACTION_APPROVED,
                    note="Автоматически подтверждено при возврате самому себе",
                    payload={"decision": CashOperation.STATUS_APPROVED, "accountable_transaction_id": movement.id},
                )
        if requires_manager_confirmation:
            messages.success(request, "Возврат подотчёта отправлен менеджеру на подтверждение.")
        else:
            messages.success(request, "Возврат подотчёта сохранён без подтверждения.")
        return redirect("finance_kkm_cash_dashboard" if can_access_cash(request.user, organization) else "finance_dashboard")
    context = {
        "form": form,
        "title": "Возврат подотчёта",
        "subtitle": "Деньги попадут в кассу ККМ после подтверждения менеджером",
        "submit_label": "Отправить возврат",
        "back_url_name": "finance_kkm_cash_dashboard" if can_access_cash(request.user, organization) else "finance_dashboard",
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/cash_form.html", context)


@login_required
@xframe_options_sameorigin
def finance_cash_count_create(request, cashbox_type):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    if cashbox_type not in {CashCount.CASHBOX_COMPANY, CashCount.CASHBOX_KKM}:
        return HttpResponseForbidden("Неизвестная касса.")
    manage = can_manage_cash(request.user, organization)
    roles = organization_roles(request.user, organization)
    if cashbox_type == CashCount.CASHBOX_COMPANY and not manage:
        return HttpResponseForbidden("Пересчёт кассы организации доступен только администратору, владельцу или бухгалтеру.")
    if cashbox_type == CashCount.CASHBOX_KKM and not (manage or "manager" in roles):
        return HttpResponseForbidden("Пересчёт ККМ доступен менеджеру или финансовому администратору.")
    form = CashCountForm(
        request.POST or None,
        organization=organization,
        cashbox_type=cashbox_type,
    )
    denomination_fields = [(name, label, value, form[name]) for name, label, value in CASH_DENOMINATIONS]
    expected_balance = (
        company_cash_balance(organization)["balance"]
        if cashbox_type == CashCount.CASHBOX_COMPANY
        else kkm_cash_balance(organization)["balance"]
    )
    if request.method == "POST" and form.is_valid():
        actual_total = form.total_amount()
        difference = actual_total - expected_balance
        cash_count = form.save(commit=False)
        cash_count.organization = organization
        cash_count.cashbox_type = cashbox_type
        cash_count.manager = None
        cash_count.counted_by = request.user
        cash_count.occurred_on = date.today()
        cash_count.denominations = {
            **form.denomination_counts(),
            "expected_balance": str(expected_balance),
            "difference": str(difference),
        }
        cash_count.total = actual_total
        cash_count.full_clean()
        cash_count.save()
        messages.success(request, f"Пересчёт сохранён. Сумма: {cash_count.total}.")
        return redirect(
            "finance_company_cash_dashboard"
            if cashbox_type == CashCount.CASHBOX_COMPANY
            else "finance_kkm_cash_dashboard"
        )
    title = "Пересчёт кассы организации" if cashbox_type == CashCount.CASHBOX_COMPANY else "Пересчёт кассы ККМ"
    context = {
        "form": form,
        "title": title,
        "cashbox_type": cashbox_type,
        "expected_balance": expected_balance,
        "back_url_name": (
            "finance_company_cash_dashboard"
            if cashbox_type == CashCount.CASHBOX_COMPANY
            else "finance_kkm_cash_dashboard"
        ),
        "denomination_fields": denomination_fields,
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/cash_count_form.html", context)


@login_required
def finance_cash_operation_detail(request, operation_id):
    operation = _cash_operation_for_user(request, operation_id)
    if not operation:
        return HttpResponseForbidden("Недостаточно прав.")
    return render(
        request,
        "pool_service/finance/cash_detail.html",
        {
            "operation": operation,
            "can_edit": _can_edit_cash_operation(request.user, operation),
            "can_delete": _can_delete_cash_operation(request.user, operation),
            "can_manage_cash": can_manage_cash(request.user, operation.organization),
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
@xframe_options_sameorigin
def finance_cash_operation_edit(request, operation_id):
    operation = _cash_operation_for_user(request, operation_id)
    if not operation:
        return HttpResponseForbidden("Недостаточно прав.")
    if not _can_edit_cash_operation(request.user, operation):
        messages.error(request, "Подтверждённую кассовую операцию редактировать нельзя.")
        return redirect("finance_cash_operation_detail", operation_id=operation.id)
    form = _cash_operation_form(operation, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_ISSUE:
                movement = form.save(commit=False)
                movement.organization = operation.organization
                movement.created_by = operation.created_by
                movement.transaction_type = AccountableTransaction.TYPE_ISSUE
                movement.status = AccountableTransaction.STATUS_PENDING
                movement.full_clean()
                movement.save()
                AccountableTransactionChange.objects.create(
                    transaction=movement,
                    actor=request.user,
                    action=AccountableTransactionChange.ACTION_UPDATED,
                    note="Изменение выдачи из ККМ",
                    payload={
                        "source": "manager_cashbox",
                        "employee_id": movement.employee_id,
                        "amount": str(movement.amount),
                        "occurred_on": movement.occurred_on.isoformat(),
                        "note": movement.note,
                    },
                )
                operation.accountable_transaction = movement
                operation.amount = movement.amount
                operation.occurred_on = movement.occurred_on
                operation.note = movement.note
            elif operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN:
                manager = form.cleaned_data["manager"]
                movement = form.save(commit=False)
                movement.organization = operation.organization
                movement.employee = operation.created_by
                movement.created_by = operation.created_by
                movement.transaction_type = AccountableTransaction.TYPE_RETURN
                movement.status = AccountableTransaction.STATUS_PENDING
                movement.full_clean()
                movement.save()
                AccountableTransactionChange.objects.create(
                    transaction=movement,
                    actor=request.user,
                    action=AccountableTransactionChange.ACTION_UPDATED,
                    note="Изменение возврата подотчёта",
                    payload={
                        "source": "manager_cashbox_return",
                        "manager_id": manager.id,
                        "amount": str(movement.amount),
                        "occurred_on": movement.occurred_on.isoformat(),
                        "note": movement.note,
                    },
                )
                operation.manager = manager
                operation.accountable_transaction = movement
                operation.amount = movement.amount
                operation.occurred_on = movement.occurred_on
                operation.note = movement.note
            else:
                updated = form.save(commit=False)
                operation.amount = updated.amount
                operation.occurred_on = updated.occurred_on
                operation.note = updated.note
                if operation.operation_type == CashOperation.TYPE_TRANSFER_TO_COMPANY:
                    operation.receiver = updated.receiver
            operation.full_clean()
            operation.save()
            _create_cash_operation_change(
                operation,
                request.user,
                CashOperationChange.ACTION_UPDATED,
                note="Операция изменена",
                payload={
                    "amount": str(operation.amount),
                    "occurred_on": operation.occurred_on.isoformat(),
                    "note": operation.note,
                    "receiver_id": operation.receiver_id,
                    "accountable_transaction_id": operation.accountable_transaction_id,
                },
            )
        messages.success(request, "Кассовая операция изменена.")
        return redirect("finance_cash_operation_detail", operation_id=operation.id)
    context = {
        "form": form,
        "title": f"Изменить: {operation.get_operation_type_display()}",
        "subtitle": "Редактирование доступно только до подтверждения",
        "submit_label": "Сохранить изменения",
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/cash_form.html", context)


@require_POST
@login_required
def finance_cash_operation_delete(request, operation_id):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    operation = get_object_or_404(
        CashOperation.objects.select_related("accountable_transaction"),
        id=operation_id,
        organization=organization,
    )
    if not _can_delete_cash_operation(request.user, operation):
        return HttpResponseForbidden("Удаление кассовых операций временно доступно только администратору.")
    with transaction.atomic():
        linked_transaction = operation.accountable_transaction
        operation.delete()
        if linked_transaction and not linked_transaction.cash_operations.exists():
            linked_transaction.delete()
    messages.success(request, "Кассовая операция и связанные события удалены.")
    return redirect("finance_kkm_cash_dashboard")


@require_POST
@login_required
def finance_cash_operation_review(request, operation_id):
    organization, denied = _cash_guard(request)
    if denied:
        return denied
    operation = get_object_or_404(CashOperation, id=operation_id, organization=organization)
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_ISSUE:
        return HttpResponseForbidden("Выдачу подотчёта из ККМ подтверждает сотрудник-получатель.")
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN and operation.manager_id != request.user.id:
        return HttpResponseForbidden("Возврат подотчёта подтверждает менеджер, которому возвращают деньги.")
    if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN and operation.created_by_id == request.user.id:
        return HttpResponseForbidden("Нельзя подтвердить собственный возврат подотчёта.")
    if operation.operation_type != CashOperation.TYPE_ACCOUNTABLE_RETURN and not can_manage_cash(request.user, organization):
        return HttpResponseForbidden("Недостаточно прав.")
    if not _cash_reviewers_can_review(request.user, operation):
        if operation.operation_type != CashOperation.TYPE_ACCOUNTABLE_RETURN:
            return HttpResponseForbidden("Нельзя подтвердить собственную кассовую операцию.")
    if operation.status != CashOperation.STATUS_PENDING:
        messages.error(request, "Операция уже рассмотрена.")
        return redirect("finance_kkm_cash_dashboard")
    if period_is_closed(organization, operation.occurred_on):
        messages.error(request, "Месяц закрыт. Для подтверждения нужно открыть период.")
        return redirect("finance_kkm_cash_dashboard")
    decision = request.POST.get("decision")
    if decision not in {CashOperation.STATUS_APPROVED, CashOperation.STATUS_REJECTED}:
        messages.error(request, "Выберите решение.")
        return redirect("finance_kkm_cash_dashboard")
    review_comment = (request.POST.get("review_comment") or "").strip()
    with transaction.atomic():
        operation.status = decision
        operation.reviewed_by = request.user
        operation.reviewed_at = timezone.now()
        operation.review_comment = review_comment
        operation.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment", "updated_at"])
        _create_cash_operation_change(
            operation,
            request.user,
            CashOperationChange.ACTION_APPROVED if decision == CashOperation.STATUS_APPROVED else CashOperationChange.ACTION_REJECTED,
            note=review_comment,
            payload={"decision": decision},
        )
        if operation.operation_type == CashOperation.TYPE_ACCOUNTABLE_RETURN and operation.accountable_transaction_id:
            movement = operation.accountable_transaction
            movement.status = decision
            movement.reviewed_by = request.user
            movement.reviewed_at = timezone.now()
            movement.review_comment = review_comment
            movement.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment"])
            AccountableTransactionChange.objects.create(
                transaction=movement,
                actor=request.user,
                action=(
                    AccountableTransactionChange.ACTION_UPDATED
                    if decision == AccountableTransaction.STATUS_APPROVED
                    else AccountableTransactionChange.ACTION_REJECTED
                ),
                note=review_comment or "Подтверждение возврата менеджером",
                payload={"decision": decision, "cash_operation_id": operation.id},
            )
    if decision == CashOperation.STATUS_APPROVED:
        messages.success(request, "Кассовая операция подтверждена.")
    else:
        messages.success(request, "Кассовая операция отклонена.")
    return redirect("finance_kkm_cash_dashboard")


@login_required
@xframe_options_sameorigin
def finance_transaction_create(request):
    organization, denied = _finance_guard(request, issue=True)
    if denied:
        return denied
    form = AccountableTransactionForm(
        request.POST or None,
        organization=organization,
        initial={"occurred_on": date.today()},
    )
    if request.method == "POST" and form.is_valid():
        movement = form.save(commit=False)
        movement.organization = organization
        movement.created_by = request.user
        requires_employee_confirmation = (
            movement.transaction_type == AccountableTransaction.TYPE_ISSUE
            and movement.employee_id != request.user.id
        )
        if requires_employee_confirmation:
            movement.status = AccountableTransaction.STATUS_PENDING
            movement.reviewed_by = None
            movement.reviewed_at = None
        else:
            movement.status = AccountableTransaction.STATUS_APPROVED
            movement.reviewed_by = request.user
            movement.reviewed_at = timezone.now()
        movement.full_clean()
        movement.save()
        AccountableTransactionChange.objects.create(
            transaction=movement,
            actor=request.user,
            action=AccountableTransactionChange.ACTION_CREATED,
            payload={
                "employee_id": movement.employee_id,
                "transaction_type": movement.transaction_type,
                "amount": str(movement.amount),
                "occurred_on": movement.occurred_on.isoformat(),
                "note": movement.note,
                "status": movement.status,
            },
        )
        notify_advance(movement)
        if requires_employee_confirmation:
            messages.success(request, "Выдача подотчёта отправлена сотруднику на подтверждение.")
        else:
            messages.success(request, "Операция подотчёта сохранена.")
        return redirect("finance_dashboard")
    context = {
        "form": form,
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/transaction_form.html", context)


@require_POST
@login_required
def finance_transaction_confirm(request, transaction_id):
    movement = get_object_or_404(
        AccountableTransaction.objects.select_related("organization", "employee", "created_by"),
        id=transaction_id,
    )
    if not can_confirm_accountable_issue(request.user, movement):
        return HttpResponseForbidden("Подтвердить выдачу может только сотрудник, которому выданы деньги.")
    if period_is_closed(movement.organization, movement.occurred_on):
        messages.error(request, "Месяц закрыт. Для подтверждения нужно открыть период.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_dashboard"))
    decision = request.POST.get("decision")
    if decision not in {AccountableTransaction.STATUS_APPROVED, AccountableTransaction.STATUS_REJECTED}:
        messages.error(request, "Выберите решение.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_dashboard"))
    review_comment = (request.POST.get("review_comment") or "").strip()
    with transaction.atomic():
        cash_operation = (
            CashOperation.objects.select_for_update()
            .filter(
                accountable_transaction=movement,
                status=CashOperation.STATUS_PENDING,
            )
            .first()
        )
        movement.status = decision
        movement.reviewed_by = request.user
        movement.reviewed_at = timezone.now()
        movement.review_comment = review_comment
        movement.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment"])
        AccountableTransactionChange.objects.create(
            transaction=movement,
            actor=request.user,
            action=(
                AccountableTransactionChange.ACTION_UPDATED
                if decision == AccountableTransaction.STATUS_APPROVED
                else AccountableTransactionChange.ACTION_REJECTED
            ),
            note=review_comment or "Подтверждение получателем",
            payload={"decision": decision},
        )
        if cash_operation:
            cash_operation.status = decision
            cash_operation.reviewed_by = request.user
            cash_operation.reviewed_at = timezone.now()
            cash_operation.review_comment = review_comment
            cash_operation.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment", "updated_at"])
            _create_cash_operation_change(
                cash_operation,
                request.user,
                CashOperationChange.ACTION_APPROVED
                if decision == CashOperation.STATUS_APPROVED
                else CashOperationChange.ACTION_REJECTED,
                note=review_comment or "Подтверждение получателем",
                payload={"decision": decision, "accountable_transaction_id": movement.id},
            )
    if decision == AccountableTransaction.STATUS_APPROVED:
        messages.success(request, "Получение подотчёта подтверждено.")
    else:
        messages.success(request, "Выдача подотчёта отклонена.")
    return redirect(request.META.get("HTTP_REFERER") or reverse("finance_dashboard"))


@login_required
@xframe_options_sameorigin
def finance_income_create(request):
    organization, denied = _finance_guard(request)
    if denied:
        return denied
    manage = can_manage_finance(request.user, organization)
    form = ClientPaymentForm(
        request.POST or None,
        organization=organization,
        user=request.user,
        can_manage=manage,
        initial={"occurred_on": date.today()},
    )
    if request.method == "POST" and form.is_valid():
        client = _resolve_income_client(organization, form)
        movement = form.save(commit=False)
        movement.organization = organization
        movement.created_by = request.user
        movement.client = client
        movement.transaction_type = AccountableTransaction.TYPE_CLIENT_PAYMENT
        movement.status = AccountableTransaction.STATUS_APPROVED
        movement.reviewed_by = request.user
        movement.reviewed_at = timezone.now()
        movement.full_clean()
        movement.save()
        AccountableTransactionChange.objects.create(
            transaction=movement,
            actor=request.user,
            action=AccountableTransactionChange.ACTION_CREATED,
            payload=_income_payload(form, client),
        )
        messages.success(request, "Приход денег сохранён.")
        return redirect("finance_dashboard")
    context = {
        "form": form,
        "client_options": _client_options(organization),
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/income_form.html", context)


@login_required
@xframe_options_sameorigin
def finance_income_edit(request, transaction_id):
    movement = _movement_for_user(request, transaction_id)
    if not movement:
        return HttpResponseForbidden("Недостаточно прав.")
    if not _can_request_income_change(request.user, movement):
        messages.error(request, "Изменение прихода денег недоступно.")
        return redirect("finance_employee_detail", employee_id=movement.employee_id)

    form = ClientPaymentForm(
        request.POST or None,
        organization=movement.organization,
        user=request.user,
        can_manage=can_manage_finance(request.user, movement.organization),
        instance=movement,
    )
    if request.method == "POST" and form.is_valid():
        client = _resolve_income_client(movement.organization, form)
        payload = _income_payload(form, client)
        movement.pending_action = AccountableTransaction.PENDING_EDIT
        movement.pending_payload = payload
        movement.pending_requested_by = request.user
        movement.pending_requested_at = timezone.now()
        movement.save(
            update_fields=[
                "pending_action",
                "pending_payload",
                "pending_requested_by",
                "pending_requested_at",
            ]
        )
        AccountableTransactionChange.objects.create(
            transaction=movement,
            actor=request.user,
            action=AccountableTransactionChange.ACTION_EDIT_REQUESTED,
            payload=payload,
        )
        messages.success(request, "Изменение отправлено на подтверждение.")
        return redirect("finance_employee_detail", employee_id=movement.employee_id)

    context = {
        "form": form,
        "movement": movement,
        "client_options": _client_options(movement.organization),
        "submit_label": "Отправить изменение",
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/income_form.html", context)


@require_POST
@login_required
def finance_income_delete(request, transaction_id):
    movement = _movement_for_user(request, transaction_id)
    if not movement:
        return HttpResponseForbidden("Недостаточно прав.")
    if not _can_request_income_change(request.user, movement):
        messages.error(request, "Удаление прихода денег недоступно.")
        return redirect("finance_employee_detail", employee_id=movement.employee_id)
    reason = (request.POST.get("reason") or "").strip()
    movement.pending_action = AccountableTransaction.PENDING_DELETE
    movement.pending_payload = {"reason": reason}
    movement.pending_requested_by = request.user
    movement.pending_requested_at = timezone.now()
    movement.save(
        update_fields=[
            "pending_action",
            "pending_payload",
            "pending_requested_by",
            "pending_requested_at",
        ]
    )
    AccountableTransactionChange.objects.create(
        transaction=movement,
        actor=request.user,
        action=AccountableTransactionChange.ACTION_DELETE_REQUESTED,
        note=reason,
        payload={"reason": reason},
    )
    messages.success(request, "Удаление отправлено на подтверждение.")
    return redirect("finance_employee_detail", employee_id=movement.employee_id)


@require_POST
@login_required
def finance_transaction_review(request, transaction_id):
    organization, denied = _finance_guard(request, close=True)
    if denied:
        return denied
    movement = get_object_or_404(AccountableTransaction, id=transaction_id, organization=organization)
    if movement.transaction_type != AccountableTransaction.TYPE_CLIENT_PAYMENT:
        return HttpResponseForbidden("Эта операция не требует подтверждения.")
    if not can_review_accountable_transaction(request.user, movement):
        return HttpResponseForbidden("Нельзя подтвердить собственную операцию.")
    if period_is_closed(organization, movement.occurred_on):
        messages.error(request, "Месяц закрыт.")
        return redirect("finance_dashboard")
    decision = request.POST.get("decision")
    if decision not in {AccountableTransaction.STATUS_APPROVED, AccountableTransaction.STATUS_REJECTED}:
        messages.error(request, "Выберите решение.")
        return redirect("finance_dashboard")
    review_comment = (request.POST.get("review_comment") or "").strip()

    if movement.pending_action:
        with transaction.atomic():
            action = movement.pending_action
            payload = dict(movement.pending_payload or {})
            if decision == AccountableTransaction.STATUS_APPROVED:
                if action == AccountableTransaction.PENDING_EDIT:
                    if period_is_closed(organization, date.fromisoformat(payload["occurred_on"])):
                        messages.error(request, "Новый месяц операции уже закрыт.")
                        return redirect("finance_employee_detail", employee_id=movement.employee_id)
                    _apply_income_payload(movement, payload)
                    movement.full_clean()
                    change_action = AccountableTransactionChange.ACTION_UPDATED
                    message = "Изменение прихода подтверждено."
                elif action == AccountableTransaction.PENDING_DELETE:
                    movement.is_voided = True
                    movement.voided_at = timezone.now()
                    movement.voided_by = request.user
                    movement.void_reason = review_comment or payload.get("reason", "")
                    change_action = AccountableTransactionChange.ACTION_DELETED
                    message = "Удаление прихода подтверждено."
                else:
                    return HttpResponseForbidden("Неизвестная заявка.")
            else:
                change_action = AccountableTransactionChange.ACTION_REJECTED
                message = "Заявка отклонена."
            movement.reviewed_by = request.user
            movement.reviewed_at = timezone.now()
            movement.review_comment = review_comment
            _clear_pending_movement(movement)
            movement.save()
            AccountableTransactionChange.objects.create(
                transaction=movement,
                actor=request.user,
                action=change_action,
                note=review_comment,
                payload=payload,
            )
        messages.success(request, message)
        return redirect("finance_employee_detail", employee_id=movement.employee_id)

    if movement.status != AccountableTransaction.STATUS_PENDING:
        messages.error(request, "Эта операция уже рассмотрена.")
        return redirect("finance_dashboard")
    movement.status = decision
    movement.reviewed_by = request.user
    movement.reviewed_at = timezone.now()
    movement.review_comment = review_comment
    movement.save(update_fields=["status", "review_comment", "reviewed_by", "reviewed_at"])
    messages.success(request, "Решение по приходу денег сохранено.")
    return redirect("finance_dashboard")


@require_POST
@login_required
def finance_transaction_void(request, transaction_id):
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    movement = get_object_or_404(AccountableTransaction, id=transaction_id, organization=organization)
    if movement.is_voided:
        return redirect("finance_dashboard")
    if period_is_closed(organization, movement.occurred_on):
        messages.error(request, "Операцию из закрытого месяца аннулировать нельзя.")
        return redirect("finance_dashboard")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Укажите причину аннулирования.")
        return redirect("finance_dashboard")
    movement.is_voided = True
    movement.voided_at = timezone.now()
    movement.voided_by = request.user
    movement.void_reason = reason
    movement.save(update_fields=["is_voided", "voided_at", "voided_by", "void_reason"])
    AccountableTransactionChange.objects.create(
        transaction=movement,
        actor=request.user,
        action=AccountableTransactionChange.ACTION_VOIDED,
        note=reason,
    )
    messages.success(request, "Операция аннулирована. История сохранена.")
    return redirect("finance_dashboard")


@login_required
def finance_employee_detail(request, employee_id):
    organization, denied = _finance_guard(request)
    if denied:
        return denied
    employee = get_object_or_404(finance_staff(organization), id=employee_id)
    if not can_manage_finance(request.user, organization) and request.user.id != employee.id:
        return HttpResponseForbidden("Недостаточно прав.")

    movements = list(
        AccountableTransaction.objects.filter(organization=organization, employee=employee)
        .select_related("client", "created_by", "reviewed_by", "voided_by", "pending_requested_by")
        .prefetch_related("changes__actor")
        .order_by("-occurred_on", "-id")
    )
    _decorate_accountable_movements(request.user, movements)

    recent_expenses = (
        Expense.objects.filter(organization=organization, employee=employee)
        .select_related("category", "client", "pool", "created_by", "reviewed_by")
        .order_by("-spent_on", "-id")[:50]
    )
    can_close = can_close_finance_period(request.user, organization)
    change_history = (
        AccountableTransactionChange.objects.filter(
            transaction__organization=organization,
            transaction__employee=employee,
        )
        .select_related("transaction", "actor")
        .order_by("-created_at", "-id")[:100]
        if can_close
        else []
    )
    context = {
        "employee": employee,
        "balance": accountable_balance(organization, employee),
        "movements": movements,
        "recent_expenses": recent_expenses,
        "change_history": change_history,
        "can_close_finance": can_close,
        "can_manage_finance": can_manage_finance(request.user, organization),
        "active_tab": "finance",
        "show_add_button": False,
    }
    return render(request, "pool_service/finance/employee_detail.html", context)


def _expense_form_response(request, organization, form, expense=None):
    context = {
        "form": form,
        "expense": expense,
        "client_options": _client_options(organization),
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/expense_form.html", context)


@login_required
@xframe_options_sameorigin
def finance_expense_create(request):
    organization, denied = _finance_guard(request)
    if denied:
        return denied
    ensure_default_categories(organization)
    manage = can_manage_finance(request.user, organization)
    requested_source = request.POST.get("source") if request.method == "POST" else request.GET.get("source")
    fixed_source = None
    if requested_source == Expense.SOURCE_KKM_CASH:
        if not can_access_cash(request.user, organization):
            return HttpResponseForbidden("Расход из кассы ККМ доступен только сотрудникам с доступом к ККМ.")
        fixed_source = Expense.SOURCE_KKM_CASH
    form = ExpenseForm(
        request.POST or None,
        request.FILES or None,
        organization=organization,
        user=request.user,
        can_manage=manage,
        fixed_source=fixed_source,
        initial={"spent_on": date.today(), "source": fixed_source or Expense.SOURCE_ACCOUNTABLE},
    )
    if request.method != "POST" or not form.is_valid():
        if request.method == "POST" and _is_offline_request(request):
            return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)
        return _expense_form_response(request, organization, form)

    request_id = form.cleaned_data["request_id"]
    existing = Expense.objects.filter(uuid=request_id).first()
    if existing:
        if existing.organization_id != organization.id or existing.created_by_id != request.user.id:
            return HttpResponseForbidden("Идентификатор запроса уже использован.")
        return _post_success(request, existing, "Расход уже был сохранён.")

    with transaction.atomic():
        client = form.resolved_client
        if form.new_client_name:
            client = find_client_by_name(organization, form.new_client_name)
            if not client:
                client = Client.objects.create(
                    organization=organization,
                    name=form.new_client_name,
                    client_type="private",
                )
        expense = form.save(commit=False)
        expense.uuid = request_id
        expense.organization = organization
        expense.created_by = request.user
        expense.client = client
        expense.pool = None
        expense.receipt_missing_confirmed = not bool(form.cleaned_data["receipts"])
        if expense.destination_type == Expense.DESTINATION_OFFICE:
            expense.destination_name = "Офисные расходы"
            expense.client = None
            expense.pool = None
        else:
            expense.destination_name = client.name
        roles = organization_roles(request.user, organization)
        if "owner" in roles:
            expense.status = Expense.STATUS_APPROVED
            expense.reviewed_by = request.user
            expense.reviewed_at = timezone.now()
        else:
            expense.status = Expense.STATUS_PENDING
        expense.full_clean()
        expense.save()
        _save_receipts(expense, form.cleaned_data["receipts"], request.user)
        ExpenseChange.objects.create(
            expense=expense,
            actor=request.user,
            action=ExpenseChange.ACTION_CREATED,
        )
    if expense.status == Expense.STATUS_PENDING:
        notify_expense_submitted(expense)
    success_message = (
        "Расход сохранён и подтверждён."
        if expense.status == Expense.STATUS_APPROVED
        else "Расход сохранён и отправлен на проверку."
    )
    return _post_success(request, expense, success_message)


@login_required
@xframe_options_sameorigin
def finance_expense_edit(request, expense_uuid):
    expense = _expense_for_user(request, expense_uuid)
    if not expense:
        return HttpResponseForbidden("Недостаточно прав.")
    if not can_edit_expense(request.user, expense):
        messages.error(request, "Подтверждённый расход изменять нельзя.")
        return redirect("finance_expense_detail", expense_uuid=expense.uuid)
    if period_is_closed(expense.organization, expense.spent_on):
        messages.error(request, "Расход относится к закрытому месяцу.")
        return redirect("finance_expense_detail", expense_uuid=expense.uuid)
    manage = can_manage_finance(request.user, expense.organization)
    form = ExpenseForm(
        request.POST or None,
        request.FILES or None,
        instance=expense,
        organization=expense.organization,
        user=request.user,
        can_manage=manage,
    )
    if request.method != "POST" or not form.is_valid():
        return _expense_form_response(request, expense.organization, form, expense=expense)
    with transaction.atomic():
        client = form.resolved_client
        if form.new_client_name:
            client = find_client_by_name(expense.organization, form.new_client_name)
            if not client:
                client = Client.objects.create(
                    organization=expense.organization,
                    name=form.new_client_name,
                    client_type="private",
                )
        updated = form.save(commit=False)
        updated.client = client
        updated.pool = None
        if form.cleaned_data["receipts"]:
            updated.receipt_missing_confirmed = False
        updated.status = Expense.STATUS_PENDING
        updated.reviewed_by = None
        updated.reviewed_at = None
        updated.review_comment = ""
        if updated.destination_type == Expense.DESTINATION_OFFICE:
            updated.destination_name = "Офисные расходы"
            updated.client = None
            updated.pool = None
        else:
            updated.destination_name = client.name
        updated.full_clean()
        updated.save()
        _save_receipts(updated, form.cleaned_data["receipts"], request.user)
        ExpenseChange.objects.create(
            expense=updated,
            actor=request.user,
            action=ExpenseChange.ACTION_UPDATED,
        )
    notify_expense_submitted(updated)
    messages.success(request, "Расход обновлён и повторно отправлен на проверку.")
    return redirect("finance_expense_detail", expense_uuid=updated.uuid)


@login_required
@xframe_options_sameorigin
def finance_expense_detail(request, expense_uuid):
    expense = _expense_for_user(request, expense_uuid)
    if not expense:
        return HttpResponseForbidden("Недостаточно прав.")
    can_delete = request.user.id == expense.created_by_id and not period_is_closed(expense.organization, expense.spent_on)
    return render(
        request,
        "pool_service/finance/expense_detail.html",
        {
            "expense": expense,
            "review_form": ExpenseReviewForm(),
            "can_review": can_review_expense(request.user, expense) and expense.status == Expense.STATUS_PENDING,
            "can_edit": can_edit_expense(request.user, expense) and not period_is_closed(expense.organization, expense.spent_on),
            "can_delete": can_delete,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@require_POST
@login_required
def finance_expense_delete(request, expense_uuid):
    expense = _expense_for_user(request, expense_uuid)
    if not expense:
        return HttpResponseForbidden("Недостаточно прав.")
    if request.user.id != expense.created_by_id:
        return HttpResponseForbidden("Удалить расход может только тот, кто его добавил.")
    if period_is_closed(expense.organization, expense.spent_on):
        messages.error(request, "Расход из закрытого месяца удалить нельзя.")
        return redirect("finance_expense_detail", expense_uuid=expense.uuid)

    receipts = list(expense.receipts.all())
    with transaction.atomic():
        expense.delete()
    for receipt in receipts:
        receipt.file.delete(save=False)
    messages.success(request, "Расход удалён.")
    return redirect("finance_dashboard")


@require_POST
@login_required
def finance_expense_review(request, expense_uuid):
    expense = get_object_or_404(Expense.objects.select_related("organization", "employee", "created_by"), uuid=expense_uuid)
    if not can_review_expense(request.user, expense):
        return HttpResponseForbidden("Нельзя согласовать собственный расход.")
    if expense.status != Expense.STATUS_PENDING:
        messages.error(request, "Этот расход уже рассмотрен.")
        return redirect("finance_expense_detail", expense_uuid=expense.uuid)
    if period_is_closed(expense.organization, expense.spent_on):
        messages.error(request, "Месяц закрыт.")
        return redirect("finance_expense_detail", expense_uuid=expense.uuid)
    form = ExpenseReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте решение и комментарий.")
        return redirect("finance_expense_detail", expense_uuid=expense.uuid)
    decision = form.cleaned_data["decision"]
    with transaction.atomic():
        expense.status = decision
        expense.review_comment = (form.cleaned_data["review_comment"] or "").strip()
        expense.reviewed_by = request.user
        expense.reviewed_at = timezone.now()
        expense.save(update_fields=["status", "review_comment", "reviewed_by", "reviewed_at", "updated_at"])
        action = ExpenseChange.ACTION_APPROVED if decision == Expense.STATUS_APPROVED else ExpenseChange.ACTION_REJECTED
        ExpenseChange.objects.create(
            expense=expense,
            actor=request.user,
            action=action,
            note=expense.review_comment,
        )
    notify_expense_reviewed(expense)
    messages.success(request, "Решение сохранено.")
    return redirect("finance_expense_detail", expense_uuid=expense.uuid)


@require_POST
@login_required
def finance_expense_bulk_review(request):
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    expense_ids = [value for value in request.POST.getlist("expense_ids") if value]
    decision = request.POST.get("decision", "").strip()
    if decision not in {Expense.STATUS_PENDING, Expense.STATUS_APPROVED, Expense.STATUS_REJECTED} or not expense_ids:
        messages.error(request, "Выберите расходы и решение.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_report"))
    review_comment = (request.POST.get("review_comment") or "").strip()
    if decision == Expense.STATUS_REJECTED and not review_comment:
        messages.error(request, "Для отклонения укажите причину.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_report"))
    expenses = list(
        Expense.objects.select_related("organization", "employee", "created_by")
        .filter(organization=organization, uuid__in=expense_ids)
    )
    reviewed_count = 0
    with transaction.atomic():
        for expense in expenses:
            if expense.status == decision:
                continue
            if not can_review_expense(request.user, expense) or period_is_closed(expense.organization, expense.spent_on):
                continue
            expense.status = decision
            expense.review_comment = review_comment
            if decision == Expense.STATUS_PENDING:
                expense.reviewed_by = None
                expense.reviewed_at = None
            else:
                expense.reviewed_by = request.user
                expense.reviewed_at = timezone.now()
            expense.save(update_fields=["status", "review_comment", "reviewed_by", "reviewed_at", "updated_at"])
            if decision == Expense.STATUS_APPROVED:
                action = ExpenseChange.ACTION_APPROVED
            elif decision == Expense.STATUS_REJECTED:
                action = ExpenseChange.ACTION_REJECTED
            else:
                action = ExpenseChange.ACTION_UPDATED
            ExpenseChange.objects.create(
                expense=expense,
                actor=request.user,
                action=action,
                note=review_comment,
            )
            reviewed_count += 1
            if decision != Expense.STATUS_PENDING:
                notify_expense_reviewed(expense)

    if reviewed_count:
        messages.success(request, f"Обработано расходов: {reviewed_count}.")
    else:
        messages.error(request, "Нет расходов, доступных для согласования.")
    return redirect(request.META.get("HTTP_REFERER") or reverse("finance_report"))


@login_required
@xframe_options_sameorigin
def finance_receipt_download(request, receipt_id):
    receipt = get_object_or_404(
        ExpenseReceipt.objects.select_related("expense__organization", "expense__employee", "expense__created_by"),
        id=receipt_id,
    )
    if not can_view_expense(request.user, receipt.expense):
        return HttpResponseForbidden("Недостаточно прав.")
    response = FileResponse(
        receipt.file.open("rb"),
        content_type=receipt.content_type or "application/octet-stream",
        as_attachment=False,
        filename=receipt.original_name,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _report_filters(request):
    def optional_id(name):
        try:
            value = int(request.GET.get(name, ""))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    return {
        "employee": optional_id("employee"),
        "category": optional_id("category"),
        "client": optional_id("client"),
        "source": request.GET.get("source", "").strip(),
        "status": request.GET.get("status", "").strip(),
        "search": request.GET.get("q", "").strip(),
    }


def _selected_month(request):
    month_value = request.GET.get("month") or date.today().strftime("%Y-%m")
    try:
        start, end = month_bounds(month_value)
    except (TypeError, ValueError):
        month_value = date.today().strftime("%Y-%m")
        start, end = month_bounds(month_value)
    return month_value, start, end


@login_required
def finance_report(request):
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    month_value, start, end = _selected_month(request)
    filters = _report_filters(request)
    expenses = list(report_expenses(organization, start, end, filters))
    approved_total = sum((item.amount for item in expenses if item.status == Expense.STATUS_APPROVED), Decimal("0.00"))
    pending_total = sum((item.amount for item in expenses if item.status == Expense.STATUS_PENDING), Decimal("0.00"))
    office_total = sum(
        (
            item.amount
            for item in expenses
            if item.status == Expense.STATUS_APPROVED and item.destination_type == Expense.DESTINATION_OFFICE
        ),
        Decimal("0.00"),
    )
    client_total = sum(
        (
            item.amount
            for item in expenses
            if item.status == Expense.STATUS_APPROVED and item.destination_type == Expense.DESTINATION_CLIENT
        ),
        Decimal("0.00"),
    )
    category_totals = defaultdict(lambda: Decimal("0.00"))
    client_totals = defaultdict(lambda: Decimal("0.00"))
    for expense in expenses:
        if expense.status != Expense.STATUS_APPROVED:
            continue
        category_totals[expense.category.name] += expense.amount
        client_totals[expense.destination_name] += expense.amount
    period = ExpensePeriod.objects.filter(organization=organization, month=start).first()
    return render(
        request,
        "pool_service/finance/report.html",
        {
            "month_value": month_value,
            "start": start,
            "end": end,
            "filters": filters,
            "expenses": expenses,
            "employee_rows": report_employee_rows(organization, start, end),
            "approved_total": approved_total,
            "pending_total": pending_total,
            "office_total": office_total,
            "client_total": client_total,
            "category_totals": sorted(category_totals.items(), key=lambda item: item[1], reverse=True),
            "client_totals": sorted(client_totals.items(), key=lambda item: item[1], reverse=True),
            "employees": finance_staff(organization),
            "categories": ExpenseCategory.objects.filter(organization=organization, is_active=True),
            "clients": Client.objects.filter(organization=organization).order_by("name"),
            "period": period,
            "can_close_finance": can_close_finance_period(request.user, organization),
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
def finance_report_export(request):
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    month_value, start, end = _selected_month(request)
    expenses = report_expenses(organization, start, end, _report_filters(request))
    dataset = tablib.Dataset(
        headers=[
            "Дата",
            "Сотрудник",
            "Источник",
            "Категория",
            "Клиент/офис",
            "Объект",
            "Поставщик",
            "Описание",
            "Статус",
            "Сумма",
            "Чеков",
        ]
    )
    for expense in expenses:
        dataset.append(
            [
                expense.spent_on.strftime("%d.%m.%Y"),
                user_display_name(expense.employee),
                expense.get_source_display(),
                expense.category.name,
                expense.destination_name,
                expense.pool.address if expense.pool else "",
                expense.vendor,
                expense.description,
                expense.get_status_display(),
                float(expense.amount),
                expense.receipts.count(),
            ]
        )
    response = HttpResponse(
        dataset.export("xlsx"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="expenses-{month_value}.xlsx"'
    return response


@require_POST
@login_required
def finance_period_close(request):
    organization, denied = _finance_guard(request, close=True)
    if denied:
        return denied
    try:
        month, start, end = _selected_month(request)
    except ValueError:
        messages.error(request, "Некорректный месяц.")
        return redirect("finance_report")
    if Expense.objects.filter(
        organization=organization,
        spent_on__range=(start, end),
        status=Expense.STATUS_PENDING,
    ).exists():
        messages.error(request, "Сначала рассмотрите все расходы за этот месяц.")
        return redirect(f"{reverse('finance_report')}?month={month}")
    period, _ = ExpensePeriod.objects.get_or_create(organization=organization, month=start)
    period.closed_at = timezone.now()
    period.closed_by = request.user
    period.save(update_fields=["closed_at", "closed_by"])
    messages.success(request, "Месяц закрыт.")
    return redirect(f"{reverse('finance_report')}?month={month}")


@require_POST
@login_required
def finance_period_reopen(request):
    organization, denied = _finance_guard(request, close=True)
    if denied:
        return denied
    month, start, _ = _selected_month(request)
    period = get_object_or_404(ExpensePeriod, organization=organization, month=start)
    period.closed_at = None
    period.closed_by = None
    period.save(update_fields=["closed_at", "closed_by"])
    messages.success(request, "Месяц снова открыт.")
    return redirect(f"{reverse('finance_report')}?month={month}")


@login_required
def finance_onec_import_list(request):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    batches = OneCImportBatch.objects.filter(organization=organization).select_related("uploaded_by")
    return render(request, "pool_service/finance/onec_import_list.html", {
        "batches": batches,
        "active_tab": "finance",
    })


@login_required
def finance_onec_monthly_profit_upload(request):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    form = MonthlyProfitUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = create_monthly_profit_preview(form.cleaned_data["report"], organization, request.user)
        except DuplicateImportError as exc:
            messages.info(request, "Этот файл уже загружен. Открыт существующий импорт.")
            target = "finance_onec_import_preview" if exc.batch.status == OneCImportBatch.STATUS_PREVIEWED else "finance_onec_import_detail"
            return redirect(target, batch_id=exc.batch.id)
        except Exception as exc:
            logger.exception(
                "1C import upload failed organization=%s user=%s error_type=%s",
                organization.id,
                request.user.id,
                type(exc).__name__,
            )
            messages.error(request, "Не удалось обработать XLSX. Проверьте формат отчёта.")
            return redirect("finance_onec_import_list")
        return redirect("finance_onec_import_preview", batch_id=batch.id)
    return render(request, "pool_service/finance/onec_import_upload.html", {
        "form": form,
        "active_tab": "finance",
    })


@login_required
def finance_onec_import_preview(request, batch_id):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(OneCImportBatch, id=batch_id, organization=organization)
    return render(request, "pool_service/finance/onec_import_preview.html", {
        "batch": batch,
        "preview": batch.metadata.get("preview", []),
        "report": batch.metadata.get("report", {}),
        "totals": batch.metadata.get("totals", {}),
        "warnings": batch.metadata.get("warnings", []),
        "warnings_hidden": batch.metadata.get("warnings_hidden", 0),
        "critical_errors": batch.metadata.get("critical_errors", []),
        "can_confirm": batch.status == OneCImportBatch.STATUS_PREVIEWED and not batch.metadata.get("critical_errors"),
        "active_tab": "finance",
    })


@require_POST
@login_required
def finance_onec_import_confirm(request, batch_id):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(OneCImportBatch, id=batch_id, organization=organization)
    if batch.status != OneCImportBatch.STATUS_PREVIEWED:
        messages.error(request, "Этот импорт уже обработан или недоступен для подтверждения.")
        return redirect("finance_onec_import_detail", batch_id=batch.id)
    try:
        batch = confirm_monthly_profit(batch.id, organization, request.user)
    except Exception:
        messages.error(request, "Импорт не выполнен. Частичные данные не сохранены.")
        return redirect("finance_onec_import_preview", batch_id=batch.id)
    messages.success(request, "Отчёт 1С импортирован.")
    return redirect("finance_onec_import_detail", batch_id=batch.id)


@require_POST
@login_required
def finance_onec_import_cancel(request, batch_id):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(OneCImportBatch, id=batch_id, organization=organization)
    try:
        cancel_monthly_profit(batch, request.user)
    except Exception:
        messages.error(request, "Подтверждённый импорт отменить нельзя.")
    else:
        messages.success(request, "Импорт отменён, временный файл удалён.")
    return redirect("finance_onec_import_detail", batch_id=batch.id)


@login_required
def finance_onec_import_detail(request, batch_id):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(OneCImportBatch, id=batch_id, organization=organization)
    rows = OneCMonthlyProfit.objects.filter(import_batch=batch, organization=organization)
    totals = rows.aggregate(revenue=Sum("revenue"), cost=Sum("cost"), gross_profit=Sum("gross_profit"))
    revenue = totals["revenue"] or Decimal("0")
    gross_profit = totals["gross_profit"] or Decimal("0")
    totals["profitability_percent"] = calculate_profitability(gross_profit, revenue)
    monthly = list(rows.values("period_month").annotate(
        revenue=Sum("revenue"), cost=Sum("cost"), gross_profit=Sum("gross_profit")
    ).order_by("period_month"))
    for item in monthly:
        month_revenue = item["revenue"] or Decimal("0")
        item["profitability_percent"] = calculate_profitability(item["gross_profit"], month_revenue)
    page = Paginator(rows.order_by("period_month", "source_row_number"), 50).get_page(request.GET.get("page"))
    return render(request, "pool_service/finance/onec_import_detail.html", {
        "batch": batch, "totals": totals, "monthly": monthly, "page_obj": page,
        "active_tab": "finance",
    })
