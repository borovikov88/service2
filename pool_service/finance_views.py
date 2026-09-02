from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import logging
import uuid
from urllib.parse import urlencode

import tablib
from PIL import Image, ImageOps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.db.models import Case, DecimalField, F, Sum, Value, When
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
    ODataCashFlowDraftForm,
    ODataProfitDraftForm,
    OneCCostControlFilterForm,
    PayrollUploadForm,
    PayrollConfirmForm,
    EmployeeIdentityMappingForm,
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
    Employee,
    EmployeeOneCIdentity,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCODataSyncRun,
    OneCReportPeriodState,
)
from pool_service.finance_imports.payroll_services import (
    confirm_payroll,
    create_payroll_preview,
    payroll_confirmation_state,
)
from pool_service.finance_imports.payroll_parser import PARSER_VERSION as PAYROLL_PARSER_VERSION
from pool_service.finance_imports.employee_matching import confirm_employee_identity
from pool_service.finance_imports.payroll_dashboard import (
    parse_payroll_period,
    payroll_dashboard_data,
    payroll_identity_rows,
    unresolved_active_payroll_identity_count,
)
from pool_service.finance_imports.cashflow_dashboard import (
    cashflow_article_trend_data,
    cashflow_dashboard_data,
)
from pool_service.finance_imports.services import (
    DuplicateImportError,
    calculate_profitability,
    cancel_monthly_profit,
    cancel_onec_import_batch,
    confirm_monthly_profit,
    create_monthly_profit_preview,
)
from pool_service.finance_imports.odata_profit import ODataPreviewError
from pool_service.finance_imports.odata_profit_drafts import (
    ODataDraftError,
    confirm_odata_profit,
    create_odata_profit_draft,
    is_odata_target_organization,
)
from pool_service.finance_imports.odata_cashflow_drafts import (
    ODataCashFlowDraftError,
    confirm_odata_cashflow,
    create_odata_cashflow_draft,
)
from pool_service.finance_imports.odata_unified_sync import (
    REPORT_CASHFLOW,
    REPORT_PROFIT,
    StaleReactivationError,
    SyncConflictError,
    reactivate_confirmed_candidate,
    start_unified_sync,
    step_unified_sync,
)
from pool_service.finance_imports.cashflow_services import confirm_cashflow
from pool_service.finance_imports.profit_dashboard import (
    PERIOD_CHOICES,
    _manager_key,
    dashboard_data,
    resolve_period,
)
from pool_service.finance_imports.cost_control import (
    available_cost_control_months,
    get_onec_cost_anomalies,
    get_onec_cost_control_dataset,
    monthly_cost_anomaly_summary,
    summarize_active_dataset,
    summarize_cost_anomalies,
)
from pool_service.services.finance import (
    accountable_balance,
    accountable_rows,
    can_access_cash,
    can_access_finance,
    can_access_finance_data,
    can_access_finance_overview,
    can_access_finance_section,
    can_access_my_finances,
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
    can_view_payroll_summary,
    can_view_payroll_personal,
    can_import_payroll,
    can_manage_employee_mapping,
    can_import_gross_profit,
    can_import_cashflow,
    can_view_cashflow,
    can_view_cost_control,
    can_view_gross_profit,
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
from pool_service.finance_imports.overview import finance_overview_data
from pool_service.services.permissions import is_org_access_blocked, organization_for_user

logger = logging.getLogger(__name__)


def _organization_for_finance(request):
    return organization_for_user(request.user)


def _onec_import_guard(request):
    return _capability_guard(request, can_import_gross_profit)


def _onec_cashflow_import_guard(request):
    return _capability_guard(request, can_import_cashflow)


def _onec_cashflow_access_guard(request):
    return _capability_guard(
        request,
        lambda user, organization: (
            can_view_cashflow(user, organization)
            or can_import_cashflow(user, organization)
        ),
    )


def _onec_accessible_import_types(user, organization):
    import_types = []
    if can_import_gross_profit(user, organization):
        import_types.append(OneCImportBatch.TYPE_MONTHLY_PROFIT)
    if (
        can_view_cashflow(user, organization)
        or can_import_cashflow(user, organization)
    ):
        import_types.append(OneCImportBatch.TYPE_CASHFLOW)
    return import_types


def _onec_import_list_guard(request):
    return _capability_guard(
        request,
        lambda user, organization: (
            can_import_gross_profit(user, organization)
            or can_import_cashflow(user, organization)
            or can_view_cashflow(user, organization)
        ),
    )


def _onec_sync_report_types(user, organization):
    result = []
    if can_import_gross_profit(user, organization):
        result.append(REPORT_PROFIT)
    if can_import_cashflow(user, organization):
        result.append(REPORT_CASHFLOW)
    return result


def _onec_sync_guard(request):
    return _capability_guard(
        request,
        lambda user, organization: bool(_onec_sync_report_types(user, organization)),
    )


def _capability_guard(request, permission, *, denied_message="Недостаточно прав."):
    organization = _organization_for_finance(request)
    if not organization:
        return None, HttpResponseForbidden("Организация не найдена.")
    if not permission(request.user, organization):
        return organization, HttpResponseForbidden(denied_message)
    if is_org_access_blocked(request.user):
        messages.error(request, "Доступ организации к сервису приостановлен.")
        return organization, redirect("billing")
    return organization, None


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


def _income_edit_return_context(request, movement):
    """Build the only permitted parent link for an income edit form."""
    requested = request.POST.get("return_to") if request.method == "POST" else request.GET.get("return_to")
    return_to = requested if requested in {"employee_detail"} else "employee_detail"
    return {
        "return_to": return_to,
        "return_url": reverse("finance_employee_detail", kwargs={"employee_id": movement.employee_id}),
    }


def _cash_operation_edit_return_context(request, operation):
    """Build a parent link from a small allowlist, never from a user URL."""
    requested = request.POST.get("return_to") if request.method == "POST" else request.GET.get("return_to")
    return_to = requested if requested in {"operation_detail", "kkm_dashboard"} else "operation_detail"
    if return_to == "kkm_dashboard":
        return_url = reverse("finance_kkm_cash_dashboard")
    else:
        return_url = reverse("finance_cash_operation_detail", kwargs={"operation_id": operation.id})
    return {
        "return_to": return_to,
        "return_url": return_url,
    }


def _expense_return_source(request):
    """Return only submitted navigation context, never a caller URL."""
    return request.POST if request.method == "POST" else request.GET


def _normalized_expense_report_filters(values, organization, prefix=""):
    """Keep only report filters that are valid for this organization."""
    normalized = {}

    month_value = (values.get(f"{prefix}month") or "").strip()
    if month_value:
        try:
            month_start, _ = month_bounds(month_value)
        except (TypeError, ValueError):
            pass
        else:
            normalized["month"] = month_start.strftime("%Y-%m")

    def organization_id(name, queryset):
        try:
            value = int(values.get(f"{prefix}{name}", ""))
        except (TypeError, ValueError):
            return
        if value > 0 and queryset.filter(id=value).exists():
            normalized[name] = str(value)

    organization_id("employee", finance_staff(organization))
    organization_id("category", ExpenseCategory.objects.filter(organization=organization, is_active=True))

    client_value = (values.get(f"{prefix}client") or "").strip()
    if client_value == Expense.DESTINATION_OFFICE:
        normalized["client"] = client_value
    else:
        organization_id("client", Client.objects.filter(organization=organization))

    source = (values.get(f"{prefix}source") or "").strip()
    if source in dict(Expense.SOURCE_CHOICES):
        normalized["source"] = source

    status = (values.get(f"{prefix}status") or "").strip()
    if status in dict(Expense.STATUS_CHOICES):
        normalized["status"] = status

    search = (values.get(f"{prefix}q") or "").strip()
    if search:
        normalized["q"] = search
    return normalized


def _expense_return_context(request, expense):
    """Build an expense parent URL from a discrete allowlist only."""
    values = _expense_return_source(request)
    requested = values.get("return_to")
    fields = []
    return_url = reverse("finance_my")
    return_label = "К моим финансам"

    if requested == "expense_report" and can_manage_finance(request.user, expense.organization):
        report_filters = _normalized_expense_report_filters(values, expense.organization, prefix="report_")
        fields = [("return_to", "expense_report"), *[(f"report_{name}", value) for name, value in report_filters.items()]]
        return_url = reverse("finance_report")
        if report_filters:
            return_url = f"{return_url}?{urlencode(report_filters)}"
        return_label = "К отчёту"
    elif requested == "employee_detail" and (
        can_manage_finance(request.user, expense.organization)
        or request.user.id == expense.employee_id
    ):
        fields = [("return_to", "employee_detail")]
        return_url = reverse("finance_employee_detail", kwargs={"employee_id": expense.employee_id})
        return_label = "К сотруднику"

    return {
        "return_url": return_url,
        "return_label": return_label,
        "return_context_fields": fields,
        "return_query": urlencode(fields),
    }


def _expense_bulk_report_return_url(request, organization):
    """Return bulk actions only to a normalized expense report context."""
    if request.POST.get("return_to") != "expense_report":
        return reverse("finance_report")
    report_filters = _normalized_expense_report_filters(
        request.POST,
        organization,
        prefix="report_",
    )
    report_url = reverse("finance_report")
    return f"{report_url}?{urlencode(report_filters)}" if report_filters else report_url


def _expense_detail_url(expense, return_context):
    url = reverse("finance_expense_detail", kwargs={"expense_uuid": expense.uuid})
    return f"{url}?{return_context['return_query']}" if return_context["return_query"] else url


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
    if user.is_superuser or "admin" in organization_roles(user, operation.organization):
        return True
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


def _kkm_history_entries(organization, user):
    operations = list(
        CashOperation.objects.filter(organization=organization)
        .select_related(
            "manager",
            "receiver",
            "created_by",
            "reviewed_by",
            "accountable_transaction__employee",
        )
        .order_by("-occurred_on", "-created_at", "-id")[:100]
    )
    expenses = list(
        Expense.objects.filter(organization=organization, source=Expense.SOURCE_KKM_CASH)
        .select_related("employee", "category", "created_by", "reviewed_by")
        .order_by("-spent_on", "-created_at", "-id")[:100]
    )
    incoming_types = {
        CashOperation.TYPE_MANAGER_INCOME,
        CashOperation.TYPE_ACCOUNTABLE_RETURN,
        CashOperation.TYPE_CASH_COUNT_INCOME,
    }
    entries = []
    for operation in operations:
        operation.can_edit = _can_edit_cash_operation(user, operation)
        operation.can_delete = _can_delete_cash_operation(user, operation)
        operation.can_review = (
            operation.status == CashOperation.STATUS_PENDING
            and operation.operation_type not in {
                CashOperation.TYPE_ACCOUNTABLE_ISSUE,
                CashOperation.TYPE_ACCOUNTABLE_RETURN,
            }
            and _cash_reviewers_can_review(user, operation)
        )
        entries.append(
            {
                "kind": "operation",
                "operation": operation,
                "business_date": operation.occurred_on,
                "created_at": operation.created_at,
                "entry_id": operation.id,
                "direction": "in" if operation.operation_type in incoming_types else "out",
                "sign": "+" if operation.operation_type in incoming_types else "−",
                "status_label": operation.get_status_display(),
                "status_class": {
                    CashOperation.STATUS_APPROVED: "text-bg-success",
                    CashOperation.STATUS_REJECTED: "text-bg-danger",
                }.get(operation.status, "text-bg-warning"),
            }
        )
    for expense in expenses:
        entries.append(
            {
                "kind": "expense",
                "expense": expense,
                "business_date": expense.spent_on,
                "created_at": expense.created_at,
                "entry_id": expense.id,
                "direction": "out",
                "sign": "−",
                "actor": expense.employee,
                "related_label": expense.category.name,
                "status_label": expense.get_status_display(),
                "status_class": {
                    Expense.STATUS_APPROVED: "text-bg-success",
                    Expense.STATUS_REJECTED: "text-bg-danger",
                }.get(expense.status, "text-bg-warning"),
            }
        )
    return sorted(
        entries,
        key=lambda entry: (entry["business_date"], entry["created_at"], entry["entry_id"]),
        reverse=True,
    )[:100]


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
    can_create_kkm_operations = can_access_cash(request.user, organization)
    if section not in {"company", "kkm"}:
        return redirect("finance_kkm_cash_dashboard" if can_access_cash(request.user, organization) else "finance_my")

    kkm_history = []
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
        kkm_balance = kkm_cash_balance(organization)
        kkm_history = _kkm_history_entries(organization, request.user)
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
            "can_create_manager_cash": can_create_kkm_operations,
            "can_return_accountable": can_access_cash(request.user, organization),
            "can_delete_cash_operations": request.user.is_superuser or "admin" in roles,
            "company_balance": company_balance,
            "kkm_balance": kkm_balance,
            "manager_rows": manager_rows,
            "kkm_history": kkm_history,
            "company_counts": company_counts,
            "kkm_counts": kkm_counts,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
def finance_dashboard(request):
    organization, denied = _capability_guard(
        request, can_access_finance_section,
        denied_message="Недостаточно прав для раздела Финансы.",
    )
    if denied:
        return denied
    if can_access_finance_overview(request.user, organization):
        return redirect("finance_overview")
    if can_access_my_finances(request.user, organization):
        return redirect("finance_my")
    if can_access_finance_data(request.user, organization):
        return redirect("finance_data")
    if can_view_cashflow(request.user, organization):
        return redirect("finance_onec_cashflow_dashboard")
    return HttpResponseForbidden("Недостаточно прав для раздела Финансы.")


@login_required
def finance_overview(request):
    organization, denied = _capability_guard(
        request, can_access_finance_overview,
        denied_message="Недостаточно прав для финансового обзора.",
    )
    if denied:
        return denied
    overview = finance_overview_data(organization, request.GET)
    return render(request, "pool_service/finance/overview.html", {
        **overview,
        "organization": organization,
        "can_view_gross_profit": can_view_gross_profit(request.user, organization),
        "can_view_payroll": can_view_payroll_summary(request.user, organization),
        "can_view_cashflow": can_view_cashflow(request.user, organization),
        "can_view_cost_control": can_view_cost_control(request.user, organization),
        "can_access_finance_data": can_access_finance_data(request.user, organization),
        "active_tab": "finance",
        "show_add_button": False,
    })


@login_required
def finance_data(request):
    organization, denied = _capability_guard(
        request, can_access_finance_data,
        denied_message="Недостаточно прав для данных 1С.",
    )
    if denied:
        return denied
    return render(request, "pool_service/finance/data.html", {
        "organization": organization,
        "can_import_gross_profit": can_import_gross_profit(request.user, organization),
        "can_import_cashflow": can_import_cashflow(request.user, organization),
        "can_import_payroll": can_import_payroll(request.user, organization),
        "can_manage_employee_mapping": can_manage_employee_mapping(request.user, organization),
        "active_tab": "finance",
        "show_add_button": False,
    })


@login_required
@xframe_options_sameorigin
def finance_my(request):
    organization, denied = _capability_guard(
        request, can_access_my_finances,
        denied_message="Недостаточно прав для операционных финансов.",
    )
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
    if not can_access_cash(request.user, organization):
        return HttpResponseForbidden("Поступление в ККМ доступно только сотрудникам с доступом к ККМ.")
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
    if not can_access_cash(request.user, organization):
        return HttpResponseForbidden("Сдать выручку может только сотрудник с доступом к ККМ.")
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
    if not can_access_cash(request.user, organization):
        return HttpResponseForbidden("Выдать подотчёт из ККМ может только сотрудник с доступом к ККМ.")
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
        return redirect("finance_kkm_cash_dashboard" if can_access_cash(request.user, organization) else "finance_my")
    context = {
        "form": form,
        "title": "Возврат подотчёта",
        "subtitle": "Деньги попадут в кассу ККМ после подтверждения менеджером",
        "submit_label": "Отправить возврат",
        "back_url_name": "finance_kkm_cash_dashboard" if can_access_cash(request.user, organization) else "finance_my",
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
    kkm_balance = None
    if cashbox_type == CashCount.CASHBOX_COMPANY:
        expected_balance = company_cash_balance(organization)["balance"]
    else:
        kkm_balance = kkm_cash_balance(organization)
        expected_balance = kkm_balance["available_balance"]
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
        "cash_balance_before_reserves": kkm_balance["balance"] if kkm_balance else None,
        "cash_reserved_total": kkm_balance["reserved_total"] if kkm_balance else None,
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
    return_context = _cash_operation_edit_return_context(request, operation)
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
    context.update(return_context)
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
        return redirect("finance_my")
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
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_my"))
    decision = request.POST.get("decision")
    if decision not in {AccountableTransaction.STATUS_APPROVED, AccountableTransaction.STATUS_REJECTED}:
        messages.error(request, "Выберите решение.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("finance_my"))
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
    return redirect(request.META.get("HTTP_REFERER") or reverse("finance_my"))


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
        return redirect("finance_my")
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

    return_context = _income_edit_return_context(request, movement)
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
    context.update(return_context)
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
        return redirect("finance_my")
    decision = request.POST.get("decision")
    if decision not in {AccountableTransaction.STATUS_APPROVED, AccountableTransaction.STATUS_REJECTED}:
        messages.error(request, "Выберите решение.")
        return redirect("finance_my")
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
        return redirect("finance_my")
    movement.status = decision
    movement.reviewed_by = request.user
    movement.reviewed_at = timezone.now()
    movement.review_comment = review_comment
    movement.save(update_fields=["status", "review_comment", "reviewed_by", "reviewed_at"])
    messages.success(request, "Решение по приходу денег сохранено.")
    return redirect("finance_my")


@require_POST
@login_required
def finance_transaction_void(request, transaction_id):
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    movement = get_object_or_404(AccountableTransaction, id=transaction_id, organization=organization)
    if movement.is_voided:
        return redirect("finance_my")
    if period_is_closed(organization, movement.occurred_on):
        messages.error(request, "Операцию из закрытого месяца аннулировать нельзя.")
        return redirect("finance_my")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Укажите причину аннулирования.")
        return redirect("finance_my")
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
    return redirect("finance_my")


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


def _expense_form_response(request, organization, form, expense=None, return_context=None):
    context = {
        "form": form,
        "expense": expense,
        "client_options": _client_options(organization),
        "active_tab": "finance",
        "show_add_button": False,
    }
    if return_context:
        context.update(return_context)
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
    return_context = _expense_return_context(request, expense)
    if not can_edit_expense(request.user, expense):
        messages.error(request, "Подтверждённый расход изменять нельзя.")
        return redirect(_expense_detail_url(expense, return_context))
    if period_is_closed(expense.organization, expense.spent_on):
        messages.error(request, "Расход относится к закрытому месяцу.")
        return redirect(_expense_detail_url(expense, return_context))
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
        return _expense_form_response(
            request,
            expense.organization,
            form,
            expense=expense,
            return_context=return_context,
        )
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
    return redirect(_expense_detail_url(updated, return_context))


@login_required
@xframe_options_sameorigin
def finance_expense_detail(request, expense_uuid):
    expense = _expense_for_user(request, expense_uuid)
    if not expense:
        return HttpResponseForbidden("Недостаточно прав.")
    return_context = _expense_return_context(request, expense)
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
            **return_context,
        },
    )


@require_POST
@login_required
def finance_expense_delete(request, expense_uuid):
    expense = _expense_for_user(request, expense_uuid)
    if not expense:
        return HttpResponseForbidden("Недостаточно прав.")
    return_context = _expense_return_context(request, expense)
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
    return redirect(return_context["return_url"])


@require_POST
@login_required
def finance_expense_review(request, expense_uuid):
    expense = get_object_or_404(Expense.objects.select_related("organization", "employee", "created_by"), uuid=expense_uuid)
    if not can_review_expense(request.user, expense):
        return HttpResponseForbidden("Нельзя согласовать собственный расход.")
    return_context = _expense_return_context(request, expense)
    if expense.status != Expense.STATUS_PENDING:
        messages.error(request, "Этот расход уже рассмотрен.")
        return redirect(_expense_detail_url(expense, return_context))
    if period_is_closed(expense.organization, expense.spent_on):
        messages.error(request, "Месяц закрыт.")
        return redirect(_expense_detail_url(expense, return_context))
    form = ExpenseReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте решение и комментарий.")
        return redirect(_expense_detail_url(expense, return_context))
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
    return redirect(_expense_detail_url(expense, return_context))


@require_POST
@login_required
def finance_expense_bulk_review(request):
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    report_return_url = _expense_bulk_report_return_url(request, organization)
    expense_ids = [value for value in request.POST.getlist("expense_ids") if value]
    decision = request.POST.get("decision", "").strip()
    if decision not in {Expense.STATUS_PENDING, Expense.STATUS_APPROVED, Expense.STATUS_REJECTED} or not expense_ids:
        messages.error(request, "Выберите расходы и решение.")
        return redirect(report_return_url)
    review_comment = (request.POST.get("review_comment") or "").strip()
    if decision == Expense.STATUS_REJECTED and not review_comment:
        messages.error(request, "Для отклонения укажите причину.")
        return redirect(report_return_url)
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
    return redirect(report_return_url)


@require_POST
@login_required
def finance_expense_bulk_onec(request):
    """Atomically set the independent 1C posting marker for selected expenses."""
    organization, denied = _finance_guard(request, manage=True)
    if denied:
        return denied
    report_return_url = _expense_bulk_report_return_url(request, organization)

    posted_value = request.POST.get("posted_to_1c", "").strip()
    if posted_value not in {"true", "false"}:
        return HttpResponseForbidden("Некорректное действие с отметкой 1С.")
    expense_ids = [value for value in request.POST.getlist("expense_ids") if value]
    if not expense_ids:
        messages.error(request, "Выберите расходы.")
        return redirect(report_return_url)
    try:
        requested_ids = {uuid.UUID(value) for value in expense_ids}
    except (TypeError, ValueError, AttributeError):
        return HttpResponseForbidden("Некорректный список расходов.")

    target_posted = posted_value == "true"
    with transaction.atomic():
        expenses = list(
            Expense.objects.select_for_update().filter(
                organization=organization,
                uuid__in=requested_ids,
            )
        )
        # A mixed-tenant or nonexistent set must never be partially updated.
        if len(expenses) != len(requested_ids):
            return HttpResponseForbidden("Часть расходов недоступна.")

        changed_count = 0
        now = timezone.now()
        for expense in expenses:
            if expense.posted_to_1c == target_posted:
                continue
            expense.posted_to_1c = target_posted
            if target_posted:
                expense.posted_to_1c_by = request.user
                expense.posted_to_1c_at = now
                action = ExpenseChange.ACTION_POSTED_TO_1C
            else:
                expense.posted_to_1c_by = None
                expense.posted_to_1c_at = None
                action = ExpenseChange.ACTION_UNPOSTED_FROM_1C
            expense.save(update_fields=["posted_to_1c", "posted_to_1c_by", "posted_to_1c_at", "updated_at"])
            ExpenseChange.objects.create(expense=expense, actor=request.user, action=action)
            changed_count += 1

    if changed_count:
        label = "внесёнными в 1С" if target_posted else "не внесёнными в 1С"
        messages.success(request, f"Обработано расходов: {changed_count}; отмечены как {label}.")
    else:
        messages.success(request, "Выбранные расходы уже имеют нужную отметку 1С.")
    return redirect(report_return_url)


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

    client_value = request.GET.get("client", "").strip()
    return {
        "employee": optional_id("employee"),
        "category": optional_id("category"),
        "client": optional_id("client"),
        "destination": Expense.DESTINATION_OFFICE if client_value == Expense.DESTINATION_OFFICE else "",
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
    report_return_filters = _normalized_expense_report_filters(request.GET, organization)
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
    expense_report_return_fields = [
        ("return_to", "expense_report"),
        *[(f"report_{name}", value) for name, value in report_return_filters.items()],
    ]
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
            "expense_report_return_fields": expense_report_return_fields,
            "expense_report_return_query": urlencode(expense_report_return_fields),
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
    organization, denied = _onec_import_list_guard(request)
    if denied:
        return denied
    can_profit = can_import_gross_profit(request.user, organization)
    can_cashflow = can_import_cashflow(request.user, organization)
    batches = OneCImportBatch.objects.filter(
        organization=organization,
        import_type__in=_onec_accessible_import_types(request.user, organization),
        sync_run__isnull=True,
    ).select_related("uploaded_by")
    show_cashflow_odata = (
        is_odata_target_organization(organization)
        and can_cashflow
    )
    report_types = _onec_sync_report_types(request.user, organization)
    latest_active = {
        report_type: OneCReportPeriodState.objects.filter(
            organization=organization, report_type=report_type,
            active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
        ).order_by("-period_month").values_list("period_month", flat=True).first()
        for report_type in report_types
    }
    sync_runs = OneCODataSyncRun.objects.filter(
        organization=organization, mode=OneCODataSyncRun.MODE_PREVIEW,
    ).select_related("requested_by")[:20]
    auto_runs = OneCODataSyncRun.objects.filter(
        organization=organization, mode=OneCODataSyncRun.MODE_AUTO_APPLY,
    ).select_related("requested_by")[:20]
    return render(request, "pool_service/finance/onec_import_list.html", {
        "batches": batches,
        "show_profit_import": can_profit,
        "show_cashflow_odata_draft": show_cashflow_odata,
        "show_unified_sync": bool(report_types) and is_odata_target_organization(organization),
        "sync_report_types": report_types,
        "latest_profit_active": latest_active.get(REPORT_PROFIT),
        "latest_cashflow_active": latest_active.get(REPORT_CASHFLOW),
        "sync_runs": sync_runs,
        "auto_runs": auto_runs,
        "active_tab": "finance",
    })


@require_POST
@login_required
def finance_onec_odata_sync_start(request):
    organization, denied = _onec_sync_guard(request)
    if denied:
        return denied
    report_types = _onec_sync_report_types(request.user, organization)
    try:
        run, created = start_unified_sync(organization, request.user, report_types)
    except PermissionDenied:
        return JsonResponse({"error_code": "permission_denied", "error": "Недостаточно прав."}, status=403)
    except ValidationError:
        return JsonResponse({"error_code": "invalid_sync_request", "error": "Не удалось начать проверку данных 1С."}, status=400)
    payload = {
        "run_id": str(run.id), "created": created, "status": run.status,
        "cursor": run.cursor.get("version", 0),
        "step_url": reverse("finance_onec_odata_sync_step", kwargs={"run_id": run.id}),
        "status_url": reverse("finance_onec_odata_sync_status", kwargs={"run_id": run.id}),
    }
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(payload)
    messages.success(request, "Проверка создана. Нажмите «Продолжить проверку», чтобы получить следующий фрагмент.")
    return redirect("finance_onec_import_list")


def _sync_run_payload(run, allowed_report_types):
    allowed = set(allowed_report_types)
    visible_result = {
        report_type: item for report_type, item in run.result_summary.items()
        if report_type in allowed
    }
    visible_scope = {
        report_type: item for report_type, item in run.sync_scope.items()
        if report_type in allowed
    }
    draft_ids = sum((item.get("drafts", []) for item in visible_result.values()), [])
    candidate_ids = [
        candidate["candidate_batch_id"]
        for item in visible_result.values()
        for candidate in item.get("reactivation_candidates", [])
    ]
    links = {}
    for batch in OneCImportBatch.objects.filter(id__in=draft_ids):
        route = "finance_onec_cashflow_preview" if batch.import_type == REPORT_CASHFLOW else "finance_onec_import_preview"
        links[str(batch.id)] = reverse(route, kwargs={"batch_id": batch.id})
    candidate_links = {}
    for batch in OneCImportBatch.objects.filter(id__in=candidate_ids):
        route = "finance_onec_cashflow_detail" if batch.import_type == REPORT_CASHFLOW else "finance_onec_import_detail"
        candidate_links[str(batch.id)] = reverse(route, kwargs={"batch_id": batch.id})
    return {
        "run_id": str(run.id), "status": run.status, "scope": visible_scope,
        "cursor": run.cursor.get("version", 0), "progress": run.progress,
        "result": visible_result, "draft_links": links, "candidate_links": candidate_links,
    }


@require_POST
@login_required
def finance_onec_odata_sync_step(request, run_id):
    organization, denied = _onec_sync_guard(request)
    if denied:
        return denied
    run = get_object_or_404(
        OneCODataSyncRun, pk=run_id, organization=organization,
        mode=OneCODataSyncRun.MODE_PREVIEW,
    )
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def reject(message, status=409):
        if wants_json:
            return JsonResponse({"error_code": "invalid_sync_cursor", "error": message}, status=status)
        messages.warning(request, message)
        return redirect("finance_onec_import_list")

    try:
        expected = int(request.POST.get("cursor", ""))
    except (TypeError, ValueError):
        return reject("Не удалось продолжить проверку: некорректная версия шага.", 400)
    persisted_cursor = int((run.cursor or {}).get("version", 0))
    if expected != persisted_cursor:
        return reject("Этот шаг проверки уже устарел. Используйте актуальную кнопку продолжения.")
    if run.status in OneCODataSyncRun.TERMINAL_STATUSES:
        if wants_json:
            return JsonResponse(_sync_run_payload(
                run, _onec_sync_report_types(request.user, organization)
            ))
        messages.info(request, "Проверка уже завершена.")
        return redirect("finance_onec_import_list")
    try:
        run = step_unified_sync(
            run.id, request.user,
            _onec_sync_report_types(request.user, organization), expected,
        )
    except PermissionDenied:
        if wants_json:
            return JsonResponse({"error_code": "permission_denied", "error": "Недостаточно прав."}, status=403)
        return HttpResponseForbidden("Недостаточно прав.")
    if not wants_json:
        if run.progress.get("step_state") == "busy":
            messages.info(request, "Этот шаг уже выполняется. Повторите попытку позже.")
        elif run.status in OneCODataSyncRun.TERMINAL_STATUSES:
            messages.success(request, "Проверка данных завершена.")
        else:
            messages.success(request, "Один шаг проверки выполнен. Продолжите проверку.")
        return redirect("finance_onec_import_list")
    return JsonResponse(_sync_run_payload(run, _onec_sync_report_types(request.user, organization)))


@login_required
def finance_onec_odata_sync_status(request, run_id):
    organization, denied = _onec_import_list_guard(request)
    if denied:
        return denied
    run = get_object_or_404(
        OneCODataSyncRun, pk=run_id, organization=organization,
        mode=OneCODataSyncRun.MODE_PREVIEW,
    )
    allowed = _onec_sync_report_types(request.user, organization)
    if not set(run.requested_report_types) & set(allowed):
        return HttpResponseForbidden("Недостаточно прав.")
    return JsonResponse(_sync_run_payload(run, allowed))


@require_POST
@login_required
def finance_onec_odata_sync_reactivate(request, run_id):
    organization, denied = _onec_sync_guard(request)
    if denied:
        return denied
    get_object_or_404(
        OneCODataSyncRun, pk=run_id, organization=organization,
        mode=OneCODataSyncRun.MODE_PREVIEW,
    )
    report_type = request.POST.get("report_type", "")
    if report_type not in _onec_sync_report_types(request.user, organization):
        return HttpResponseForbidden("Недостаточно прав.")
    try:
        reactivate_confirmed_candidate(
            run_id, organization, request.user, report_type,
            request.POST.get("month", ""), request.POST.get("batch_id", ""),
            request.POST.get("fingerprint", ""),
        )
    except StaleReactivationError:
        return HttpResponse(
            "Активная версия месяца изменилась после проверки. Запустите обновление из 1С повторно",
            status=409,
        )
    except PermissionDenied:
        return HttpResponseForbidden("Недостаточно прав.")
    except (ValidationError, ValueError, OneCImportBatch.DoesNotExist, OneCODataSyncRun.DoesNotExist):
        messages.error(request, "Не удалось активировать выбранную подтверждённую версию.")
    else:
        messages.success(request, "Выбранная подтверждённая версия месяца активирована.")
    return redirect("finance_onec_import_list")


def _parse_auto_month(value, fallback):
    if not value:
        return fallback
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError) as exc:
        raise ValidationError("Некорректный месяц периода.") from exc
    return parsed


def _auto_run_payload(run):
    scopes = {
        report_type: {
            "start": scope.get("start"),
            "end": scope.get("end"),
        }
        for report_type, scope in (run.sync_scope or {}).items()
        if report_type in {REPORT_PROFIT, REPORT_CASHFLOW}
    }
    changed = {
        report_type: len(item.get("changed_months", []))
        for report_type, item in (run.result_summary or {}).items()
        if report_type in {REPORT_PROFIT, REPORT_CASHFLOW}
    }
    return {
        "run_id": str(run.id),
        "status": run.status,
        "scope": scopes,
        "cursor": (run.cursor or {}).get("version", 0),
        "progress": {
            key: value for key, value in (run.progress or {}).items()
            if key in {"completed_chunks", "total_chunks", "step_state", "outcome", "applied_batches"}
        },
        "changed_month_counts": changed,
        "message": run.error_message if run.status == OneCODataSyncRun.STATUS_FAILED else "",
    }


@require_POST
@login_required
def finance_onec_refresh_apply_start(request):
    organization, denied = _onec_sync_guard(request)
    if denied:
        return denied
    report_types = _onec_sync_report_types(request.user, organization)
    current = timezone.localdate().replace(day=1)
    try:
        end = _parse_auto_month(request.POST.get("period_end"), current)
        start = _parse_auto_month(
            request.POST.get("period_start"),
            date(end.year - (1 if end.month < 12 else 0), (end.month % 12) + 1, 1),
        )
        run, created = start_unified_sync(
            organization, request.user, report_types,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            period_start=start, period_end=end,
        )
    except SyncConflictError:
        return JsonResponse({"error_code": "sync_conflict", "error": "Уже выполняется другое обновление данных 1С."}, status=409)
    except PermissionDenied:
        return JsonResponse({"error_code": "permission_denied", "error": "Недостаточно прав."}, status=403)
    except ValidationError:
        return JsonResponse({"error_code": "invalid_period", "error": "Проверьте период: допускается не более 24 полных месяцев."}, status=400)
    payload = _auto_run_payload(run)
    payload.update({
        "created": created,
        "step_url": reverse("finance_onec_refresh_apply_step", kwargs={"run_id": run.id}),
        "status_url": reverse("finance_onec_refresh_apply_status", kwargs={"run_id": run.id}),
        "detail_url": reverse("finance_onec_refresh_apply_detail", kwargs={"run_id": run.id}),
    })
    return JsonResponse(payload)


@require_POST
@login_required
def finance_onec_refresh_apply_step(request, run_id):
    organization, denied = _onec_sync_guard(request)
    if denied:
        return denied
    run = get_object_or_404(
        OneCODataSyncRun, pk=run_id, organization=organization,
        mode=OneCODataSyncRun.MODE_AUTO_APPLY,
    )
    try:
        expected = int(request.POST.get("cursor", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error_code": "invalid_cursor", "error": "Некорректная версия шага."}, status=400)
    if expected != int((run.cursor or {}).get("version", 0)):
        return JsonResponse({"error_code": "stale_cursor", "error": "Шаг обновления устарел."}, status=409)
    if run.status not in OneCODataSyncRun.TERMINAL_STATUSES:
        try:
            run = step_unified_sync(
                run.id, request.user, _onec_sync_report_types(request.user, organization), expected,
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        except PermissionDenied:
            return JsonResponse({"error_code": "permission_denied", "error": "Недостаточно прав."}, status=403)
    return JsonResponse(_auto_run_payload(run))


@login_required
def finance_onec_refresh_apply_status(request, run_id):
    organization, denied = _onec_import_list_guard(request)
    if denied:
        return denied
    run = get_object_or_404(
        OneCODataSyncRun, pk=run_id, organization=organization,
        mode=OneCODataSyncRun.MODE_AUTO_APPLY,
    )
    allowed = set(_onec_sync_report_types(request.user, organization))
    if not set(run.requested_report_types).issubset(allowed):
        return HttpResponseForbidden("Недостаточно прав.")
    return JsonResponse(_auto_run_payload(run))


@login_required
def finance_onec_refresh_apply_detail(request, run_id):
    return finance_onec_refresh_apply_status(request, run_id)


@require_POST
@login_required
def finance_onec_odata_profit_draft(request):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    form = ODataProfitDraftForm(request.POST)
    if not form.is_valid():
        batches = OneCImportBatch.objects.filter(
            organization=organization,
            import_type__in=_onec_accessible_import_types(request.user, organization),
            sync_run__isnull=True,
        ).select_related("uploaded_by")
        return render(request, "pool_service/finance/onec_import_list.html", {
            "batches": batches,
            "odata_form": form,
            "show_odata_draft": is_odata_target_organization(organization),
            "active_tab": "finance",
        }, status=400)
    start_month = form.cleaned_data["start_month"].strftime("%Y-%m")
    end_month = form.cleaned_data["end_month"].strftime("%Y-%m")
    try:
        batch = create_odata_profit_draft(
            start_month, end_month, organization, request.user
        )
    except ODataDraftError as exc:
        messages.error(request, "; ".join(exc.messages))
        if exc.batch:
            return redirect("finance_onec_import_preview", batch_id=exc.batch.id)
        return redirect("finance_onec_import_list")
    except ODataPreviewError as exc:
        messages.error(request, str(exc))
        return redirect("finance_onec_import_list")
    messages.success(request, "Черновик OData создан. Активные данные не изменены.")
    return redirect("finance_onec_import_preview", batch_id=batch.id)


@require_POST
@login_required
def finance_onec_odata_cashflow_draft(request):
    organization, denied = _onec_cashflow_import_guard(request)
    if denied:
        return denied
    form = ODataCashFlowDraftForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(
            message for messages_list in form.errors.values()
            for message in messages_list
        ))
        return redirect("finance_onec_import_list")
    try:
        batch = create_odata_cashflow_draft(
            form.cleaned_data["start_month"].strftime("%Y-%m"),
            form.cleaned_data["end_month"].strftime("%Y-%m"),
            organization,
            request.user,
        )
    except (ODataCashFlowDraftError, ODataPreviewError) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        failed_batch = getattr(exc, "batch", None)
        if failed_batch:
            return redirect("finance_onec_cashflow_preview", batch_id=failed_batch.id)
        return redirect("finance_onec_import_list")
    messages.success(request, "Черновик ДДС создан. Активные данные не изменены.")
    return redirect("finance_onec_cashflow_preview", batch_id=batch.id)


def _cashflow_batch_context(batch, user, organization):
    metadata = batch.metadata or {}
    report = metadata.get("report", {})
    totals = metadata.get("totals") or report.get("control_totals", {})
    return {
        "batch": batch,
        "report": report,
        "totals": totals,
        "monthly": metadata.get("monthly", []),
        "articles": metadata.get("articles", []),
        "preview": metadata.get("preview", []),
        "warnings": metadata.get("warnings", []),
        "warnings_hidden": metadata.get("warnings_hidden", 0),
        "critical_errors": metadata.get("critical_errors", []),
        "overlap_months": metadata.get("overlap_months", []),
        "overlap_count": metadata.get("overlap_count", 0),
        "can_confirm": (
            can_import_cashflow(user, organization)
            and batch.status == OneCImportBatch.STATUS_PREVIEWED
            and not metadata.get("critical_errors")
        ),
        "can_cancel": (
            can_import_cashflow(user, organization)
            and batch.status in (
                OneCImportBatch.STATUS_PREVIEWED,
                OneCImportBatch.STATUS_FAILED,
            )
        ),
        "active_tab": "finance",
    }


@login_required
def finance_onec_cashflow_preview(request, batch_id):
    organization, denied = _onec_cashflow_access_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        sync_run__isnull=True,
    )
    return render(
        request,
        "pool_service/finance/onec_cashflow_preview.html",
        _cashflow_batch_context(batch, request.user, organization),
    )


@login_required
def finance_onec_cashflow_detail(request, batch_id):
    organization, denied = _onec_cashflow_access_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        sync_run__isnull=True,
    )
    return render(
        request,
        "pool_service/finance/onec_cashflow_preview.html",
        _cashflow_batch_context(batch, request.user, organization),
    )


@require_POST
@login_required
def finance_onec_cashflow_confirm(request, batch_id):
    organization, denied = _onec_cashflow_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        sync_run__isnull=True,
    )
    try:
        if batch.source_type == OneCImportBatch.SOURCE_ODATA:
            confirm_odata_cashflow(batch.id, organization, request.user)
        else:
            confirm_cashflow(batch.id, organization, request.user)
    except Exception:
        messages.error(request, "Импорт ДДС не выполнен. Частичные данные не сохранены.")
        return redirect("finance_onec_cashflow_preview", batch_id=batch.id)
    messages.success(request, "Черновик ДДС подтверждён.")
    if batch.source_type == OneCImportBatch.SOURCE_ODATA:
        return redirect("finance_onec_cashflow_dashboard")
    return redirect("finance_onec_cashflow_detail", batch_id=batch.id)


