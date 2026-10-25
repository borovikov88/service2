"""Read-only coverage of existing explicit links; never guessed sales attribution."""
from datetime import timedelta
import re

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_models import Conversation
from .communication_services import organization_access, conversation_capability
from .avito_feed_models import AvitoServicesFeed
from .avito_services_feed import bound, entries

LIMIT = 500


def _identifier(value):
    if type(value) is int and 0 < value < 10**20:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value):
        return value
    return None


def coverage(connection, actor, now=None):
    now = now or timezone.now()
    if (connection.pk != 4 or str(connection.external_id) != '446312402'
            or not connection.is_active or not connection.channel.is_active
            or connection.channel.kind != 'avito'
            or organization_access(actor, connection.channel.organization) is None
            or not conversation_capability(actor, 'can_view_conversations', connection.channel.organization)
            or not conversation_capability(actor, 'can_manage_channels', connection.channel.organization)):
        return None
    feed = AvitoServicesFeed.objects.filter(connection=connection).first()
    known = {}
    if feed and feed.enabled and bound(feed, connection):
        pairs = {(row['Id'], row['AvitoId']) for row in entries(feed.published)}
        for row in feed.latest_observations if isinstance(feed.latest_observations, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                checked = parse_datetime(row.get('checked_at', ''))
            except (TypeError, ValueError):
                checked = None
            if (checked is not None and timezone.is_aware(checked)
                    and now-timedelta(days=1) <= checked <= now
                    and row.get('source') == 'avito_item_detail'
                    and (row.get('id'), row.get('avito_id')) in pairs
                    and row.get('status') in {'active', 'old', 'removed', 'blocked', 'rejected'}):
                known[row['avito_id']] = checked
    query = Conversation.objects.filter(connection=connection,
        organization_id=connection.channel.organization_id)
    total = query.count()
    rows = list(query.order_by('pk').values(
        'pk', 'provider_context', 'context_synced_at', 'client_id', 'client__organization_id')[:LIMIT])
    from .models import Client
    from .client_crm_views import _can_view_client_card
    linked_ids = {row['client_id'] for row in rows if row['client_id'] is not None
                  and row['client__organization_id'] == connection.channel.organization_id}
    visible_clients = {client.pk for client in Client.objects.filter(
        pk__in=linked_ids, organization_id=connection.channel.organization_id).only('pk', 'organization_id')
        if _can_view_client_card(actor, client)}
    clients, confirmed, client_links, full_links = set(), 0, 0, 0
    by_ad = {}
    for row in rows:
        context = row['provider_context'] if isinstance(row['provider_context'], dict) else {}
        item = context.get('item') if isinstance(context.get('item'), dict) else {}
        identifier = _identifier(item.get('id'))
        synced = row['context_synced_at']
        source_confirmed = (identifier in known and synced is not None
            and timezone.is_aware(synced) and synced <= now)
        client_confirmed = row['client_id'] in visible_clients
        if client_confirmed:
            client_links += 1
            clients.add(row['client_id'])
        if source_confirmed:
            confirmed += 1
            full_links += bool(client_confirmed)
            bucket = by_ad.setdefault(identifier, {'avito_id': identifier, 'dialogs': 0,
                'linked_client_dialogs': 0, 'client_ids': set(), 'ownership_checked_at': known[identifier]})
            bucket['dialogs'] += 1
            if client_confirmed:
                bucket['linked_client_dialogs'] += 1
                bucket['client_ids'].add(row['client_id'])
    return {
        'source': 'stored_provider_context.item.id_and_owned_ad_observation',
        'period': 'all_existing_service2_dialogs', 'checked_at': now,
        'total_dialogs': total, 'inspected_dialogs': len(rows), 'complete': total <= LIMIT,
        'ad_source_confirmed_dialogs': confirmed, 'ad_source_unknown_dialogs': len(rows)-confirmed,
        'dialogs_with_explicit_client': client_links, 'distinct_client_cards': len(clients),
        'ad_dialog_client_chains': full_links,
        'ads': [{key: value for key, value in row.items() if key != 'client_ids'}
                | {'distinct_client_cards': len(row['client_ids'])} for row in by_ad.values()],
        'call_ad_links': None, 'lead_links': None, 'order_links': None,
        'payment_links': None, 'revenue': None,
    }
