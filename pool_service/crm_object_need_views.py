"""Bounded internal HTTP adapter; GET never creates source anchors."""
import json
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, F, OuterRef, Q, Subquery, Value, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .crm_commercial_models import CrmAccess
from .crm_object_need_models import CrmObjectNeedReview, CrmObjectNeedSourceRevision, MODES
from .crm_live_views import _id
from .services.crm_commercial_access import can_view_team_deals
from .models import Organization, ServiceTask
from .services.crm_object_needs import NeedConflict, editable, materials_hash, review_need, source_tasks
from .services.permissions import company_has_access
from .services.task_feedback import version_for


STATES = {'pending': 'Не разобрано', 'reviewed': 'Разобрано', 'changed': 'Состав изменился', 'needs_resolution': 'Связи требуют проверки'}
MAX_SOURCE_DISPLAY = 20000


def _scope(request):
    selected = request.GET.getlist('org_id')
    if len(selected) != 1:
        raise PermissionDenied
    try:
        org_id = _id(selected[0])
    except ValidationError as exc:
        raise PermissionDenied from exc
    org = Organization.objects.filter(pk=org_id).first()
    if (not org or not company_has_access(org) or not can_view_team_deals(request.user, org)
            or not CrmAccess.objects.filter(organization=org, user_id=request.user.pk, view_team_deals=True).exists()):
        raise PermissionDenied
    return org


def _queryset(request, org):
    candidates = ServiceTask.objects.filter(water_reading_id=OuterRef('water_reading_id'), task_type='supply_request').order_by().values('water_reading_id').annotate(total=Count('id')).values('total')
    reviews = CrmObjectNeedReview.objects.filter(need_id=OuterRef('object_need__id'), sequence=OuterRef('object_need__review_revision')).values('source__sequence')[:1]
    sources = CrmObjectNeedSourceRevision.objects.filter(need_id=OuterRef('object_need__id'), sequence=OuterRef('object_need__source_revision')).values('materials')[:1]
    qs = source_tasks(request.user, org).annotate(_candidates=Subquery(candidates), _review_source=Subquery(reviews), _source_materials=Subquery(sources))
    qs = qs.annotate(_need_state=Case(
        When(_candidates__gt=1, then=Value('needs_resolution')),
        When(crm_item__isnull=True, then=Value('needs_resolution')),
        When(object_need__isnull=True, then=Value('pending')),
        When(object_need__review_revision=0, then=Value('pending')),
        When(_review_source=F('object_need__source_revision'), then=Value('reviewed')),
        default=Value('changed'), output_field=CharField()))
    return qs.select_related('object_need')


def _row(task, request, org, *, detail=False):
    need = getattr(task, 'object_need', None)
    latest = need.reviews.select_related('source', 'actor').filter(sequence=need.review_revision).first() if need else None
    source = need.sources.select_related('actor').filter(sequence=need.source_revision).first() if need else None
    materials = (task.water_reading.required_materials or '').strip()
    state = task._need_state
    mismatch = bool(need and (need.reading_id != task.water_reading_id or need.crm_item_id != task.crm_item_id
        or need.organization_id != org.pk or not source or source.content_hash != materials_hash(materials)))
    if mismatch:
        state = 'needs_resolution'
    grant = CrmAccess.objects.filter(user_id=request.user.pk, organization=org).first()
    can_review = bool(state != 'needs_resolution' and len(materials) <= MAX_SOURCE_DISPLAY
        and org.crm_v2_write and grant and grant.edit_own_deals
        and grant.view_team_deals
        and task.primary_responsible_id == request.user.pk and editable(task))
    reading = task.water_reading
    author = source.actor if source else reading.added_by
    result = {'id': task.pk, 'need_id': need.pk if need else None,
        'client': task.client.name if task.client_id else '', 'object': task.pool.address,
        'author': (author.get_full_name() or author.username) if author else 'Неизвестен',
        'recorded_at': (source.recorded_at if source else reading.date).isoformat(),
        'source_revision': need.source_revision if need else 0, 'source_hash': materials_hash(materials),
        'review_revision': need.review_revision if need else 0, 'task_version': version_for(task) if detail else None,
        'materials': materials[:MAX_SOURCE_DISPLAY if detail else 240],
        'materials_truncated': len(materials) > (MAX_SOURCE_DISPLAY if detail else 240),
        'legacy': need is None, 'state': state, 'state_label': STATES[state],
        'modes': list(dict.fromkeys(line['mode'] for line in latest.positions)) if not mismatch and latest and source and latest.source_id == source.pk else [],
        'can_review': can_review, 'task_url': reverse('task_edit', args=[task.pk]),
        'readonly_reason': '' if can_review else 'Разбор недоступен. Проверьте ответственного, доступ, состояние и связи в исходной задаче.'}
    if detail:
        # Old classification is displayed as history only, never prefilled into a
        # changed composition. Each review names its own immutable source.
        result['positions'] = latest.positions if not mismatch and latest and source and latest.source_id == source.pk else []
        result['history'] = [{'sequence': row.sequence, 'source_revision': row.source.sequence,
            'actor': row.actor.get_full_name() or row.actor.username, 'recorded_at': row.recorded_at.isoformat(),
            'positions': row.positions} for row in need.reviews.select_related('source', 'actor').order_by('-sequence')[:20]] if need else []
        result['sources'] = [{'sequence': row.sequence, 'materials': row.materials[:MAX_SOURCE_DISPLAY],
            'materials_truncated': len(row.materials) > MAX_SOURCE_DISPLAY,
            'actor': (row.actor.get_full_name() or row.actor.username) if row.actor else 'Неизвестен',
            'recorded_at': row.recorded_at.isoformat()} for row in need.sources.select_related('actor').order_by('-sequence')[:20]] if need else []
    return result


