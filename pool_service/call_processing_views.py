from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from pool_service.models import OrganizationAccess
from pool_service.call_processing_models import (
    CallProcessingBudget,
    CallProcessingRule,
    CallPrivateNumber,
)
from pool_service.services.permissions import organization_for_user
from pool_service.services.call_usage import usage_summary
from pool_service.services.call_processing_settings import (
    STAFF_ROLES, settings_allowed, save_rule, save_budget, add_private_numbers,
    remove_private_number, preview_rules,
)


class RuleForm(forms.Form):
    employee_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    mode = forms.ChoiceField(label="Режим", choices=[
        ("manual", "Только вручную"),
        ("all_except", "Все звонки, кроме личных исключений"),
        ("allowlist", "Только по правилам: сотрудники и рабочие номера"),
    ], widget=forms.Select(attrs={"class": "form-select"}))
    include_staff = forms.BooleanField(label="В разрешающем режиме включать разговоры с сотрудниками", required=False)
    numbers_text = forms.CharField(
        label="Разрешённые рабочие номера", required=False, max_length=20000,
        help_text="Каждый номер с новой строки. Применяется в режиме «Только по правилам». Для иностранных номеров укажите + и код страны.",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )


class BudgetForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    monthly_limit_usd = forms.DecimalField(
        label="Месячный лимит, USD",
        required=False,
        min_value=0.0001,
        max_digits=12,
        decimal_places=4,
        help_text="Пустое поле означает, что денежный лимит не задан. Автообработка всё равно остаётся выключенной, пока не завершён запускной контур.",
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
    )


class PrivateForm(forms.Form):
    label = forms.CharField(label="Название личного контакта", max_length=120, widget=forms.TextInput(attrs={"class": "form-control"}))
    numbers_text = forms.CharField(
        label="Номера контакта", max_length=20000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        help_text="Можно добавить несколько номеров, каждый с новой строки.",
    )


def _rule_rows(organization, actor, bound_form=None):
    owners = set(OrganizationAccess.objects.filter(organization=organization, role="owner").values_list("user_id", flat=True))
    employees = list(User.objects.filter(
        is_active=True, organizationaccess__organization=organization,
        organizationaccess__role__in=STAFF_ROLES,
    ).distinct().order_by("last_name", "first_name", "username"))
    editable_ids = [person.pk for person in employees if person.pk == actor.pk or person.pk not in owners]
    stored = {row.employee_id: row for row in CallProcessingRule.objects.filter(organization=organization, employee_id__in=editable_ids)}
    result = []
    for employee in employees:
        editable = employee.pk in editable_ids
        rule = stored.get(employee.pk)
        form = None
        if editable:
            initial = {
                "employee_id": employee.pk, "expected_revision": rule.revision if rule else 0,
                "mode": rule.mode if rule else "manual", "include_staff": rule.include_staff if rule else True,
                "numbers_text": "\n".join(rule.work_numbers) if rule else "",
            }
            form = RuleForm(initial=initial, auto_id=f"rule_{employee.pk}_%s")
            if bound_form and str(bound_form.data.get("employee_id")) == str(employee.pk):
                form = bound_form
        result.append({
            "id": employee.pk, "name": employee.get_full_name() or employee.username,
            "editable": editable, "is_self": employee.pk == actor.pk,
            "saved": rule is not None, "form": form,
        })
    return result


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
@csrf_protect
def call_processing_settings(request):
    from pool_service.views import _redirect_if_access_blocked

    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked
    organization = organization_for_user(request.user)
    if not settings_allowed(request.user, organization):
        raise PermissionDenied
    bound_rule = None
    private_form = PrivateForm()
    budget = CallProcessingBudget.objects.filter(organization=organization).first()
    budget_form = BudgetForm(initial={
        "expected_revision": budget.revision if budget else 0,
        "monthly_limit_usd": budget.monthly_limit_usd if budget else None,
    })
    errors = []
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "save_rule":
                bound_rule = RuleForm(request.POST)
                if bound_rule.is_valid():
                    save_rule(user=request.user, organization=organization, **bound_rule.cleaned_data)
                    messages.success(request, "Правило сохранено для предпросмотра. Авторасшифровка не запущена.")
                    return redirect("call_processing_settings")
                status = 400
            elif action == "save_budget":
                budget_form = BudgetForm(request.POST)
                if budget_form.is_valid():
                    save_budget(
                        user=request.user,
                        organization=organization,
                        **budget_form.cleaned_data,
                    )
                    messages.success(request, "Месячный лимит расходов сохранён. Автоматический запуск не включён.")
                    return redirect("call_processing_settings")
                status = 400
            elif action == "add_private":
                private_form = PrivateForm(request.POST)
                if private_form.is_valid():
                    add_private_numbers(user=request.user, organization=organization, **private_form.cleaned_data)
                    messages.success(request, "Личный номер сохранён. Совпадающие ваши звонки скрываются из рабочих разделов сразу; авторасшифровка по общим правилам всё ещё выключена.")
                    return redirect("call_processing_settings")
                status = 400
            elif action == "remove_private":
                try:
                    number_id = int(request.POST.get("number_id", ""))
                except (TypeError, ValueError):
                    raise ValidationError("Некорректный номер записи.")
                remove_private_number(user=request.user, organization=organization, number_id=number_id)
                messages.success(request, "Номер удалён из вашего подготовленного списка исключений.")
                return redirect("call_processing_settings")
            else:
                raise ValidationError("Неизвестное действие.")
        except ValidationError as exc:
            errors = exc.messages
            status = 400
    preview = preview_rules(user=request.user, organization=organization) if request.method == "GET" and request.GET.get("preview") == "1" else None
    return render(request, "pool_service/communications/auto_processing.html", {
        "page_title": "Автообработка звонков", "active_tab": "communications",
        "can_manage_communication_channels": True,
        "rule_rows": _rule_rows(organization, request.user, bound_rule),
        "private_form": private_form, "budget_form": budget_form,
        "usage": usage_summary(organization),
        "errors": errors, "preview": preview,
        "private_numbers": CallPrivateNumber.objects.filter(organization=organization, owner=request.user).order_by("label", "phone_key"),
    }, status=status)