@require_POST
@login_required
def finance_onec_cashflow_cancel(request, batch_id):
    organization, denied = _onec_cashflow_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        sync_run__isnull=True,
    )
    try:
        cancel_onec_import_batch(batch, request.user)
    except ValidationError:
        messages.error(request, "Подтверждённый импорт отменить нельзя.")
    else:
        messages.success(request, "Черновик ДДС отменён.")
    return redirect("finance_onec_import_list")


@login_required
def finance_onec_cashflow_dashboard(request):
    organization, denied = _capability_guard(request, can_view_cashflow)
    if denied:
        return denied
    period_from = period_to = None
    period_error = ""
    if request.GET.get("period_from") or request.GET.get("period_to"):
        try:
            period_from, period_to = parse_payroll_period(
                request.GET.get("period_from"), request.GET.get("period_to")
            )
        except ValueError as exc:
            period_error = str(exc)
    data = cashflow_dashboard_data(organization, period_from, period_to)
    article_trend = cashflow_article_trend_data(
        organization,
        period_from,
        period_to,
        mode=request.GET.get("article_mode", "all"),
        selected_articles=request.GET.getlist("article"),
    )
    return render(request, "pool_service/finance/onec_cashflow_dashboard.html", {
        **data,
        "article_trend": article_trend,
        "period_from": period_from,
        "period_to": period_to,
        "period_error": period_error,
        "active_tab": "finance",
    })


