"""Read-only, fixed-shape readiness for the existing-photo draft check.

This diagnoses the same prerequisites. It never repairs a draft, relaxes its
round-trip guard, creates a token, saves an image or calls the signed POST.
"""
import time

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import Http404

from . import avito_services_media as media
from .avito_feed_models import AvitoServicesFeed
from .avito_services_media_livecheck import LiveMediaCheckError
from .communication_avito import AvitoError
from .communication_models import ChannelConnection
from .communication_services import conversation_capability, organization_access

CHECKS = (
    'connection_scope', 'actor_active', 'editor_organization', 'manage_channels',
    'setup_binding', 'feed_binding', 'published_revision', 'shared_identity',
    'shared_photo', 'photo_scope', 'form_valid', 'title_roundtrip',
    'description_roundtrip', 'image_urls_roundtrip', 'source_read', 'image_decode',
)
REASONS = {
    'connection_scope': 'media_connection_invalid',
    'actor_active': 'media_actor_invalid',
    'editor_organization': 'media_editor_organization_mismatch',
    'manage_channels': 'media_capability_denied',
    'setup_binding': 'media_setup_binding_invalid',
    'feed_binding': 'media_feed_invalid',
    'published_revision': 'media_published_revision_mismatch',
    'shared_identity': 'media_shared_identity_missing',
    'shared_photo': 'media_shared_photo_missing',
    'photo_scope': 'media_photo_scope_invalid',
    'form_valid': 'media_form_invalid',
    'title_roundtrip': 'media_title_roundtrip_changed',
    'description_roundtrip': 'media_description_roundtrip_changed',
    'image_urls_roundtrip': 'media_image_urls_roundtrip_changed',
    'source_read': 'media_source_read_failed',
    'image_decode': 'media_image_decode_failed',
}
EXCEPTION_CLASSES = frozenset({
    'PermissionDenied', 'Http404', 'LiveMediaCheckError', 'AvitoError', 'ValueError',
    'OSError', 'DatabaseError', 'TypeError', 'KeyError', 'AttributeError', 'OtherError',
})


def _exception_class(error):
    for kind in (PermissionDenied, Http404, LiveMediaCheckError, AvitoError,
                 DatabaseError, OSError, ValueError, TypeError, KeyError, AttributeError):
        if isinstance(error, kind):
            return kind.__name__
    return 'OtherError'


def media_release_readiness(connection):
    """No URLs, content, IDs, tokens, digests, exception messages or writes."""
    from .avito_services_feed import bound, entries, ContentForm
    from .avito_services_workplace_setup import KEY
    result = {'state': 'blocked', 'stage': 'connection_scope',
        'reason': REASONS['connection_scope'], 'exception_class': None,
        'checks': dict.fromkeys(CHECKS)}

    def guard(stage, valid):
        result['stage'], result['reason'] = stage, REASONS[stage]
        result['checks'][stage] = bool(valid)
        if not valid:
            raise LiveMediaCheckError('media_readiness_blocked')

    def begin(stage):
        result['stage'], result['reason'] = stage, REASONS[stage]

    try:
        current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
        guard('connection_scope', current.pk == 4 and current.external_id == '446312402'
            and current.is_active and current.channel.is_active and current.channel.kind == 'avito')
        begin('actor_active')
        marker = current.settings.get(KEY) if isinstance(current.settings, dict) else None
        actor_id = marker.get('actor_id') if isinstance(marker, dict) else None
        actor = User.objects.filter(pk=actor_id, is_active=True).first() if type(actor_id) is int else None
        guard('actor_active', actor is not None)
        begin('editor_organization')
        access = organization_access(actor)
        selected = access is not None and access.organization_id == current.channel.organization_id
        result['checks']['editor_organization'] = selected
        begin('manage_channels')
        capability = conversation_capability(actor, 'can_manage_channels', current.channel.organization)
        result['checks']['manage_channels'] = capability
        guard('editor_organization', selected)
        guard('manage_channels', capability)
        begin('setup_binding')
        owner, binding = media._storage_probe_context(current, actor.pk)
        guard('setup_binding', owner.pk == actor.pk)
        begin('feed_binding')
        feed = AvitoServicesFeed.objects.filter(connection=current).first()
        guard('feed_binding', feed is not None and feed.enabled and bound(feed, current))
        guard('published_revision', feed.published_revision == 15)
        begin('shared_identity')
        pairs = [(row, next((item for item in entries(feed.published)
            if item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId']), None))
            for row in entries(feed.draft)]
        guard('shared_identity', any(published is not None for row, published in pairs))
        begin('shared_photo')
        row = next((row for row, published in pairs if published is not None
            and any(url in published.get('ImageUrls', '').split(' | ')
                    for url in row.get('ImageUrls', '').split(' | ') if url)), None)
        guard('shared_photo', row is not None)
        begin('photo_scope')
        urls = media.validate_photo_scope(current, row['ImageUrls']).split(' | ')
        guard('photo_scope', True)
        begin('form_valid')
        form = ContentForm({'title': row['Title'], 'description': row['Description'],
            'image_urls': row['ImageUrls'], 'reason': ''})
        guard('form_valid', form.is_valid())
        for stage, field, original in (
                ('title_roundtrip', 'title', 'Title'),
                ('description_roundtrip', 'description', 'Description'),
                ('image_urls_roundtrip', 'image_urls', 'ImageUrls')):
            result['checks'][stage] = form.cleaned_data[field] == row[original]
        for stage in ('title_roundtrip', 'description_roundtrip', 'image_urls_roundtrip'):
            guard(stage, result['checks'][stage])
        begin('source_read')
        published = next(item for item in entries(feed.published)
            if item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId'])
        allowed = [url for url in urls if url in published.get('ImageUrls', '').split(' | ')]
        source = next((url for url in allowed if media._asset(current, url) is None), allowed[0])
        asset = media._asset(current, source)
        raw = media._read_asset(asset) if asset else media._download(source, time.monotonic() + 10)
        guard('source_read', True)
        begin('image_decode')
        media._image(raw, normalize=False)
        guard('image_decode', True)
        result.update(state='ready', stage='complete', reason='media_ready')
    except Exception as error:
        # Failed stages and exception families are fixed identifiers only.
        if result['checks'][result['stage']] is None:
            result['checks'][result['stage']] = False
        result['exception_class'] = _exception_class(error)
    return result
