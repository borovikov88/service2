from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from pool_service.models import Employee
from pool_service.reward_models import OneCAuthorIdentity, RewardParticipation, RewardSchemeVersion
from pool_service.services.permissions import organization_for_user
from pool_service.services.rewards import (
    calculate_month,
    add_documentation_participant,
    can_close_period,
    can_manage_participation,
    can_manage_rules,
    can_view_rewards,
    close_month,
    create_manual_participation,
    confirm_participation,
    create_scheme_version,
    ensure_test_scheme,
    map_author,
    month_start,
    reward_document_options,
    sync_author_proposals,
)


def _org(request):
    organization = organization_for_user(request.user)
    if not organization:
        raise PermissionDenied
    return organization


def _period(request):
    value = request.GET.get("month") or request.POST.get("month")
    if value:
        return month_start(value)
    return timezone.localdate().replace(day=1)


@login_required
def employee_rewards(request):
    organization = _org(request)
    if not can_view_rewards(request.user, organization):
        return render(request, "403.html", status=403)
    period_month = _period(request)
    selected_employee = request.GET.get("employee")
    employee_id = int(selected_employee) if selected_employee and selected_employee.isdigit() else None

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "ensure_scheme":
                ensure_test_scheme(organization, request.user, period_month)
                messages.success(request, "Тестовая схема создана.")
            elif action == "sync_authors":
                result = sync_author_proposals(organization, request.user, period_month)
                messages.success(request, f"Предложения по авторам: {result['created']}; требуют данных: {result['issues']}.")
            elif action == "map_author":
                identity = get_object_or_404(OneCAuthorIdentity, pk=request.POST.get("identity_id"), organization=organization)
                employee = get_object_or_404(Employee, pk=request.POST.get("employee_id"), organization=organization)
                map_author(identity, employee, request.user)
                messages.success(request, "Автор 1С сопоставлен с сотрудником.")
            elif action == "add_participation":
                employee = get_object_or_404(Employee, pk=request.POST.get("employee_id"), organization=organization)
                selected_lines = request.POST.getlist("line_identity")
                create_manual_participation(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    employee=employee,
                    role=request.POST.get("role", ""),
                    share=Decimal(request.POST.get("share_percent", "0")) / 100,
                    line_identities=selected_lines,
                )
                messages.success(request, "Участие добавлено и ожидает подтверждения.")
            elif action == "mark_not_applicable":
                create_manual_participation(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    employee=None,
                    role=request.POST.get("role", ""),
                    share=Decimal("0"),
                    line_identities=[],
                    not_applicable=True,
                )
                messages.success(request, "Роль отмечена как «не применяется».")
            elif action == "add_co_documenter":
                source = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                    role=RewardParticipation.ROLE_DOCUMENTATION,
                )
                employee = get_object_or_404(Employee, pk=request.POST.get("employee_id"), organization=organization)
                add_documentation_participant(
                    source,
                    employee,
                    Decimal(request.POST.get("share_percent", "0")) / 100,
                    request.user,
                )
                messages.success(request, "Совместный оформитель добавлен.")
            elif action == "confirm":
                if not can_manage_participation(request.user, organization):
                    raise PermissionDenied
                item = get_object_or_404(RewardParticipation, pk=request.POST.get("participation_id"), organization=organization, period_month=period_month)
                confirm_participation(item, request.user)
                messages.success(request, "Участие подтверждено.")
            elif action == "save_scheme":
                create_scheme_version(
                    organization, request.user, effective_from=period_month,
                    values={
                        "documentation_retail_fixed": request.POST["documentation_retail_fixed"],
                        "documentation_document_fixed": request.POST["documentation_document_fixed"],
                        "sale_rate": Decimal(request.POST["sale_rate"]) / 100,
                        "project_rate": Decimal(request.POST["project_rate"]) / 100,
                        "work_rate": Decimal(request.POST["work_rate"]) / 100,
                        "client_manager_rate": Decimal("0"),
                    },
                )
                messages.success(request, "Создана новая версия тестовых правил.")
            elif action == "close_month":
                close_month(organization, request.user, period_month)
                messages.success(request, "Тестовый месяц закрыт и зафиксирован.")
        except (ValidationError, ValueError, KeyError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        return redirect(f"{request.path}?month={period_month:%Y-%m}")

    data = calculate_month(organization, period_month, employee_id=employee_id)
    scheme = RewardSchemeVersion.objects.filter(organization=organization, effective_from__lte=period_month).order_by("-effective_from", "-version").first()
    participations = (
        RewardParticipation.objects.filter(organization=organization, period_month=period_month)
        .select_related("employee", "author_identity")
        .order_by("source_document_date", "source_document_number", "role", "employee__display_name")
    )
    employees = Employee.objects.filter(organization=organization, is_active=True).order_by("display_name")
    document_options = reward_document_options(organization, period_month)
    selected_document_key = request.GET.get("document_key", "")
    return render(request, "pool_service/finance/employee_rewards.html", {
        "data": data,
        "period_month": period_month,
        "employees": employees,
        "document_options": document_options,
        "selected_document_key": selected_document_key,
        "participations": participations,
        "scheme": scheme,
        "can_manage_participation": can_manage_participation(request.user, organization),
        "can_manage_rules": can_manage_rules(request.user, organization),
        "can_close_period": can_close_period(request.user, organization),
        "active_tab": "finance",
        "show_add_button": False,
    })


@login_required
def employee_reward_detail(request, employee_id):
    organization = _org(request)
    employee = get_object_or_404(Employee, pk=employee_id, organization=organization)
    is_self = employee.user_id == request.user.id
    if not is_self and not can_view_rewards(request.user, organization):
        return render(request, "403.html", status=403)
    period_month = _period(request)
    data = calculate_month(organization, period_month, employee_id=employee.id)
    return render(request, "pool_service/finance/employee_reward_detail.html", {
        "employee": employee,
        "data": data,
        "period_month": period_month,
        "active_tab": "finance",
        "show_add_button": False,
    })