def _payroll_access(request, permission):
    organization = _organization_for_finance(request)
    if not organization:
        return None, HttpResponseForbidden("Организация не найдена.")
    if not permission(request.user, organization):
        return organization, HttpResponseForbidden("Недостаточно прав для раздела ФОТ.")
    if is_org_access_blocked(request.user):
        messages.error(request, "Доступ организации к сервису приостановлен.")
        return organization, redirect("billing")
    return organization, None


@login_required
def finance_payroll_dashboard(request):
    organization, denied = _payroll_access(request, can_view_payroll_summary)
    if denied:
        return denied
    error = ""
    try:
        period_from, period_to = parse_payroll_period(
            request.GET.get("period_from"), request.GET.get("period_to")
        )
    except ValueError as exc:
        error = str(exc)
        period_from, period_to = parse_payroll_period(None, None)
    show_personal = can_view_payroll_personal(request.user, organization)
    data = payroll_dashboard_data(
        organization, period_from, period_to, include_personal=show_personal
    )
    unresolved_count = None
    mapping_access = can_manage_employee_mapping(request.user, organization)
    if mapping_access:
        unresolved_count = unresolved_active_payroll_identity_count(organization)
    return render(request, "pool_service/finance/payroll_dashboard.html", {
        "data": data,
        "period_from": period_from,
        "period_to": period_to,
        "period_error": error,
        "show_personal": show_personal,
        "can_import_payroll": can_import_payroll(request.user, organization),
        "can_manage_mapping": mapping_access,
        "unresolved_count": unresolved_count,
        "active_tab": "finance",
    })


