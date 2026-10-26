"""Read-only owner evidence and manager triage over existing Avito/CRM records."""
from copy import deepcopy
from datetime import date
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from . import avito_improvement_proposals as projects
from . import crm_source_links as links
from .avito_services_feed import entries
from .communication_models import Conversation
from .crm_source_models import CrmSourceLink

PROJECTS = {'8346503274': 9, '8343962135': 10}
WATER_ID = '8346503274'
SCAN_LIMIT = 500
DISPLAY_LIMIT = 50


def _baseline(source):
    def day(value):
        try:
            return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
        except ValueError:
            return None
    received = projects._time(source.get('received_at'))
    expected, reported = source.get('expected_days'), source.get('reported_days')
    known = source.get('metrics_known')
    return {'from': day(source.get('from')), 'to': day(source.get('to')),
        'cutoff': day(source.get('cutoff')), 'received_at': received.isoformat() if received else None,
        'expected_days': expected if type(expected) is int and 0 <= expected <= 30 else None,
        'reported_days': reported if type(reported) is int and 0 <= reported <= 30 else None,
        'pages_complete': source.get('pages_complete') is True,
        'ad_rows_complete': source.get('ad_rows_complete') is True,
        'low_or_unknown_volume': source.get('low_or_unknown_volume') is not False,
        'provider_timezone': 'unknown', 'barnaul_calendar_confirmed': False,
        'metrics_known': {key: isinstance(known, dict) and known.get(key) is True
                          for key in projects.METRICS}}


def project_receipt(connection, actor):
    """Fixed project evidence, with literal title values confined to server memory."""
    current, actor = projects._publication_connection(connection.pk, actor)
    if not projects._pilot(current):
        raise PermissionDenied
    binding = projects._digest(projects._binding(current))
    context = projects.page_context(current, actor)
    feed = context['feed']
    before = (feed.revision, feed.published_revision, deepcopy(feed.published), deepcopy(feed.draft))
    publication = projects._source(current, feed)
    rows = entries(feed.published)
    index = {row['AvitoId']: row for row in rows}
    if len(index) != len(rows):
        raise projects.ProposalError('source_unavailable')
    found = {item['avito_id']: item for item in context['projects']
        if PROJECTS.get(item['avito_id']) == item['event'].pk}
    output = []
    for identifier, event_id in PROJECTS.items():
        item, row = found.get(identifier), index.get(identifier)
        result = {'avito_id': identifier, 'event_id': event_id, 'state': 'unavailable'}
        if item is not None and row is not None:
            change = item['event'].changes[0]
            same_identity = row.get('Id') == change.get('id')
            baseline = item['project']['analysis']
            active = projects._status(current, feed, publication, row, timezone.now())
            evaluation = item['project']['evaluation']
            result.update(state='available', source_revision=item['source_revision'],
                current_source_matches=item['current_source'] is True,
                current_local_title_matches=same_identity and row.get('Title') == item['before'],
                draft_unchanged_since_project=item['draft_unchanged_since_project'] is True,
                proposed_differs_from_before=item['proposed'] != item['before'],
                changed_field='Title', provider_content_verified=False,
                fresh_active_status_confirmed=active is not None,
                status_checked_at=active.get('checked_at') if active else None,
                project_created_at=item['event'].created_at.isoformat(),
                baseline=_baseline(baseline),
                evaluation={'state': 'not_started', 'source_dates': 14,
                    'primary_metric': evaluation.get('primary_metric')
                        if evaluation.get('primary_metric') == 'qualified_inquiry_share' else 'unknown',
                    'requires_separate_owner_publication': True,
                    'requires_calendar_confirmation': True},
                wordstat_available=False)
        output.append(result)
    latest, actor = projects._publication_connection(current.pk, actor)
    feed.refresh_from_db()
    if (not projects._pilot(latest) or binding != projects._digest(projects._binding(latest))
            or before != (feed.revision, feed.published_revision, feed.published, feed.draft)
            or projects._source(latest, feed)['version'].pk != context['source_version'].pk):
        raise projects.ProposalError('scope_changed')
    return {'state': 'available', 'connection_id': 4, 'account_id': projects.ACCOUNT,
        'checked_at': timezone.now().isoformat(), 'publication_revision': feed.published_revision,
        'source_version_id': context['source_version'].pk, 'projects': output,
        'provider_action_performed': False, 'titles_exported': False}


def _association(connection, kind, source, link, known, now):
    """An explicit unknown or broken saved link never becomes inferred attribution."""
    if link is not None:
        if (not links._evidence_valid(link, connection, kind, source)
                or link.ad_source not in {'provider', 'manual'} or link.avito_item_id not in known
                or kind == 'call' and link.call_origin != 'manual'):
            return None
        if link.ad_source == 'manual':
            return link.avito_item_id, 'manager_confirmation'
    if kind != 'conversation':
        return None
    context = source.provider_context if isinstance(source.provider_context, dict) else {}
    item = context.get('item') if isinstance(context.get('item'), dict) else {}
    identifier, synced = str(item.get('id', '')), source.context_synced_at
    if (identifier not in known or synced is None or not timezone.is_aware(synced) or synced > now
            or link is not None and link.avito_item_id != identifier):
        return None
    return identifier, 'stored_provider_context'


