"""Read-only production acceptance of existing contact, intake and permission decisions."""
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from pool_service.client_crm_models import ClientBusinessRole
from pool_service.client_phone_matching import clients_by_phone
from pool_service.client_queries import readable_clients
from pool_service.communication_models import PhoneCall
from pool_service.communication_services import conversation_capability
from pool_service.crm_commercial_models import CrmLeadDealLink
from pool_service.models import Organization
from pool_service.services.call_access import can_access_call_content, can_read_call, scope_calls
from pool_service.services.call_privacy import private_call_owner_pairs
from pool_service.services.call_processing_policy import phone_key
from pool_service.services.call_processing_settings import settings_allowed
from pool_service.services.crm_commercial_access import deals_for_user
from pool_service.services.crm_communication_routing import readable_routes
from pool_service.services.crm_lead_access import _private_call_annotations, _readable_sources, leads_for_user
from pool_service.staff_call_models import CallWorkContext

PAGE_SIZE = 8


class ReviewForm(forms.Form):
    phone = forms.CharField(label='Номер собеседника', max_length=40)
    employee = forms.ModelChoiceField(label='Проверить права сотрудника', queryset=get_user_model().objects.none())
    before = forms.IntegerField(required=False, min_value=1, max_value=2**63-1, widget=forms.HiddenInput)

    def __init__(self, organization, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['employee'].queryset = get_user_model().objects.filter(is_active=True,
            organizationaccess__organization=organization).distinct().order_by('last_name', 'first_name', 'pk')
        self.fields['employee'].label_from_instance = lambda user: user.get_full_name() or user.get_username()

    def clean_phone(self):
        key = phone_key(self.cleaned_data['phone'])
        if not key:
            raise forms.ValidationError('Введите полный номер телефона.')
        return key


def card_results(actor, subject, organization, call):
    links = _readable_sources(actor, organization).filter(inbox__source_type='call', inbox__source_id=call.pk)
    leads = set(links.values_list('lead_id', flat=True))
    contexts = CallWorkContext.objects.filter(organization=organization, call=call)
    leads.update(contexts.exclude(lead_id=None).values_list('lead_id', flat=True))
    visible_leads = list(leads_for_user(actor, organization).filter(pk__in=leads).order_by('pk')[:8]) if leads else []
    deals = set(contexts.exclude(deal_id=None).values_list('deal_id', flat=True))
    if visible_leads:
        deals.update(CrmLeadDealLink.objects.filter(organization=organization, lead__in=visible_leads,
            deal__organization=organization).values_list('deal_id', flat=True))
    deals.update(readable_routes(actor, organization).filter(inbox__source_type='call', inbox__source_id=call.pk,
        deal__isnull=False).values_list('deal_id', flat=True))
    visible_deals = list(deals_for_user(actor, organization).filter(pk__in=deals).order_by('pk')[:8]) if deals else []
    results = []
    for label, visible, permitted, responsible in (
        ('Лид', visible_leads, leads_for_user, 'assignee_id'),
        ('Сделка', visible_deals, deals_for_user, 'manager_id'),
    ):
        if visible:
            allowed = set(permitted(subject, organization).filter(pk__in=[card.pk for card in visible]).values_list('pk', flat=True))
            results.extend({'kind': label, 'id': card.pk, 'read': card.pk in allowed,
                'assigned': getattr(card, responsible) == subject.pk} for card in visible)
    return results


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def review_call_access(request, organization_id):
    org = get_object_or_404(Organization, pk=organization_id)
    actor = request.user
    if not settings_allowed(actor, org) or not conversation_capability(actor, 'can_view_all_calls', org):
        raise PermissionDenied
    form = ReviewForm(org, request.POST if request.method == 'POST' else None)
    context = {'form': form, 'org': org, 'active_tab': 'communications'}
    if form.is_valid():
        key, subject = form.cleaned_data['phone'], form.cleaned_data['employee']
        candidates = clients_by_phone(org, key, limit=26)
        visible = readable_clients(actor).filter(organization=org, pk__in=[client.pk for client in candidates[:25]])
        categories = {}
        for role in ClientBusinessRole.objects.filter(client__in=visible, active=True):
            categories.setdefault(role.client_id, []).append(role.get_role_display())
        contacts = [{'id': client.pk, 'name': client.name, 'roles': ', '.join(categories.get(client.pk, ()))}
            for client in visible.order_by('name', 'pk')]
        base = PhoneCall.objects.filter(organization=org, connection__organization=org,
            source_kind=PhoneCall.SOURCE_TELEPHONY, direction__in=('in', 'out'), privacy_phone_key=key, privacy_phone_source=F('phone_number'))
        if form.cleaned_data['before']:
            base = base.filter(pk__lt=form.cleaned_data['before'])
        # Never enumerate another owner's private calls. The raw cursor also
        # must not reveal gaps, IDs or counts for conversations outside scope.
        base = _private_call_annotations(base).filter(_crm_private=False, _crm_held=False, _crm_key_current=True)
        rows = list(scope_calls(actor, org, base).order_by('-pk')[:PAGE_SIZE + 1])
        pairs = private_call_owner_pairs(rows)
        private_ids = {call_id for call_id, _ in pairs}
        visible_rows = [call for call in rows[:PAGE_SIZE] if call.pk not in private_ids]
        results = []
        for call in visible_rows:
            if not can_read_call(actor, call):
                continue
            reason = 'Рабочий звонок'
            if call.internal_number_pending:
                reason = 'Ожидается подтверждение времени звонка'
            elif call.internal_contact_number_id:
                reason = 'Подтверждённый внутренний номер'
            elif call.intake_scope and call.intake_occurred_at is None:
                reason = 'АТС не подтвердила дату. Для доступа учитывается возможная должность на момент разговора.'
            elif call.sales_head_user_snapshot:
                reason = 'Рабочий звонок РОПа по сохранённому назначению'
            elif call.owner_user_snapshot:
                reason = 'Рабочий звонок с сохранённой отметкой директора'
            results.append({'date': call.started_at, 'id': call.pk, 'reason': reason,
                'audio_allowed': can_access_call_content(subject, call), 'file_exists': bool(call.recording_file),
                'cards': card_results(actor, subject, org, call)})
        context.update(checked=True, contacts=contacts, contacts_limited=len(candidates)>25,
            results=results, subject=subject, key=key, next_id=results[-1]['id'] if results and len(rows)>PAGE_SIZE else None)
    return render(request, 'pool_service/call_access_review.html', context)
