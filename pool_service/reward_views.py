from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, Employee, Pool
from pool_service.reward_models import OneCAuthorIdentity, RewardParticipantTemplate, RewardParticipation, RewardSchemeVersion
from pool_service.services.permissions import organization_for_user
from pool_service.services.rewards import (
    calculate_month,
    add_documentation_participant,
    cancel_pending_participation,
    can_close_period,
    can_manage_participation,
    can_manage_rules,
    can_view_rewards,
    close_month,
    create_manual_participation,
    confirm_participation,
    confirm_participation_batch,
    create_scheme_version,
    ensure_test_scheme,
    map_author,
    month_start,
    reward_document_options,
    resolve_documentation_placeholder,
    sync_author_proposals,
    update_participation_share,
)


def _org(request):
    organization = organization_for_user(request.user)
    if not organization:
        raise PermissionDenied
    return organization


def _percent_value(raw, label):
    text_value = str(raw or "").strip().replace(",", ".")
    try:
        value = Decimal(text_value)
    except Exception as exc:
        raise ValidationError(f"{label}: введите числовое значение.") from exc
    if not value.is_finite():
        raise ValidationError(f"{label}: введите конечное числовое значение.")
    return value / Decimal("100")


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
                messages.success(
                    request,
                    (
                        f"Предложения по авторам: {result['created']}; "
                        f"требуют данных: {result['issues']}; "
                        f"имён обновлено: {result['names_updated']}."
                    ),
                )
            elif action == "map_author":
                identity = get_object_or_404(OneCAuthorIdentity, pk=request.POST.get("identity_id"), organization=organization)
                employee = get_object_or_404(Employee, pk=request.POST.get("employee_id"), organization=organization)
                map_author(identity, employee, request.user)
                messages.success(request, "Автор 1С сопоставлен с сотрудником.")
            elif action == "create_template":
                if not can_manage_participation(request.user, organization):
                    raise PermissionDenied
                client = None
                pool = None
                employee = None
                if request.POST.get("client_id"):
                    client = get_object_or_404(Client, pk=request.POST["client_id"], organization=organization)
                if request.POST.get("pool_id"):
                    pool = get_object_or_404(Pool, pk=request.POST["pool_id"], organization=organization)
                if request.POST.get("employee_id"):
                    employee = get_object_or_404(Employee, pk=request.POST["employee_id"], organization=organization)
                is_company_client = request.POST.get("is_company_client") == "1"
                item = RewardParticipantTemplate(
                    organization=organization,
                    client=client,
                    pool=pool,
                    employee=employee,
                    role=request.POST.get("role", ""),
                    share=_percent_value(request.POST.get("share_percent"), "Доля шаблона"),
                    effective_from=period_month,
                    is_company_client=is_company_client,
                    created_by=request.user,
                )
                item.full_clean()
                item.save()
                messages.success(request, "Шаблон участников создан. Он применяется только к будущим назначениям.")
            elif action == "end_template":
                if not can_manage_participation(request.user, organization):
                    raise PermissionDenied
                item = get_object_or_404(RewardParticipantTemplate, pk=request.POST.get("template_id"), organization=organization)
                if period_month < item.effective_from.replace(day=1):
                    raise ValidationError("Нельзя завершить шаблон до даты начала его действия.")
                next_month = (period_month.replace(day=28) + timedelta(days=4)).replace(day=1)
                item.effective_to = next_month - timedelta(days=1)
                item.save(update_fields=["effective_to"])
                messages.success(request, "Шаблон завершён после выбранного месяца; история назначений не изменена.")
            elif action == "add_participation":
                employee = get_object_or_404(Employee, pk=request.POST.get("employee_id"), organization=organization)
                selected_lines = request.POST.getlist("line_identity")
                template = None
                if request.POST.get("template_id"):
                    template = get_object_or_404(
                        RewardParticipantTemplate,
                        pk=request.POST["template_id"],
                        organization=organization,
                    )
                create_manual_participation(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    employee=employee,
                    role=request.POST.get("role", ""),
                    share=_percent_value(request.POST.get("share_percent"), "Доля участия"),
                    line_identities=selected_lines,
                    assignment_source=(
                        RewardParticipation.SOURCE_TEMPLATE
                        if template else RewardParticipation.SOURCE_MANUAL
                    ),
                    basis=(
                        f"Предложено шаблоном #{template.id}; фактическое участие уточнено вручную."
                        if template else "Ручное распределение по подтверждённым строкам ВП"
                    ),
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
            elif action == "resolve_missing_author":
                item = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                    role=RewardParticipation.ROLE_DOCUMENTATION,
                )
                mark_na = request.POST.get("resolution") == "not_applicable"
                employee = None
                if not mark_na:
                    employee = get_object_or_404(
                        Employee,
                        pk=request.POST.get("employee_id"),
                        organization=organization,
                    )
                resolve_documentation_placeholder(
                    item,
                    request.user,
                    employee=employee,
                    not_applicable=mark_na,
                )
                messages.success(request, "Оформление без Автор_Key разрешено.")
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
                    _percent_value(request.POST.get("share_percent"), "Доля совместного оформления"),
                    request.user,
                )
                messages.success(request, "Совместный оформитель добавлен.")
            elif action == "cancel_pending":
                item = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                )
                cancel_pending_participation(item, request.user)
                messages.success(
                    request,
                    "Ошибочное назначение отменено. Создайте корректное назначение заново.",
                )
            elif action == "update_share":
                item = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                )
                update_participation_share(
                    item,
                    request.user,
                    _percent_value(request.POST.get("share_percent"), "Доля участия"),
                )
                messages.success(request, "Доля участия изменена.")
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
                        "sale_rate": _percent_value(request.POST.get("sale_rate"), "Продажа"),
                        "project_rate": _percent_value(request.POST.get("project_rate"), "Проект / расчёт"),
                        "work_rate": _percent_value(request.POST.get("work_rate"), "Выполнение работ"),
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
    clients = Client.objects.filter(organization=organization).order_by("name", "id")
    pools = Pool.objects.filter(organization=organization, is_deleted=False).select_related("client").order_by("client__name", "address", "id")
    participant_templates = (
        RewardParticipantTemplate.objects.filter(
            organization=organization,
            effective_from__lte=period_month,
        ).filter(
            models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=period_month)
        ).select_related("client", "pool", "employee").order_by("role", "client__name", "pool__address", "employee__display_name", "id")
    )
    document_options = reward_document_options(organization, period_month)
    selected_document_key = request.GET.get("document_key", "")
    return render(request, "pool_service/finance/employee_rewards.html", {
        "data": data,
        "period_month": period_month,
        "employees": employees,
        "clients": clients,
        "pools": pools,
        "participant_templates": participant_templates,
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
    if not can_view_rewards(request.user, organization):
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


@login_required
def employee_reward_confirm_preview(request):
    organization = _org(request)
    if not can_manage_participation(request.user, organization):
        return render(request, "403.html", status=403)
    period_month = _period(request)
    raw_ids = request.POST.getlist("participation_id") if request.method == "POST" else []
    ids = [int(value) for value in raw_ids if str(value).isdigit()]
    items = list(
        RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            pk__in=ids,
        ).select_related("employee", "author_identity").order_by("source_document_date", "source_document_number", "role", "id")
    )
    if request.method == "POST" and request.POST.get("confirm") == "1":
        try:
            confirm_participation_batch(organization, request.user, period_month, ids)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"Подтверждено назначений: {len(items)}.")
        return redirect(f"{reverse('finance_employee_rewards')}?month={period_month:%Y-%m}")
    return render(request, "pool_service/finance/employee_reward_confirm_preview.html", {
        "period_month": period_month,
        "items": items,
        "active_tab": "finance",
        "show_add_button": False,
    })