def water_quality_queue(actor, connection):
    """Visible existing sources only; names, messages and guessed sales are absent."""
    connection = links.scope(actor, connection.pk)
    now = timezone.now()
    binding = projects._digest(projects._binding(connection))
    known = links.owned_ads(connection, now)
    dialogs = list(Conversation.objects.filter(connection=connection,
        organization_id=connection.channel.organization_id,
        connection__channel__organization_id=connection.channel.organization_id)
        .order_by('-last_message_at', '-pk')[:SCAN_LIMIT+1])
    call_links = list(CrmSourceLink.objects.filter(organization_id=connection.channel.organization_id,
        avito_connection=connection, phone_call__isnull=False).order_by('-pk')[:SCAN_LIMIT+1])
    sources = [('conversation', candidate.uuid) for candidate in dialogs[:SCAN_LIMIT]]
    sources += [('call', link.phone_call_id) for link in call_links[:SCAN_LIMIT]]
    output, eligible, confirmed, unknown, assessed = [], 0, 0, 0, 0
    for kind, identifier in sources:
        try:
            source = links.source_for(actor, connection, kind, identifier)
        except PermissionDenied:
            continue
        stored = links.link_for(connection, kind, source)
        link = links.visible_link(actor, connection, kind, source)
        if stored is not None and link is None:
            continue  # Existing CRM visibility remains authoritative.
        association = _association(connection, kind, source, link, known, now)
        if association is None:
            unknown += 1
            continue
        if association[0] != WATER_ID:
            continue
        confirmed += 1
        quality = link.quality if link else 'unknown'
        if quality in {'target', 'non_target'}:
            assessed += 1
            continue
        if quality != 'unknown':
            continue
        eligible += 1
        if len(output) >= DISPLAY_LIMIT:
            continue
        item = links.item_for(actor, connection, link.crm_item_id) if link and link.crm_item_id else None
        task = link.next_task if link and link.next_task_id else None
        task_id = task.pk if task and links.task_allowed(actor, task, item, connection.channel.organization_id) else None
        try:
            links.source_for(actor, connection, kind, identifier, edit=True)
            editable = True
        except PermissionDenied:
            editable = False
        opaque_id = str(source.uuid) if kind == 'conversation' else str(source.pk)
        output.append({'kind': kind, 'id': opaque_id, 'source_method': association[1],
            'quality': 'unknown', 'crm_item_id': item.pk if item else None,
            'next_task_id': task_id, 'can_edit': editable,
            'manager_url': reverse('avito_crm_attribution', args=[connection.pk]) + '?' + urlencode({
                'conversation' if kind == 'conversation' else 'call': opaque_id})})
    latest = links.scope(actor, connection.pk)
    if (binding != projects._digest(projects._binding(latest))
            or links.owned_ads(latest, now) != known):
        raise PermissionDenied
    for row in output:
        source = links.source_for(actor, latest, row['kind'], row['id'])
        stored = links.link_for(latest, row['kind'], source)
        visible = links.visible_link(actor, latest, row['kind'], source)
        if stored is not None and visible is None:
            raise PermissionDenied
        association = _association(latest, row['kind'], source, visible, known, now)
        if (association is None or association != (WATER_ID, row['source_method'])
                or visible is not None and visible.quality != 'unknown'):
            raise PermissionDenied
    return {'state': 'available', 'avito_id': WATER_ID, 'checked_at': now.isoformat(),
        'rows': output, 'confirmed_water_sources_inspected': confirmed,
        'awaiting_quality_inspected': eligible, 'assessed_water_sources_inspected': assessed,
        'unknown_source_inspected': unknown,
        'scan_complete': len(dialogs) <= SCAN_LIMIT and len(call_links) <= SCAN_LIMIT,
        'display_complete': eligible <= DISPLAY_LIMIT,
        'calls_without_explicit_links_not_attributed': True}


def safe_owner_review_receipt():
    """For the existing protected operator only; no text, URLs or financial fields."""
    from .avito_services_workplace_setup import _actor, _connection
    try:
        connection = _connection(4)
        actor = _actor(connection)
        result = project_receipt(connection, actor)
    except Exception:
        return {'state': 'unavailable', 'code': 'owner_review_scope_or_source_unverified',
            'connection_id': 4, 'account_id': projects.ACCOUNT, 'provider_action_performed': False}
    try:
        queue = water_quality_queue(actor, connection)
        result['water_quality'] = {key: queue[key] for key in (
            'state', 'avito_id', 'checked_at', 'confirmed_water_sources_inspected',
            'awaiting_quality_inspected', 'assessed_water_sources_inspected',
            'unknown_source_inspected', 'scan_complete', 'display_complete')}
    except Exception:
        result['water_quality'] = {'state': 'unavailable', 'code': 'crm_access_or_source_unverified'}
    return result
