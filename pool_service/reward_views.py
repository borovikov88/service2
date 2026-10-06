import re
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from pool_service.client_queries import active_clients
from pool_service.models import Client, Employee, Pool
from pool_service.reward_models import (
    OneCAuthorIdentity,
    RewardParticipantTemplate,
    RewardParticipation,
    RewardSchemeVersion,
)
from pool_service.services.permissions import organization_for_user
from pool_service.services.rewards import (
    add_documentation_participant,
    calculate_month,
    cancel_pending_participation,
    can_close_period,
    can_manage_participation,
    can_manage_rules,
    can_view_rewards,
    close_month,
    confirm_participation,
    confirm_participation_batch,
    create_manual_participation,
    create_manual_participations_batch,
    create_scheme_version,
    ensure_test_scheme,
    map_author,
    map_customer_identity,
    map_order_object,
    month_start,
    resolve_documentation_placeholder,
    reward_order_workspace,
    sync_author_proposals,
    sync_reward_rules,
    update_order_participation,
    update_participation_share,
)


BLOCKING_ISSUES = {
    "missing_scheme",
    "missing_base",
    "missing_cost",
    "month_missing_cost",
    "share_overflow",
    "unmapped_author",
    "unconfirmed",
    "unallocated",
    "partial_direct_cost_allocation",
    "missing_selected_lines",
    "moved_selected_lines",
    "moved_all_lines_scope",
    "author_sync_stale",
    "missing_sale_role",
    "missing_documentation_role",
    "missing_work_role",
}


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


def _reward_selectable_employees(organization):
    """Employees currently available for new reward assignments."""
    today = timezone.localdate()
    return (
        Employee.objects.filter(
            organization=organization,
            is_active=True,
            employment_status=Employee.STATUS_EMPLOYED,
        )
        .filter(
            models.Q(hired_at__isnull=True) | models.Q(hired_at__lte=today)
        )
        .filter(
            models.Q(dismissed_at__isnull=True) | models.Q(dismissed_at__gt=today)
        )
        .order_by("display_name")
    )


def _reward_order_groups(orders, open_order=""):
    """Presentation-only grouping for repeated 1C expense invoices by customer."""
    groups = {}
    sequence = []

    for ui_index, order in enumerate(orders, start=1):
        order["ui_index"] = ui_index
        is_expense_invoice = (
            order.get("source_document_type") == "Document_РасходнаяНакладная"
        )
        customer_key = None
        if is_expense_invoice:
            if order.get("client_id"):
                customer_key = ("client", str(order["client_id"]))
            elif order.get("customer_guid"):
                customer_key = ("onec", str(order["customer_guid"]).lower())

        if customer_key:
            key = ("expense-customer",) + customer_key
            kind = "expense-customer"
        else:
            key = ("single", order.get("scope_key"))
            kind = "single"

        if key not in groups:
            group = {
                "key": "|".join(str(part) for part in key),
                "kind": kind,
                "orders": [],
            }
            groups[key] = group
            sequence.append(group)
        groups[key]["orders"].append(order)

    for group in sequence:
        documents = group["orders"]
        first = documents[0]
        group["is_customer_group"] = (
            group["kind"] == "expense-customer" and len(documents) > 1
        )
        group["document_count"] = len(documents)
        group["customer"] = (
            first.get("client_name")
            or first.get("customer")
            or "Без клиента"
        )
        group["attention_count"] = sum(
            1 for item in documents if item.get("problems")
        )
        group["open"] = any(
            item.get("scope_key") == open_order for item in documents
        )
        group["revenue"] = str(
            sum(
                (Decimal(str(item.get("revenue") or "0")) for item in documents),
                Decimal("0"),
            )
        )
        costs = [item.get("cost") for item in documents]
        group["cost"] = (
            None
            if any(value is None for value in costs)
            else str(sum((Decimal(str(value)) for value in costs), Decimal("0")))
        )
        gross_profits = [item.get("gross_profit") for item in documents]
        group["gross_profit"] = (
            None
            if any(value is None for value in gross_profits)
            else str(
                sum(
                    (Decimal(str(value)) for value in gross_profits),
                    Decimal("0"),
                )
            )
        )
    return sequence


