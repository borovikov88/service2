from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import tablib
from PIL import Image, ImageOps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from pool_service.finance_forms import AccountableTransactionForm, ClientPaymentForm, ExpenseForm, ExpenseReviewForm
from pool_service.models import (
    AccountableTransaction,
    AccountableTransactionChange,
    Client,
    Expense,
    ExpenseCategory,
    ExpenseChange,
    ExpensePeriod,
    ExpenseReceipt,
)
from pool_service.services.finance import (
    accountable_balance,
    accountable_rows,
    can_access_finance,
    can_close_finance_period,
    can_edit_expense,
    can_manage_finance,
    can_review_accountable_transaction,
    can_review_expense,
    can_view_expense,
    ensure_default_categories,
    finance_staff,
    find_client_by_name,
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


def _organization_for_finance(request):
    return organization_for_user(request.user)


def _finance_guard(request, *, manage=False, close=False):
    organization = _organization_for_finance(request)
    if not organization:
        return None, HttpResponseForbidden("Организация не найдена.")
    allowed = can_access_finance(request.user, organization)
    if manage:
        allowed = can_manage_finance(request.user, organization)
    if close:
        allowed = can_close_finance_period(request.user, organization)
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


@login_required
@xframe_options_sameorigin
def finance_dashboard(request):
    organization, denied = _finance_guard(request)
    if denied:
        return denied
    ensure_default_categories(organization)
    manage = can_manage_finance(request.user, organization)
    today = date.today()
    month_start, month_end = month_bounds(today)
    expenses = (
        Expense.objects.filter(organization=organization)
        .select_related("employee", "category", "client")
        .prefetch_related("receipts")
    )
    transactions = AccountableTransaction.objects.filter(
        organization=organization,
        is_voided=False,
    ).select_related("employee", "created_by", "client", "pending_requested_by")
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
    return render(
        request,
        "pool_service/finance/dashboard.html",
        {
            "organization": organization,
            "can_manage_finance": manage,
            "can_close_finance": can_close_finance_period(request.user, organization),
            "balance_rows": balance_rows,
            "latest_expenses": expenses[:20],
            "latest_transactions": transactions[:15],
            "approved_total": approved_total,
            "pending_total": pending_total,
            "company_cash_total": company_cash_total,
            "month_label": today.strftime("%m.%Y"),
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
@xframe_options_sameorigin
def finance_transaction_create(request):
    organization, denied = _finance_guard(request, manage=True)
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
        movement.status = AccountableTransaction.STATUS_APPROVED
        movement.reviewed_by = request.user
        movement.reviewed_at = timezone.now()
        movement.full_clean()
        movement.save()
        notify_advance(movement)
        messages.success(request, "Операция подотчёта сохранена.")
        return redirect("finance_dashboard")
    context = {
        "form": form,
        "active_tab": "finance",
        "show_add_button": False,
    }
    context.update(_finance_modal_context(request))
    return render(request, "pool_service/finance/transaction_form.html", context)


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
    for movement in movements:
        movement.can_request_change = _can_request_income_change(request.user, movement)

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
    form = ExpenseForm(
        request.POST or None,
        request.FILES or None,
        organization=organization,
        user=request.user,
        can_manage=manage,
        initial={"spent_on": date.today()},
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
