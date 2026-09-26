from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from pool_service.models import (
    Employee,
    EmployeeOneCUserIdentity,
    EmployeeRewardAdjustment,
    EmployeeRewardAssignment,
    EmployeeRewardRule,
    EmployeeRewardScheme,
)
from pool_service.services.employee_rewards import (
    active_scheme,
    close_reward_month,
    confirm_adjustment,
    confirm_assignments,
    confirmation_preview,
    create_adjustment,
    create_or_update_assignment,
    create_scheme_version,
    document_rows,
    map_onec_author,
    reward_dashboard_data,
)
from pool_service.services.finance import (
    can_close_reward_month,
    can_manage_employee_rewards,
    can_propose_employee_rewards,
    can_manage_reward_rules,
    can_view_employee_reward_detail,
    can_view_employee_rewards,
)
from pool_service.services.permissions import organization_for_user


def _organization(request):
    organization = organization_for_user(request.user)
    if organization is None:
        raise PermissionDenied
    return organization


def _month(value):
    if not value:
        today = date.today()
        return today.replace(day=1)
    try:
        parsed = date.fromisoformat(f"{value}-01" if len(value) == 7 else value)
    except (TypeError, ValueError):
        raise ValidationError("Некорректный месяц.")
    return parsed.replace(day=1)


def _validation_message(exc):
    if hasattr(exc, "messages"):
        return " ".join(str(item) for item in exc.messages)
    return str(exc)


def _employee_queryset(organization):
    return Employee.objects.filter(
        organization=organization,
        is_active=True,
    ).select_related("user").order_by("display_name", "id")


@login_required
def reward_dashboard(request):
    organization = _organization(request)
    if not can_view_employee_rewards(request.user, organization):
        raise PermissionDenied
    try:
        period_month = _month(request.GET.get("month"))
    except ValidationError as exc:
        messages.warning(request, _validation_message(exc))
        period_month = date.today().replace(day=1)

    employee_id = request.GET.get("employee")
    employee = None
    if employee_id:
        employee = get_object_or_404(_employee_queryset(organization), pk=employee_id)

    data = reward_dashboard_data(
        organization,
        period_month,
        employee_id=employee.id if employee else None,
    )
    return render(
        request,
        "pool_service/finance/rewards_dashboard.html",
        {
            **data,
            "selected_employee": employee,
            "employees": _employee_queryset(organization),
            "can_manage_rewards": can_manage_employee_rewards(request.user, organization),
            "can_manage_rules": can_manage_reward_rules(request.user, organization),
            "can_close_month": can_close_reward_month(request.user, organization),
        },
    )


@login_required
def reward_employee_detail(request, employee_id):
    organization = _organization(request)
    employee = get_object_or_404(
        Employee.objects.select_related("user"),
        pk=employee_id,
        organization=organization,
    )
    if not can_view_employee_reward_detail(request.user, organization, employee):
        raise PermissionDenied
    try:
        period_month = _month(request.GET.get("month"))
    except ValidationError as exc:
        messages.warning(request, _validation_message(exc))
        period_month = date.today().replace(day=1)
    data = reward_dashboard_data(organization, period_month, employee_id=employee.id)
    row = next((item for item in data["rows"] if item["employee_id"] == employee.id), None)
    return render(
        request,
        "pool_service/finance/reward_employee_detail.html",
        {
            **data,
            "employee": employee,
            "reward_row": row,
            "can_view_all_rewards": can_view_employee_rewards(request.user, organization),
        },
    )


