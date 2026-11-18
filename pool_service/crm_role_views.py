"""Opt-in owner preview/confirmation UI. No automatic grant or role migration."""
from dataclasses import asdict
from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotFound
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from .services.crm_role_transition import prepare_role_transition, apply_role_transition
from .services.crm_role_policy_preview import CAPABILITIES, preview_role_change

SALT = 'crm-role-confirm-v1'
_load_confirmation = signing.loads
LABELS = dict(zip(CAPABILITIES, (
    'Просмотр сделок команды', 'Просмотр доступных лидов', 'Создание своих лидов',
    'Взятие лида в работу', 'Квалификация своих лидов', 'Изменение своих лидов',
    'Смена этапа своих лидов', 'Изменение своих сделок', 'Смена этапа своих сделок',
    'Создание своих сделок', 'Управление следующим действием своих карточек',
)))


class RoleReviewForm(forms.Form):
    roles = forms.MultipleChoiceField(label='Роли', choices=(('manager', 'Менеджер'),
        ('service', 'Сервис'), ('installer', 'Монтажник')), widget=forms.CheckboxSelectMultiple)
    reviewed = forms.BooleanField(label='Я проверил все индивидуальные ограничения ниже')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for capability in CAPABILITIES:
            self.fields['cap_' + capability] = forms.ChoiceField(label=LABELS[capability],
                choices=(('inherit', 'По роли'), ('allow', 'Разрешить явно'), ('deny', 'Запретить явно')))


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def review_roles(request, organization_id, target_id):
    if getattr(settings, 'CRM_ROLE_TRANSITION_ENABLED', False) is not True:
        return HttpResponseNotFound()
    ids = dict(actor_id=request.user.pk, organization_id=organization_id, target_id=target_id)
    # Reauthorize even before parsing a submitted token; no target enumeration.
    prepared = prepare_role_transition(**ids)
    state = prepared['snapshot']
    context = {'organization_id': organization_id, 'target_id': target_id,
        'current_roles': state['roles'], 'current_flags': [(LABELS[f], v) for f, v in state['effective_flags'].items()]}
    if request.method == 'POST' and request.POST.get('operation') == 'apply':
        try:
            payload = _load_confirmation(request.POST.get('confirmation', ''), salt=SALT, max_age=300)
            if payload.get('ids') != ids:
                raise PermissionDenied
            apply_role_transition(**ids, proposed_roles=payload['roles'], overrides=payload['overrides'],
                approved_increases=payload['increases'], expected_digest=payload['expected_digest'],
                acknowledge_assigned_call_visibility=request.POST.get('call_visibility_ack') == 'yes')
        except (signing.BadSignature, KeyError, TypeError, AttributeError, ValidationError):
            context['error'] = 'Подтверждение устарело или права изменились. Проверьте изменения заново.'
            context['form'] = RoleReviewForm(initial={'roles': state['roles'],
                **{'cap_' + f: (state['policy_overrides'] or {}).get(f, 'inherit') for f in CAPABILITIES}})
            return render(request, 'pool_service/crm_role_review.html', context, status=409)
        return redirect('crm_role_review', organization_id=organization_id, target_id=target_id)
    form = RoleReviewForm(request.POST or None, initial={'roles': state['roles'],
        **{'cap_' + f: (state['policy_overrides'] or {}).get(f, 'inherit') for f in CAPABILITIES}})
    context['form'] = form
    if request.method == 'POST' and form.is_valid():
        roles = form.cleaned_data['roles']
        overrides = {f: form.cleaned_data['cap_' + f] for f in CAPABILITIES}
        proposal = preview_role_change(proposed_roles=roles, current_flags=state['effective_flags'], overrides=overrides,
            active=True, dismissed=False, member=True, crm_read=state['crm_read'], crm_write=state['crm_write'],
            account_kind='person', provenance='managed')
        context['proposed_roles'] = roles
        context['rows'] = [{**asdict(row), 'label': LABELS[row.capability]} for row in proposal.rows]
        context['needs_call_ack'] = bool(set(proposal.increases) & {'view_leads', 'view_team_deals'})
        context['confirmation'] = signing.dumps({'ids': ids, 'roles': roles, 'overrides': overrides,
            'increases': list(proposal.increases), 'expected_digest': prepared['expected_digest']}, salt=SALT)
    return render(request, 'pool_service/crm_role_review.html', context)