@login_required
def finance_payroll_import_list(request):
    organization, denied = _payroll_access(request, can_import_payroll)
    if denied:
        return denied
    batches = OneCImportBatch.objects.filter(
        organization=organization, import_type=OneCImportBatch.TYPE_PAYROLL
    ).select_related("uploaded_by").prefetch_related("active_period_states")
    return render(request, "pool_service/finance/payroll_import_list.html", {
        "batches": batches,
        "active_tab": "finance",
    })


@login_required
def finance_payroll_import_upload(request):
    organization, denied = _payroll_access(request, can_import_payroll)
    if denied:
        return denied
    form = PayrollUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = create_payroll_preview(form.cleaned_data["report"], organization, request.user)
        except DuplicateImportError as exc:
            messages.error(request, "Этот файл уже загружался. Откройте существующий импорт.")
            return redirect("finance_payroll_import_preview", batch_id=exc.batch.id)
        except (ValidationError, ValueError) as exc:
            form.add_error("report", getattr(exc, "messages", [str(exc)]))
        else:
            return redirect("finance_payroll_import_preview", batch_id=batch.id)
    return render(request, "pool_service/finance/payroll_import_upload.html", {
        "form": form, "active_tab": "finance",
    })


@login_required
def finance_payroll_import_preview(request, batch_id):
    organization, denied = _payroll_access(request, can_import_payroll)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch, pk=batch_id, organization=organization,
        import_type=OneCImportBatch.TYPE_PAYROLL,
    )
    metadata = batch.metadata or {}
    show_personal = can_view_payroll_personal(request.user, organization)
    parser_version_current = batch.parser_version == PAYROLL_PARSER_VERSION
    confirmation_state = payroll_confirmation_state(batch)
    return render(request, "pool_service/finance/payroll_import_preview.html", {
        "batch": batch,
        "report": metadata.get("report", {}),
        "summary": metadata.get("payroll_summary", {}),
        "preview": metadata.get("preview", []) if show_personal else [],
        "show_personal": show_personal,
        "warnings": metadata.get("warnings", []),
        "critical_errors": metadata.get("critical_errors", []),
        "overlap_months": metadata.get("overlap_months", []),
        "parser_version_current": parser_version_current,
        "can_confirm": confirmation_state["can_confirm"],
        "confirmation_state": confirmation_state,
        "active_tab": "finance",
    })


