"""Append-only feed history. Snapshots are private and always reached through a scoped feed."""
from copy import deepcopy
import hashlib

from .avito_feed_models import AvitoServicesFeedVersion


def _rows(sheets):
    return [row for sheet in sheets for row in sheet['rows']]


def changes_between(before, after):
    previous = {row['AvitoId']: row for row in _rows(before)}
    current = {row['AvitoId']: row for row in _rows(after)}
    changes = []
    for identifier in sorted(previous.keys() | current.keys()):
        old, new = previous.get(identifier, {}), current.get(identifier, {})
        for field in sorted(old.keys() | new.keys()):
            if old.get(field) != new.get(field):
                changes.append({'avito_id': identifier, 'id': new.get('Id') or old.get('Id'),
                                'field': field, 'before': old.get(field), 'after': new.get(field)})
    return changes


def require_identities(feed, candidate):
    """Keep the Id↔AvitoId bijection and every previously known listing."""
    known, reverse = {}, {}
    def remember(sheets):
        for row in _rows(sheets):
            avito_id, item_id = row['AvitoId'], row['Id']
            if ((avito_id in known and known[avito_id] != item_id)
                    or (item_id in reverse and reverse[item_id] != avito_id)):
                raise ValueError('Нельзя менять соответствие Id и AvitoId. Нужна проверка истории источника.')
            known[avito_id], reverse[item_id] = item_id, avito_id
    remember(feed.draft)
    remember(feed.published)
    if feed.pk:
        for snapshot in feed.versions.values_list('snapshot', flat=True).iterator():
            remember(snapshot)
    candidate_map = {}
    candidate_reverse = {}
    for row in _rows(candidate):
        avito_id, item_id = row['AvitoId'], row['Id']
        if avito_id in candidate_map or item_id in candidate_reverse:
            raise ValueError('Повторяется Id или AvitoId.')
        candidate_map[avito_id], candidate_reverse[item_id] = item_id, avito_id
        if ((avito_id in known and known[avito_id] != item_id)
                or (item_id in reverse and reverse[item_id] != avito_id)):
            raise ValueError('Нельзя менять соответствие Id и AvitoId.')
    if not known.keys() <= candidate_map.keys():
        raise ValueError('Нельзя исключать известные объявления из черновика. Архивные строки сохраняются в истории и не отправляются повторно.')


def record_version(feed, action, before, snapshot, *, actor=None, reason='',
                   observations=None, source_version=None, content=None):
    if source_version is None and action == 'publish':
        source_version = feed.versions.filter(revision=feed.revision,
            action__in=('import', 'edit', 'restore', 'baseline')).order_by('-pk').first()
    elif source_version is None and action == 'verify':
        source_version = feed.versions.filter(action='publish',
            revision=feed.published_revision).order_by('-pk').first()
    if source_version is not None and source_version.feed_id != feed.pk:
        raise ValueError('Версия относится к другому источнику.')
    return AvitoServicesFeedVersion.objects.create(feed=feed, revision=feed.revision,
        published_revision=feed.published_revision, action=action, actor=actor, reason=reason,
        snapshot=deepcopy(snapshot), changes=changes_between(before, snapshot),
        observations=deepcopy(observations or []), source_version=source_version,
        file_digest=hashlib.sha256(content).hexdigest() if content is not None else '')


def ensure_baseline(feed):
    """Preserve existing snapshots lazily under the caller's feed/connection locks."""
    if feed.versions.exists():
        return
    record_version(feed, 'baseline', [], feed.draft,
                   reason='Снимок существующего черновика при запуске истории; прежние автор и время не восстановлены')
    if feed.published:
        from .avito_services_xml import xml_bytes
        record_version(feed, 'legacy_published', [], feed.published,
            reason='Снимок ранее опубликованного файла; обработка Авито не подтверждена',
            content=xml_bytes(feed.published, max_images=feed.published_photo_limit))


def restored_snapshot(feed, version):
    """Restore old contents into a new complete draft, retaining later added rows."""
    if version.feed_id != feed.pk:
        raise ValueError('Версия относится к другому источнику.')
    old = {row['AvitoId']: deepcopy(row) for row in _rows(version.snapshot)}
    # A former publication may have omitted an inactive row. Never delete it here.
    result = deepcopy(feed.draft)
    seen = set()
    for sheet in result:
        for offset, row in enumerate(sheet['rows']):
            identifier = row['AvitoId']
            if identifier in old:
                sheet['rows'][offset] = old[identifier]
            seen.add(identifier)
    if old.keys() - seen:
        # Imports currently cannot drop identities, so missing rows indicate a
        # corrupted/unrelated snapshot; do not guess their category sheet.
        raise ValueError('Версия содержит строки вне текущего черновика. Нужна проверка источника.')
    require_identities(feed, result)
    return result