def _rewards_redirect(request, period_month, *, default_tab="orders"):
    tab = (request.POST.get("return_tab") or default_tab).strip()
    if tab not in {"orders", "attention", "employees", "settings"}:
        tab = default_tab
    params = {"month": f"{period_month:%Y-%m}", "tab": tab}
    open_order = (request.POST.get("return_open") or "").strip()
    if open_order:
        params["open"] = open_order
    query = (request.POST.get("return_q") or "").strip()
    if query:
        params["q"] = query
    anchor = (request.POST.get("return_anchor") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", anchor):
        anchor = ""
    suffix = f"#{anchor}" if anchor else ""
    return redirect(f"{request.path}?{urlencode(params)}{suffix}")


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
                messages.success(request, "Правила вознаграждений созданы.")
            elif action == "sync_authors":
                result = sync_author_proposals(
                    organization, request.user, period_month, enrich_names=True
                )
                messages.success(
                    request,
                    (
                        f"Оформители обновлены: новых назначений {result['created']}; "
                        f"требуют решения {result['issues']}; "
                        f"имён обновлено {result['names_updated']}."
                    ),
                )
            elif action == "map_author":
                identity = get_object_or_404(
                    OneCAuthorIdentity,
                    pk=request.POST.get("identity_id"),
                    organization=organization,
                )
                employee = get_object_or_404(
                    _reward_selectable_employees(organization),
                    pk=request.POST.get("employee_id"),
                )
                map_author(identity, employee, request.user)
                messages.success(
                    request,
                    "Автор 1С сопоставлен. Открытые назначения оформления подтверждены автоматически.",
                )
            elif action == "map_customer":
                client = get_object_or_404(
                    Client,
                    pk=request.POST.get("client_id"),
                    organization=organization,
                )
                map_customer_identity(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    client=client,
                )
                sync_reward_rules(organization, request.user, period_month)
                messages.success(request, "Контрагент 1С связан с клиентом Service2.")
            elif action == "map_order_object":
                pool = get_object_or_404(
                    Pool,
                    pk=request.POST.get("pool_id"),
                    organization=organization,
                    is_deleted=False,
                )
                map_order_object(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    pool=pool,
                )
                sync_reward_rules(organization, request.user, period_month)
                messages.success(request, "Заказ связан с объектом Service2.")
            elif action == "create_template":
                if not can_manage_participation(request.user, organization):
                    raise PermissionDenied
                client = None
                pool = None
                employee = None
                if request.POST.get("client_id"):
                    client = get_object_or_404(
                        Client, pk=request.POST["client_id"], organization=organization
                    )
                if request.POST.get("pool_id"):
                    pool = get_object_or_404(
                        Pool,
                        pk=request.POST["pool_id"],
                        organization=organization,
                        is_deleted=False,
                    )
                if request.POST.get("employee_id"):
                    employee = get_object_or_404(
                        _reward_selectable_employees(organization),
                        pk=request.POST["employee_id"],
                    )
                is_company_client = request.POST.get("is_company_client") == "1"
                item = RewardParticipantTemplate(
                    organization=organization,
                    client=client,
                    pool=pool,
                    employee=employee,
                    role=request.POST.get("role", ""),
                    share=_percent_value(
                        request.POST.get("share_percent"), "Доля закрепления"
                    ),
                    effective_from=period_month,
                    is_company_client=is_company_client,
                    created_by=request.user,
                )
                item.full_clean()
                item.save()
                sync_reward_rules(organization, request.user, period_month)
                if item.role == RewardParticipantTemplate.ROLE_CLIENT_MANAGER:
                    messages.success(
                        request,
                        "Закрепление сохранено. Оно применяется только к надёжно связанным открытым заказам.",
                    )
                else:
                    messages.success(
                        request,
                        "Предложение роли сохранено. Факт участия всё равно подтверждается по заказу.",
                    )
            elif action == "end_template":
                if not can_manage_participation(request.user, organization):
                    raise PermissionDenied
                item = get_object_or_404(
                    RewardParticipantTemplate,
                    pk=request.POST.get("template_id"),
                    organization=organization,
                )
                if period_month < item.effective_from.replace(day=1):
                    raise ValidationError(
                        "Нельзя завершить правило до даты начала его действия."
                    )
                next_month = (period_month.replace(day=28) + timedelta(days=4)).replace(day=1)
                item.effective_to = next_month - timedelta(days=1)
                item.save(update_fields=["effective_to"])
                messages.success(
                    request,
                    "Правило завершено после выбранного месяца; история назначений не изменена.",
                )
            elif action == "add_participation":
                employee = get_object_or_404(
                    _reward_selectable_employees(organization),
                    pk=request.POST.get("employee_id"),
                )
                create_manual_participation(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    employee=employee,
                    role=request.POST.get("role", ""),
                    share=_percent_value(
                        request.POST.get("share_percent"), "Доля участия"
                    ),
                    line_identities=request.POST.getlist("line_identity"),
                    assignment_source=RewardParticipation.SOURCE_MANUAL,
                    basis="Ручное назначение руководителем в карточке заказа",
                )
                messages.success(request, "Участник сохранён и сразу подтверждён.")
            elif action == "add_participations_batch":
                total = int(request.POST.get("participants_total") or 0)
                if total < 1 or total > 20:
                    raise ValidationError(
                        "Добавьте от 1 до 20 участников за одно сохранение."
                    )
                assignments = []
                for index in range(total):
                    employee_id = (request.POST.get(
                        f"participant_{index}_employee_id"
                    ) or "").strip()
                    role = (request.POST.get(
                        f"participant_{index}_role"
                    ) or "").strip()
                    share_raw = (request.POST.get(
                        f"participant_{index}_share_percent"
                    ) or "").strip()
                    line_identities = request.POST.getlist(
                        f"participant_{index}_line_identity"
                    )
                    if not employee_id and not role and not share_raw:
                        continue
                    employee = get_object_or_404(
                        _reward_selectable_employees(organization),
                        pk=employee_id,
                    )
                    assignments.append({
                        "employee": employee,
                        "role": role,
                        "share": _percent_value(
                            share_raw,
                            f"Строка {index + 1}: доля участия",
                        ),
                        "line_identities": line_identities,
                    })
                created = create_manual_participations_batch(
                    organization,
                    request.user,
                    period_month,
                    document_key=request.POST.get("document_key", ""),
                    assignments=assignments,
                )
                messages.success(
                    request,
                    f"Сохранено участников: {len(created)}.",
                )
            elif action == "edit_participation":
                item = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                )
                employee = get_object_or_404(
                    _reward_selectable_employees(organization),
                    pk=request.POST.get("employee_id"),
                )
                update_order_participation(
                    item,
                    request.user,
                    employee=employee,
                    role=request.POST.get("role", ""),
                    share=_percent_value(
                        request.POST.get("share_percent"),
                        "Доля участия",
                    ),
                    line_identities=request.POST.getlist("line_identity"),
                )
                messages.success(request, "Участник обновлён.")
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
                        _reward_selectable_employees(organization),
                        pk=request.POST.get("employee_id"),
                    )
                resolve_documentation_placeholder(
                    item,
                    request.user,
                    employee=employee,
                    not_applicable=mark_na,
                )
                messages.success(request, "Оформитель сохранён.")
            elif action == "add_co_documenter":
                source = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                    role=RewardParticipation.ROLE_DOCUMENTATION,
                )
                employee = get_object_or_404(
                    _reward_selectable_employees(organization),
                    pk=request.POST.get("employee_id"),
                )
                add_documentation_participant(
                    source,
                    employee,
                    _percent_value(
                        request.POST.get("share_percent"), "Доля совместного оформления"
                    ),
                    request.user,
                )
                messages.success(
                    request, "Совместный оформитель добавлен и сразу подтверждён."
                )
            elif action == "cancel_pending":
                item = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                )
                cancel_pending_participation(item, request.user)
                messages.success(request, "Автоматическое предложение отклонено.")
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
                    _percent_value(
                        request.POST.get("share_percent"), "Доля участия"
                    ),
                )
                messages.success(request, "Доля участия изменена.")
            elif action == "confirm":
                if not can_manage_participation(request.user, organization):
                    raise PermissionDenied
                item = get_object_or_404(
                    RewardParticipation,
                    pk=request.POST.get("participation_id"),
                    organization=organization,
                    period_month=period_month,
                )
                confirm_participation(item, request.user)
                messages.success(request, "Автоматическое предложение подтверждено.")
            elif action == "save_scheme":
                create_scheme_version(
                    organization,
                    request.user,
                    effective_from=period_month,
                    values={
                        "documentation_retail_fixed": request.POST[
                            "documentation_retail_fixed"
                        ],
                        "retail_check_rate": _percent_value(
                            request.POST.get("retail_check_rate"),
                            "Розничный чек",
                        ),
                        "documentation_document_fixed": request.POST[
                            "documentation_document_fixed"
                        ],
                        "sale_rate": _percent_value(
                            request.POST.get("sale_rate"), "Продажа"
                        ),
                        "project_rate": _percent_value(
                            request.POST.get("project_rate"), "Проект / расчёт"
                        ),
                        "work_rate": _percent_value(
                            request.POST.get("work_rate"), "Выполнение работ"
                        ),
                        "client_manager_rate": _percent_value(
                            request.POST.get("client_manager_rate", "0"),
                            "Менеджер клиента",
                        ),
                    },
                )
                messages.success(request, "Создана новая версия правил вознаграждений.")
            elif action == "close_month":
                close_month(organization, request.user, period_month)
                messages.success(
                    request,
                    f"Вознаграждения за {period_month:%m.%Y} зафиксированы.",
                )
        except (ValidationError, ValueError, KeyError) as exc:
            messages.error(
                request, "; ".join(getattr(exc, "messages", [str(exc)]))
            )
        return _rewards_redirect(request, period_month)

    # Reconcile deterministic saved-data assignments on an open month. No live 1C
    # call is allowed here; author-name enrichment stays an explicit action.
    initial = calculate_month(organization, period_month, employee_id=employee_id)
    if (
        can_manage_participation(request.user, organization)
        and not initial.get("closed")
    ):
        sync_author_proposals(
            organization, request.user, period_month, enrich_names=False
        )
        sync_reward_rules(organization, request.user, period_month)

    workspace = reward_order_workspace(
        organization, period_month, employee_id=employee_id
    )
    data = workspace["data"]
    query = (request.GET.get("q") or "").strip()
    # Orders stay fully rendered so the page can use the project-standard live
    # search without a reload. The query is kept only to restore the field/URL.
    orders = workspace["orders"]
    open_order = request.GET.get("open", "")
    order_groups = _reward_order_groups(orders, open_order=open_order)
    attention_keys = {item["scope_key"] for item in workspace["attention"]}
    attention = [item for item in orders if item["scope_key"] in attention_keys]

    scheme = (
        RewardSchemeVersion.objects.filter(
            organization=organization,
            effective_from__lte=period_month,
        )
        .filter(
            models.Q(effective_to__isnull=True)
            | models.Q(effective_to__gte=period_month)
        )
        .order_by("-effective_from", "-version")
        .first()
    )
    participations = (
        RewardParticipation.objects.filter(
            organization=organization, period_month=period_month
        )
        .select_related("employee", "author_identity")
        .order_by(
            "source_document_date",
            "source_document_number",
            "role",
            "employee__display_name",
        )
    )
    employees = _reward_selectable_employees(organization)
    clients = active_clients(Client.objects.filter(organization=organization)).order_by("name", "id")
    pools = (
        Pool.objects.filter(organization=organization, is_deleted=False)
        .select_related("client")
        .order_by("client__name", "address", "id")
    )
    participant_templates = (
        RewardParticipantTemplate.objects.filter(
            organization=organization,
            effective_from__lte=period_month,
        )
        .filter(
            models.Q(effective_to__isnull=True)
            | models.Q(effective_to__gte=period_month)
        )
        .select_related("client", "pool", "employee")
        .order_by(
            "role",
            "client__name",
            "pool__address",
            "employee__display_name",
            "id",
        )
    )
    blocking_issues = [
        issue for issue in data.get("issues", [])
        if issue.get("kind") in BLOCKING_ISSUES
    ]

    selected_tab = (request.GET.get("tab") or "orders").strip()
    if selected_tab not in {"orders", "attention", "employees", "settings"}:
        selected_tab = "orders"

    return render(
        request,
        "pool_service/finance/employee_rewards.html",
        {
            "data": data,
            "orders": orders,
            "order_groups": order_groups,
            "attention_orders": attention,
            "filled_order_count": max(0, len(orders) - len(attention)),
            "blocking_issues": blocking_issues,
            "period_month": period_month,
            "employees": employees,
            "clients": clients,
            "pools": pools,
            "participant_templates": participant_templates,
            "participations": participations,
            "scheme": scheme,
            "search_query": query,
            "selected_tab": selected_tab,
            "open_order": open_order,
            "can_manage_participation": can_manage_participation(
                request.user, organization
            ),
            "can_manage_rules": can_manage_rules(request.user, organization),
            "can_close_period": can_close_period(request.user, organization),
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
def employee_reward_detail(request, employee_id):
    organization = _org(request)
    employee = get_object_or_404(
        Employee, pk=employee_id, organization=organization
    )
    if not can_view_rewards(request.user, organization):
        return render(request, "403.html", status=403)
    period_month = _period(request)
    data = calculate_month(organization, period_month, employee_id=employee.id)
    return render(
        request,
        "pool_service/finance/employee_reward_detail.html",
        {
            "employee": employee,
            "data": data,
            "period_month": period_month,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )


@login_required
def employee_reward_confirm_preview(request):
    """Legacy/admin confirmation surface retained for automatic proposals."""
    organization = _org(request)
    if not can_manage_participation(request.user, organization):
        return render(request, "403.html", status=403)
    period_month = _period(request)
    raw_ids = (
        request.POST.getlist("participation_id") if request.method == "POST" else []
    )
    ids = [int(value) for value in raw_ids if str(value).isdigit()]
    items = list(
        RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            pk__in=ids,
        )
        .select_related("employee", "author_identity")
        .order_by(
            "source_document_date",
            "source_document_number",
            "role",
            "id",
        )
    )
    if request.method == "POST" and request.POST.get("confirm") == "1":
        try:
            confirm_participation_batch(
                organization, request.user, period_month, ids
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"Подтверждено предложений: {len(items)}.")
        return redirect(
            f"{reverse('finance_employee_rewards')}?month={period_month:%Y-%m}&tab=settings"
        )
    return render(
        request,
        "pool_service/finance/employee_reward_confirm_preview.html",
        {
            "period_month": period_month,
            "items": items,
            "active_tab": "finance",
            "show_add_button": False,
        },
    )