@login_required
def finance_payroll_import_confirm(request, batch_id):
    organization, denied = _payroll_access(request, can_import_payroll)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch, pk=batch_id, organization=organization,
        import_type=OneCImportBatch.TYPE_PAYROLL,
    )
    metadata = batch.metadata or {}
    confirmation_state = payroll_confirmation_state(batch)
    form = PayrollConfirmForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        remote_ip = request.META.get("REMOTE_ADDR") or None
        try:
            if remote_ip:
                validate_ipv46_address(remote_ip)
        except ValidationError:
            remote_ip = None
        audit_context = {
            "action": "payroll_confirm",
            "route": request.resolver_match.view_name if request.resolver_match else "",
            "remote_ip": remote_ip,
            "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:512],
            "request_timestamp": timezone.now().isoformat(),
        }
        try:
            confirm_payroll(
                batch_id, organization, request.user, audit_context=audit_context
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            batch.refresh_from_db()
            metadata = batch.metadata or {}
            confirmation_state = payroll_confirmation_state(batch)
        else:
            messages.success(request, "Импорт подтверждён.")
            return redirect("finance_payroll_dashboard")
    return render(request, "pool_service/finance/payroll_import_confirm.html", {
        "batch": batch,
        "summary": metadata.get("payroll_summary", {}),
        "warnings": metadata.get("warnings", []),
        "critical_errors": metadata.get("critical_errors", []),
        "overlap_months": metadata.get("overlap_months", []),
        "parser_version_current": batch.parser_version == PAYROLL_PARSER_VERSION,
        "confirmation_state": confirmation_state,
        "form": form,
        "active_tab": "finance",
    })


@login_required
def finance_payroll_employee_mapping(request):
    organization, denied = _payroll_access(request, can_manage_employee_mapping)
    if denied:
        return denied
    employees = Employee.objects.filter(organization=organization, is_active=True).order_by("display_name", "id")
    return render(request, "pool_service/finance/payroll_employee_mapping.html", {
        "identities": payroll_identity_rows(organization),
        "employees": employees,
        "employee_count": employees.count(),
        "active_tab": "finance",
    })


@require_POST
@login_required
def finance_payroll_employee_map(request, identity_id):
    organization, denied = _payroll_access(request, can_manage_employee_mapping)
    if denied:
        return denied
    identity = get_object_or_404(
        EmployeeOneCIdentity, pk=identity_id, organization=organization
    )
    form = EmployeeIdentityMappingForm(request.POST, organization=organization)
    if not form.is_valid():
        messages.error(request, "Выберите существующего сотрудника этой организации.")
        return redirect("finance_payroll_employee_mapping")
    try:
        confirm_employee_identity(
            identity, form.cleaned_data["employee"], request.user,
            comment=form.cleaned_data["comment"],
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Сопоставление сотрудника сохранено.")
    return redirect("finance_payroll_employee_mapping")


@login_required
def finance_onec_profit_dashboard(request):
    organization, denied = _capability_guard(
        request, can_view_gross_profit,
        denied_message="Недостаточно прав для аналитики валовой прибыли.",
    )
    if denied:
        return denied
    period = resolve_period(request.GET)
    manager_values = (
        OneCMonthlyProfit.objects.active_for(organization)
        .exclude(manager_name="")
        .order_by()
        .values_list("manager_name", flat=True)
        .distinct()
    )
    managers_by_key = {}
    for value in manager_values:
        display = " ".join((value or "").split())
        key = _manager_key(display)
        if key:
            managers_by_key.setdefault(key, display)
    managers = sorted(managers_by_key.values(), key=str.casefold)
    requested_manager = request.GET.get("manager", "")
    manager = managers_by_key.get(_manager_key(requested_manager), "")
    data = dashboard_data(organization, period, manager=manager)

    sort = request.GET.get("sort", "-revenue")
    sort_fields = {
        "revenue": "revenue",
        "cost": "cost",
        "gross_profit": "gross_profit",
        "profitability": "profitability",
    }
    sort_name = sort.lstrip("-")
    if sort_name not in sort_fields:
        sort, sort_name = "-revenue", "revenue"
    sort_field = sort_fields[sort_name]
    customers = data["customers"]
    customers.sort(key=lambda item: item["name"].casefold())
    valued = [item for item in customers if item[sort_field] is not None]
    empty = [item for item in customers if item[sort_field] is None]
    valued.sort(key=lambda item: item[sort_field], reverse=sort.startswith("-"))
    data["customers"] = valued + empty
    page = Paginator(data["customers"], 50).get_page(request.GET.get("page"))
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    filter_params.pop("sort", None)
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    return render(request, "pool_service/finance/onec_profit_dashboard.html", {
        **data, "period": period, "period_choices": PERIOD_CHOICES,
        "page_obj": page, "filter_query": filter_params.urlencode(),
        "pagination_query": pagination_params.urlencode(),
        "managers": managers, "manager": manager, "sort": sort,
        "can_import_gross_profit": can_import_gross_profit(request.user, organization),
        "active_tab": "finance",
    })


@login_required
def finance_onec_cost_control(request):
    organization, denied = _capability_guard(
        request, can_view_cost_control,
        denied_message="Недостаточно прав для контроля себестоимости.",
    )
    if denied:
        return denied

    form = OneCCostControlFilterForm(request.GET or None)
    filters_valid = not form.is_bound or form.is_valid()
    period_month = None
    search = ""
    if filters_valid and form.is_bound:
        period_month = form.cleaned_data["period"]
        search = form.cleaned_data["search"]

    if filters_valid:
        active_rows = get_onec_cost_control_dataset(
            organization,
            period_month=period_month,
        )
        anomaly_rows = get_onec_cost_anomalies(
            organization,
            period_month=period_month,
            search=search,
        )
    else:
        active_rows = OneCMonthlyProfit.objects.none()
        anomaly_rows = OneCMonthlyProfit.objects.none()

    page = Paginator(anomaly_rows, 50).get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(request, "pool_service/finance/onec_cost_control.html", {
        "form": form,
        "dataset_summary": summarize_active_dataset(active_rows),
        "anomaly_summary": summarize_cost_anomalies(anomaly_rows),
        "monthly_summary": monthly_cost_anomaly_summary(anomaly_rows),
        "available_months": available_cost_control_months(organization),
        "page_obj": page,
        "filter_query": query_params.urlencode(),
        "can_import_gross_profit": can_import_gross_profit(request.user, organization),
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
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        sync_run__isnull=True,
    )
    return render(request, "pool_service/finance/onec_import_preview.html", {
        "batch": batch,
        "preview": batch.metadata.get("preview", []),
        "report": batch.metadata.get("report", {}),
        "totals": batch.metadata.get("totals", {}),
        "warnings": batch.metadata.get("warnings", []),
        "warnings_hidden": batch.metadata.get("warnings_hidden", 0),
        "critical_errors": batch.metadata.get("critical_errors", []),
        "overlap_months": batch.metadata.get("overlap_months", []),
        "overlap_count": batch.metadata.get("overlap_count", 0),
        "monthly_comparison": batch.metadata.get("monthly", []),
        "can_confirm": batch.status == OneCImportBatch.STATUS_PREVIEWED and not batch.metadata.get("critical_errors"),
        "active_tab": "finance",
    })


@require_POST
@login_required
def finance_onec_import_confirm(request, batch_id):
    organization, denied = _onec_import_guard(request)
    if denied:
        return denied
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        sync_run__isnull=True,
    )
    if batch.status != OneCImportBatch.STATUS_PREVIEWED:
        messages.error(request, "Этот импорт уже обработан или недоступен для подтверждения.")
        return redirect("finance_onec_import_detail", batch_id=batch.id)
    try:
        if batch.source_type == OneCImportBatch.SOURCE_ODATA:
            batch = confirm_odata_profit(batch.id, organization, request.user)
        else:
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
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        sync_run__isnull=True,
    )
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
    batch = get_object_or_404(
        OneCImportBatch,
        id=batch_id,
        organization=organization,
        import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        sync_run__isnull=True,
    )
    rows = OneCMonthlyProfit.objects.filter(import_batch=batch, organization=organization)
    money_field = DecimalField(max_digits=20, decimal_places=2)
    analytical_cost = Case(
        When(cost_source=OneCMonthlyProfit.COST_SOURCE_CALCULATED, then=F("calculated_cost")),
        When(cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED, then=Value(None)),
        default=F("cost"), output_field=money_field,
    )
    analytical_profit = Case(
        When(cost_source="", then=F("gross_profit")),
        default=F("analytical_gross_profit"), output_field=money_field,
    )
    totals = rows.aggregate(
        revenue=Sum("revenue"), cost=Sum(analytical_cost), gross_profit=Sum(analytical_profit)
    )
    revenue = totals["revenue"] or Decimal("0")
    gross_profit = totals["gross_profit"] or Decimal("0")
    totals["profitability_percent"] = calculate_profitability(gross_profit, revenue)
    monthly = list(rows.values("period_month").annotate(
        revenue=Sum("revenue"), cost=Sum(analytical_cost), gross_profit=Sum(analytical_profit)
    ).order_by("period_month"))
    for item in monthly:
        month_revenue = item["revenue"] or Decimal("0")
        item["profitability_percent"] = calculate_profitability(item["gross_profit"], month_revenue)
    page = Paginator(rows.order_by("period_month", "source_row_number"), 50).get_page(request.GET.get("page"))
    batch_months = set(rows.values_list("period_month", flat=True).distinct())
    active_months = set(
        batch.active_period_states.filter(organization=organization).values_list(
            "period_month", flat=True
        )
    )
    return render(request, "pool_service/finance/onec_import_detail.html", {
        "batch": batch, "totals": totals, "monthly": monthly, "page_obj": page,
        "active_month_count": len(batch_months & active_months),
        "replaced_month_count": len(batch_months - active_months),
        "active_tab": "finance",
    })