@login_required
def reward_document(request, document_type, document_guid):
    organization = _organization(request)
    can_manage = can_manage_employee_rewards(request.user, organization)
    can_propose = can_propose_employee_rewards(request.user, organization)
    if not (can_manage or can_propose):
        raise PermissionDenied
    try:
        period_month = _month(request.GET.get("month"))
    except ValidationError:
        raise Http404("Некорректный месяц.")
    rows = document_rows(
        organization, period_month, document_type, str(document_guid)
    )
    if not rows:
        raise Http404("Документ не найден в активных подтверждённых данных.")

    assignment_qs = EmployeeRewardAssignment.objects.filter(
        organization=organization,
        period_month=period_month,
        source_document_type=document_type,
        source_document_guid=document_guid,
    )
    own_employee = None
    if not can_manage:
        own_employee = Employee.objects.filter(
            organization=organization,
            user=request.user,
            is_active=True,
        ).first()
        if own_employee is None:
            raise PermissionDenied
        assignment_qs = assignment_qs.filter(employee=own_employee)
    assignments = list(
        assignment_qs.select_related("employee")
        .prefetch_related("lines")
        .order_by("role", "scope_key", "employee__display_name", "id")
    )
    return render(
        request,
        "pool_service/finance/reward_document.html",
        {
            "period_month": period_month,
            "document_type": document_type,
            "document_guid": document_guid,
            "document_label": assignments[0].source_document_label if assignments else rows[0].document_name,
            "rows": rows,
            "assignments": assignments,
            "employees": (
                _employee_queryset(organization)
                if can_manage
                else Employee.objects.filter(pk=own_employee.pk)
            ),
            "role_choices": EmployeeRewardRule.ROLE_CHOICES,
            "can_manage_rewards": can_manage,
            "show_financial_basis": can_manage,
            "is_closed": reward_dashboard_data(organization, period_month)["is_closed"],
        },
    )