@login_required
@never_cache
@ensure_csrf_cookie
@require_GET
def workspace(request):
    org = _scope(request)
    suffix = '?' + urlencode({'org_id': org.pk})
    return render(request, 'pool_service/crm_live/object_needs.html', {'organization': org,
        'data_url': reverse('crm_object_need_data') + suffix,
        'command_url': reverse('crm_object_need_review') + suffix,
        'detail_base': reverse('crm_object_need_detail', args=[1]).rsplit('1/', 1)[0],
        'modes': MODES, 'states': STATES, 'active_tab': 'crm'})


@login_required
@never_cache
@require_GET
def data(request):
    org = _scope(request)
    qs = _queryset(request, org)
    q = request.GET.get('q', '').strip()[:200]
    if request.GET.get('scope') == 'mine':
        qs = qs.filter(primary_responsible_id=request.user.pk)
    state = request.GET.get('state', '')
    if state in STATES:
        qs = qs.filter(_need_state=state)
    mode = request.GET.get('mode', '')
    if mode in MODES:
        # Filter the latest review for the current source only. JSON contains
        # typed bounded positions, not untrusted source/provider payloads.
        line_mode = Q()
        for index in range(50):
            line_mode |= Q(**{f'object_need__reviews__positions__{index}__mode': mode})
        qs = qs.filter(Q(object_need__reviews__sequence=F('object_need__review_revision'))
            & Q(object_need__reviews__source__sequence=F('object_need__source_revision')) & line_mode)
    if q:
        qs = qs.filter(Q(client__name__icontains=q) | Q(pool__address__icontains=q)
            | Q(water_reading__required_materials__icontains=q))
    page = Paginator(qs.order_by('-water_reading__date', '-id'), 50).get_page(request.GET.get('page', '1'))
    return JsonResponse({'rows': [_row(task, request, org) for task in page], 'total': page.paginator.count,
        'page': page.number, 'pages': page.paginator.num_pages})


@login_required
@never_cache
@require_GET
def detail(request, task_id):
    org = _scope(request)
    task = get_object_or_404(_queryset(request, org), pk=task_id)
    return JsonResponse({'need': _row(task, request, org, detail=True)})


@login_required
@never_cache
@require_POST
def review(request):
    try:
        org = _scope(request)
        if len(request.body) > 524288:
            return JsonResponse({'error': 'Слишком большой запрос.'}, status=400)
        payload = json.loads(request.body)
        expected = {'task_id', 'source_revision', 'source_hash', 'review_revision', 'task_version', 'operation_key', 'positions'}
        if type(payload) is not dict or set(payload) != expected:
            raise ValidationError('Неизвестные поля команды.')
        result = review_need(actor=request.user, organization=org, **payload)
    except PermissionDenied:
        return JsonResponse({'error': 'Текущий доступ к CRM или исходной задаче недоступен.'}, status=403)
    except (ValueError, UnicodeError, ValidationError) as exc:
        message = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'Некорректный запрос.'
        return JsonResponse({'error': message}, status=409 if isinstance(exc, NeedConflict) else 400)
    return JsonResponse({'result': result})