@login_required
@require_POST
def reward_assignment_save(request, document_type, document_guid):
    organization = _organization(request)
    can_manage = can_manage_employee_rewards(request.user, organization)
    can_propose = can_propose_employee_rewards(request.user, organization)
    if not (can_manage or can_propose):
        raise PermissionDenied
    next_url = reverse(
        "finance_reward_document",
        kwargs={"document_type": document_type, "document_guid": document_guid},
    )
    try:
        period_month = _month(request.POST.get("month"))
        if can_manage:
            employee = get_object_or_404(
                _employee_queryset(organization),
                pk=request.POST.get("employee"),
            )
        else:
            employee = get_object_or_404(
                Employee,
                organization=organization,
                user=request.user,
                is_active=True,
            )
        role = request.POST.get("role")
        if role not in dict(EmployeeRewardRule.ROLE_CHOICES):
            raise ValidationError("Некорректная роль.")
        line_ids = request.POST.getlist("line")
        create_or_update_assignment(
            organization=organization,
            period_month=period_month,
            document_type=document_type,
            document_guid=document_guid,
            role=role,
            employee=employee,
            share_percent=request.POST.get("share_percent"),
            actor=request.user,
            line_identities=line_ids,
            confirm=can_manage and request.POST.get("confirm") == "1",
            basis_note=request.POST.get("basis_note", ""),
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(request, "Участие сохранено в тестовом контуре.")
    return redirect(f"{next_url}?month={request.POST.get('month', '')}")


@login_required
@require_POST
def reward_confirmation_preview(request):
    organization = _organization(request)
    if not can_manage_employee_rewards(request.user, organization):
        raise PermissionDenied
    raw_ids = request.POST.getlist("assignment")
    try:
        ids = [int(value) for value in raw_ids]
        assignments, warnings = confirmation_preview(organization, ids)
    except (TypeError, ValueError, ValidationError) as exc:
        messages.error(request, _validation_message(exc))
        return redirect("finance_rewards")
    return render(
        request,
        "pool_service/finance/reward_confirm_preview.html",
        {"assignments": assignments, "warnings": warnings},
    )


@login_required
@require_POST
def reward_confirm(request):
    organization = _organization(request)
    if not can_manage_employee_rewards(request.user, organization):
        raise PermissionDenied
    try:
        ids = [int(value) for value in request.POST.getlist("assignment")]
        assignments, warnings = confirm_assignments(organization, ids, request.user)
    except (TypeError, ValueError, ValidationError) as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(
            request,
            f"Подтверждено назначений: {len(assignments)}."
            + (f" Предупреждений: {len(warnings)}." if warnings else ""),
        )
    return redirect("finance_rewards")


@login_required
def reward_author_mapping(request):
    organization = _organization(request)
    if not can_manage_employee_rewards(request.user, organization):
        raise PermissionDenied
    identities = (
        EmployeeOneCUserIdentity.objects.filter(organization=organization)
        .select_related("employee")
        .order_by("status", "display_name", "id")
    )
    return render(
        request,
        "pool_service/finance/reward_author_mapping.html",
        {
            "identities": identities,
            "employees": _employee_queryset(organization),
        },
    )


@login_required
@require_POST
def reward_author_map(request, identity_id):
    organization = _organization(request)
    if not can_manage_employee_rewards(request.user, organization):
        raise PermissionDenied
    identity = get_object_or_404(
        EmployeeOneCUserIdentity,
        pk=identity_id,
        organization=organization,
    )
    technical = request.POST.get("technical") == "1"
    employee = None
    if not technical:
        employee = get_object_or_404(
            _employee_queryset(organization),
            pk=request.POST.get("employee"),
        )
    try:
        map_onec_author(
            identity,
            employee=employee,
            technical=technical,
            actor=request.user,
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(request, "Сопоставление автора 1С сохранено.")
    return redirect("finance_reward_author_mapping")


@login_required
def reward_rules(request):
    organization = _organization(request)
    if not can_manage_reward_rules(request.user, organization):
        raise PermissionDenied
    schemes = (
        EmployeeRewardScheme.objects.filter(organization=organization)
        .prefetch_related("rules")
        .order_by("-effective_from", "-version")
    )
    return render(
        request,
        "pool_service/finance/reward_rules.html",
        {
            "schemes": schemes,
            "current_scheme": active_scheme(
                organization, date.today().replace(day=1)
            ),
        },
    )


@login_required
@require_POST
def reward_rule_version_create(request):
    organization = _organization(request)
    if not can_manage_reward_rules(request.user, organization):
        raise PermissionDenied
    try:
        effective_from = _month(request.POST.get("effective_from"))
        values = {}
        for role, unit in [
            (EmployeeRewardRule.ROLE_PAPERWORK, EmployeeRewardRule.UNIT_PAPERWORK_RETAIL),
            (EmployeeRewardRule.ROLE_PAPERWORK, EmployeeRewardRule.UNIT_PAPERWORK_PACKAGE),
            (EmployeeRewardRule.ROLE_SALE, EmployeeRewardRule.UNIT_SALE),
            (EmployeeRewardRule.ROLE_PROJECT, EmployeeRewardRule.UNIT_PROJECT),
            (EmployeeRewardRule.ROLE_WORK, EmployeeRewardRule.UNIT_WORK),
        ]:
            key = f"{role}:{unit}"
            if unit.startswith("paperwork_"):
                amount = Decimal(request.POST[f"{unit}_amount"])
                if amount < 0:
                    raise ValidationError("Фиксированная сумма не может быть отрицательной.")
                values[key] = {"fixed_amount": amount}
            else:
                rate = Decimal(request.POST[f"{unit}_rate"])
                if rate < 0:
                    raise ValidationError("Ставка не может быть отрицательной.")
                values[key] = {"rate_percent": rate}
        create_scheme_version(
            organization, effective_from, values, request.user
        )
    except (KeyError, InvalidOperation, ValidationError) as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(request, "Создана новая версия тестовой схемы.")
    return redirect("finance_reward_rules")


@login_required
@require_POST
def reward_month_close(request):
    organization = _organization(request)
    if not can_close_reward_month(request.user, organization):
        raise PermissionDenied
    try:
        period_month = _month(request.POST.get("month"))
        close_reward_month(organization, period_month, request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(
            request,
            "Месяц тестового расчёта закрыт. Результат и версия правил зафиксированы.",
        )
    return redirect(f"{reverse('finance_rewards')}?month={period_month:%Y-%m}")


@login_required
@require_POST
def reward_adjustment_create(request):
    organization = _organization(request)
    if not can_manage_employee_rewards(request.user, organization):
        raise PermissionDenied
    try:
        period_month = _month(request.POST.get("month"))
        employee = get_object_or_404(
            Employee,
            organization=organization,
            pk=request.POST.get("employee"),
        )
        create_adjustment(
            organization,
            period_month,
            employee,
            request.POST.get("amount"),
            request.POST.get("reason"),
            request.user,
        )
    except (InvalidOperation, ValidationError) as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(
            request,
            "Поздняя корректировка предложена. Она не является удержанием из зарплаты.",
        )
    return redirect(f"{reverse('finance_rewards')}?month={request.POST.get('month', '')}")


@login_required
@require_POST
def reward_adjustment_confirm(request, adjustment_id):
    organization = _organization(request)
    if not can_manage_employee_rewards(request.user, organization):
        raise PermissionDenied
    adjustment = get_object_or_404(
        EmployeeRewardAdjustment.objects.select_related("employee"),
        pk=adjustment_id,
        organization=organization,
    )
    try:
        confirm_adjustment(adjustment, request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(request, "Корректировка тестового расчёта подтверждена.")
    return redirect(
        f"{reverse('finance_rewards')}?month={adjustment.period_month:%Y-%m}"
    )
