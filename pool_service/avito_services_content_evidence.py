"""Append-only, field-scoped human evidence; never provider/source verification."""
from datetime import datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from django import forms
from django.db import transaction
from django.db.models import TextField
from django.db.models.functions import Cast, Length
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from .avito_publication_models import AvitoServicesPublication
from .avito_services_history import record_version
from .avito_services_publication import _binding_valid
from .avito_services_xml import xml_bytes

KEY = 'manual_photo_evidence_v1'
CONNECTION_ID = 4
ACCOUNT_ID = '446312402'
BARNAUL = ZoneInfo('Asia/Barnaul')
SOURCE_LABELS = {
    'owner_reported_screenshot': 'Сообщение владельца о скриншоте',
    'reviewer_observation': 'Ручная сверка менеджером',
}
METHODS = {'manager_form', 'protected_operator_owner_report'}
MAX_EVENTS = 200
OWNER_ASSET_KEY_SHA256 = '9416751d05f7db36dc6f666b35e3bc2e4c3ab4d8cc39308f68506802d27faa1d'
REPORT_ONCE_KEY = 'avito_photo10_owner_report_read_v1'
BODY_KEYS = {
    'schema_version', 'connection_id', 'channel_id', 'organization_id', 'account_id',
    'publication_version_id', 'publication_revision', 'publication_file_sha256',
    'xml_id', 'avito_id', 'photo_number', 'photo_url_sha256',
    'source_kind', 'source_filename', 'observed_at', 'observed_precision',
    'observed_timezone', 'evidence_file_retained', 'recording_method',
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _fingerprint(body):
    return _digest({key: value for key, value in body.items() if key != 'recording_method'})


def _scope(connection):
    return (connection.pk == CONNECTION_ID and str(connection.external_id) == ACCOUNT_ID
        and connection.is_active and connection.channel.is_active
        and connection.channel.kind == 'avito')


def _filename(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9А-Яа-яЁё_. ()-]{1,120}\.(?:jpe?g|png|webp)', value, re.IGNORECASE)
            or '..' in value or len(value) > 128):
        raise ValueError('Укажите только имя файла доказательства, без пути или ссылки.')
    return value


def _observed(value, publication, *, now=None):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError('Нужны дата и время наблюдения с часовым поясом Барнаула.')
    value = value.astimezone(BARNAUL)
    if value.second or value.microsecond:
        raise ValueError('Время на скриншоте фиксируется с точностью до минуты.')
    if value < publication.version.created_at or value > (now or timezone.now()):
        raise ValueError('Наблюдение должно быть после публикации и не позже текущего времени.')
    return value


def _photo(publication, avito_id, number):
    from .avito_services_feed import entries, images
    if type(number) is not int or not 1 <= number <= 10:
        raise ValueError('Укажите номер одной фотографии от 1 до 10.')
    rows = [row for row in entries(publication.version.snapshot) if row.get('AvitoId') == avito_id]
    if len(rows) != 1:
        raise ValueError('Объявление не принадлежит выбранной опубликованной версии.')
    urls = images(rows[0].get('ImageUrls', '')).split(' | ')
    if number > len(urls):
        raise ValueError('В этой версии нет фотографии с таким номером.')
    return rows[0], hashlib.sha256(urls[number - 1].encode()).hexdigest(), urls[number - 1]


def _publication(connection, feed, version_id, *, require_current):
    publication = AvitoServicesPublication.objects.select_related('version__feed__connection__channel').filter(
        version_id=version_id, version__feed=feed, account_id=ACCOUNT_ID,
        organization_id=connection.channel.organization_id).first()
    try:
        valid = (publication is not None and _binding_valid(publication, connection)
            and hashlib.sha256(xml_bytes(publication.version.snapshot)).hexdigest() == publication.file_sha256)
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError('Опубликованная версия и её привязка не подтверждены.')
    if require_current and (feed.published_revision != publication.version.revision
            or feed.published != publication.version.snapshot):
        raise ValueError('Опубликованная версия изменилась. Обновите страницу.')
    return publication


def _events(feed, version):
    # Read only bounded metadata, not repeated full snapshots.
    return list(feed.versions.filter(action='verify', source_version=version).annotate(
        evidence_bytes=Length(Cast('changes', output_field=TextField()))).filter(
            evidence_bytes__lte=8192).order_by('-pk').values(
                'pk', 'source_version_id', 'actor_id', 'created_at', 'changes')[:MAX_EVENTS + 1])


def _valid_body(value, connection, publication):
    if (not isinstance(value, dict) or set(value) != BODY_KEYS
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['connection_id'] != connection.pk or value['channel_id'] != connection.channel_id
            or value['organization_id'] != connection.channel.organization_id
            or value['account_id'] != str(connection.external_id)
            or value['publication_version_id'] != publication.version_id
            or value['publication_revision'] != publication.version.revision
            or value['publication_file_sha256'] != publication.file_sha256
            or not isinstance(value['source_kind'], str) or value['source_kind'] not in SOURCE_LABELS
            or not isinstance(value['recording_method'], str) or value['recording_method'] not in METHODS
            or value['observed_precision'] != 'minute'
            or value['observed_timezone'] != 'Asia/Barnaul'
            or value['evidence_file_retained'] is not False):
        return False
    try:
        _filename(value['source_filename'])
        observed = parse_datetime(value['observed_at'])
        _observed(observed, publication)
        row, digest, unused = _photo(publication, value['avito_id'], value['photo_number'])
        return row['Id'] == value['xml_id'] and digest == value['photo_url_sha256']
    except (ValueError, TypeError, KeyError):
        return False


def _payload(event):
    changes = event.get('changes')
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict):
        return None
    entry = changes[0]
    if entry.get('kind') != KEY or not isinstance(entry.get('evidence'), dict):
        return None
    body = entry['evidence']
    if entry.get('fingerprint') != _fingerprint(body):
        return None
    return body


def is_manual_photo_event(event):
    changes = event.changes if isinstance(event, AvitoServicesFeedVersion) else event.get('changes')
    return (isinstance(changes, list) and len(changes) == 1 and isinstance(changes[0], dict)
            and changes[0].get('kind') == KEY)


def record_photo_evidence(connection, actor, *, publication_version_id, avito_id, photo_number,
                          source_kind, source_filename, observed_at,
                          recording_method='manager_form', require_current=True):
    """One photo assertion, no draft/publication/provider-proof changes."""
    from .avito_services_feed import _publication_connection, bound
    if (type(publication_version_id) is not int or publication_version_id <= 0
            or not isinstance(source_kind, str) or source_kind not in SOURCE_LABELS
            or not isinstance(recording_method, str) or recording_method not in METHODS):
        raise ValueError('Некорректный источник частичного подтверждения.')
    filename = _filename(source_filename)
    with transaction.atomic():
        current, actor = _publication_connection(connection.pk, actor, locked=True)
        if not _scope(current):
            raise ValueError('Ручное подтверждение доступно только для подключения услуг №4.')
        feed = AvitoServicesFeed.objects.select_for_update().get(connection=current)
        if not bound(feed, current):
            raise ValueError('Привязка аккаунта или организации изменилась.')
        publication = _publication(current, feed, publication_version_id, require_current=require_current)
        observed = _observed(observed_at, publication)
        row, photo_digest, unused = _photo(publication, avito_id, photo_number)
        body = {
            'schema_version': 1, 'connection_id': current.pk, 'channel_id': current.channel_id,
            'organization_id': current.channel.organization_id, 'account_id': str(current.external_id),
            'publication_version_id': publication.version_id, 'publication_revision': publication.version.revision,
            'publication_file_sha256': publication.file_sha256, 'xml_id': row['Id'],
            'avito_id': avito_id, 'photo_number': photo_number, 'photo_url_sha256': photo_digest,
            'source_kind': source_kind, 'source_filename': filename,
            'observed_at': observed.isoformat(), 'observed_precision': 'minute',
            'observed_timezone': 'Asia/Barnaul', 'evidence_file_retained': False,
            'recording_method': recording_method,
        }
        fingerprint = _fingerprint(body)
        previous = _events(feed, publication.version)
        for event in previous:
            existing = _payload(event)
            if (existing is not None and _valid_body(existing, current, publication)
                    and _fingerprint(existing) == fingerprint):
                return event['pk'], False
        if len(previous) >= MAX_EVENTS:
            raise ValueError('Достигнут лимит подтверждений этой версии; нужна проверка журнала.')
        event = record_version(feed, 'verify', publication.version.snapshot, publication.version.snapshot, actor=actor,
            reason='Частичная ручная сверка фотографии; статусы API и весь файл не подтверждаются',
            source_version=publication.version, extra_changes=[{
                'kind': KEY, 'avito_id': avito_id, 'id': row['Id'], 'field': 'manual_photo_review',
                'before': '', 'after': f'Ручная сверка фото №{photo_number}; {SOURCE_LABELS[source_kind]}',
                'fingerprint': fingerprint, 'evidence': body,
            }])
        return event.pk, True


class PhotoEvidenceForm(forms.Form):
    publication_version_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    avito_id = forms.ChoiceField(label='Объявление опубликованной версии',
        widget=forms.Select(attrs={'class': 'form-select'}))
    photo_number = forms.IntegerField(label='Номер фотографии', min_value=1, max_value=10,
        widget=forms.NumberInput(attrs={'class': 'form-control'}))
    source_kind = forms.ChoiceField(label='Источник подтверждения', choices=list(SOURCE_LABELS.items()),
        widget=forms.Select(attrs={'class': 'form-select'}))
    source_filename = forms.CharField(label='Имя файла доказательства', max_length=128,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '1000025527.jpg'}))
    observed_at = forms.CharField(label='Дата и время наблюдения · Барнаул',
        widget=forms.TextInput(attrs={'class': 'form-control', 'type': 'datetime-local'}))

    def __init__(self, publication, *args, **kwargs):
        from .avito_services_feed import entries
        super().__init__(*args, **kwargs)
        self.fields['avito_id'].choices = [(row['AvitoId'], row['AvitoId'] + ' · ' + row['Title'])
                                           for row in entries(publication.version.snapshot)]
        self.fields['publication_version_id'].initial = publication.version_id
        self.fields['photo_number'].initial = 10
        self.fields['observed_at'].initial = timezone.localtime(timezone.now(), BARNAUL).strftime('%Y-%m-%dT%H:%M')

    def clean_source_filename(self):
        return _filename(self.cleaned_data['source_filename'])

    def clean_observed_at(self):
        value = self.cleaned_data['observed_at']
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', value):
            raise forms.ValidationError('Введите время с точностью до минуты по Барнаулу.')
        try:
            return datetime.strptime(value, '%Y-%m-%dT%H:%M').replace(tzinfo=BARNAUL)
        except ValueError:
            raise forms.ValidationError('Некорректная дата или время.') from None


def manual_photo_context(connection, feed, published_version=None):
    result = {'available': False, 'records': [], 'incomplete': False, 'form': None}
    if not feed or not published_version or not _scope(connection):
        return result
    try:
        publication = _publication(connection, feed, published_version.pk, require_current=True)
        previous = _events(feed, publication.version)
        result['incomplete'] = len(previous) > MAX_EVENTS
        for event in previous[:MAX_EVENTS]:
            body = _payload(event)
            if body is None or not _valid_body(body, connection, publication):
                continue
            result['records'].append({
                'avito_id': body['avito_id'], 'photo_number': body['photo_number'],
                'source_label': SOURCE_LABELS[body['source_kind']], 'source_filename': body['source_filename'],
                'observed_at': parse_datetime(body['observed_at']).astimezone(BARNAUL).strftime('%d.%m.%Y %H:%M'),
                'recorded_at': event['created_at'].astimezone(BARNAUL).strftime('%d.%m.%Y %H:%M:%S'), 'history_id': event['pk'],
            })
        result.update(available=True, form=PhotoEvidenceForm(publication))
    except (ValueError, KeyError, TypeError):
        pass
    return result


def handle_photo_evidence_post(request, connection, feed):
    from .avito_services_feed import _page_context
    version = AvitoServicesPublication.objects.select_related('version__feed').filter(
        version__feed=feed).order_by('-version__created_at', '-pk').first()
    if version is None:
        return render(request, 'pool_service/avito/services_feed.html',
            _page_context(request, connection, feed, 'Опубликованная версия не найдена.'), status=409)
    form = PhotoEvidenceForm(version, request.POST)
    error = ''
    if form.is_valid():
        try:
            record_photo_evidence(connection, request.user, **form.cleaned_data)
            return redirect('avito_services_feed', connection_id=connection.pk)
        except ValueError as exc:
            error = str(exc)
    else:
        error = 'Проверьте поля частичного подтверждения фотографии.'
    feed.refresh_from_db()
    context = _page_context(request, connection, feed, error)
    context['manual_photo']['form'] = form
    return render(request, 'pool_service/avito/services_feed.html', context, status=400)


def record_owner_photo_report():
    """The user's already authorised statement; no claim to reading screenshot bytes."""
    from .communication_models import ChannelConnection
    from .avito_services_workplace_setup import _actor
    from .avito_services_media import _asset
    from .avito_services_feed import _publication_connection
    connection = ChannelConnection.objects.select_related('channel').get(pk=CONNECTION_ID)
    actor = _actor(connection)
    with transaction.atomic():
        current, unused = _publication_connection(CONNECTION_ID, actor, locked=True)
        feed = AvitoServicesFeed.objects.select_for_update().get(connection=current)
        publication = _publication(current, feed, 7, require_current=False)
        if publication.version.revision != 18:
            raise ValueError('Версия подтверждения изменилась.')
        unused, unused_digest, url = _photo(publication, '8343962135', 10)
        asset = _asset(current, url)
        if (asset is None or asset.published_in_feed_at is None
                or hashlib.sha256(str(asset.public_key).encode()).hexdigest()
                    != OWNER_ASSET_KEY_SHA256):
            raise ValueError('Фотография не соответствует согласованному опубликованному объекту.')
        event_id, created = record_photo_evidence(current, actor,
            publication_version_id=7, avito_id='8343962135', photo_number=10,
            source_kind='owner_reported_screenshot', source_filename='1000025527.jpg',
            observed_at=datetime(2026, 10, 25, 14, 13, tzinfo=BARNAUL),
            recording_method='protected_operator_owner_report', require_current=False)
        event = feed.versions.get(pk=event_id)
    return {'schema_version': 1, 'kind': 'avito_manual_photo_evidence', 'connection_id': CONNECTION_ID,
        'account_id': ACCOUNT_ID, 'publication_revision': 18, 'publication_version_id': 7,
        'history_event_id': event_id, 'created': created, 'avito_id': '8343962135', 'photo_number': 10,
        'source_kind': 'owner_reported_screenshot', 'source_filename': '1000025527.jpg',
        'observed_at': '2026-10-25T14:13:00+07:00', 'observed_precision': 'minute',
        'observed_timezone': 'Asia/Barnaul', 'recorded_at': event.created_at.isoformat(),
        'evidence_file_retained': False, 'global_content_verified': False,
        'provider_source_confirmed_by_manual_evidence': False}


def _report_context(*, locked=False):
    from .avito_services_feed import _publication_connection
    from .avito_services_workplace_setup import _actor, _binding
    from .communication_models import ChannelConnection
    from .communication_avito import _provider_root
    if _provider_root() != 'https://api.avito.ru':
        raise ValueError('manual_report_provider_root_invalid')
    query = ChannelConnection.objects.select_related('channel', 'avito_credential')
    if locked:
        query = query.select_for_update()
    connection = query.get(pk=CONNECTION_ID)
    actor = _actor(connection)
    connection, actor = _publication_connection(CONNECTION_ID, actor, locked=locked)
    if not _scope(connection):
        raise ValueError('manual_report_scope_invalid')
    feeds = AvitoServicesFeed.objects.select_for_update() if locked else AvitoServicesFeed.objects
    feed = feeds.get(connection=connection)
    publication = _publication(connection, feed, 7, require_current=True)
    if publication.version.revision != 18:
        raise ValueError('manual_report_version_changed')
    stamp = _digest({'binding': _binding(connection), 'actor_id': actor.pk, 'feed_id': feed.pk,
        'public_key': str(feed.public_key), 'revision': feed.revision,
        'published_revision': feed.published_revision, 'photo_limit': feed.published_photo_limit,
        'enabled': feed.enabled, 'published': feed.published,
        'file_sha256': publication.file_sha256, 'version_id': publication.version_id})
    return connection, feed, publication, stamp


def _safe_report_error(exc):
    from .communication_avito import AvitoError
    value = str(exc) if isinstance(exc, AvitoError) else ''
    return value if re.fullmatch(r'(?:provider|autoload_report)_[a-z0-9_]{1,80}', value) else 'provider_report_unavailable'


def _report_result(state='not_checked'):
    return {'state': state, 'upload_id': None, 'started_at': None, 'upload_status': None,
        'complete': None, 'finished': None, 'input_copy_state': None,
        'input_copy_code': None, 'target': None, 'error_code': ''}


def _read_owner_report_once(manual):
    from .communication_avito import access_token, authorized_account_id
    from .avito_autoload_report import fetch_report
    from .avito_services_publication import correlate_report, publication_facts, _date
    facts = dict.fromkeys(('file_published', 'upload_found', 'source_confirmed',
                          'rows_processed', 'live_content_verified'), False)
    result = _report_result()
    stamp = None
    try:
        with transaction.atomic():
            connection, feed, publication, stamp = _report_context(locked=True)
            facts = publication_facts(publication, publication.attempts.first())
            metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
            saved = metadata.get(REPORT_ONCE_KEY)
            if saved is not None:
                result['state'] = ('already_checked' if isinstance(saved, dict)
                    and saved.get('stamp') == stamp and saved.get('state') == 'finished'
                    else 'prior_check_unfinished_or_binding_changed')
                return result, facts
            metadata[REPORT_ONCE_KEY] = {'state': 'pending', 'stamp': stamp,
                'history_event_id': manual['history_event_id'], 'started_at': timezone.now().isoformat()}
            connection.settings = metadata
            connection.save(update_fields=['settings'])
        # The manual event committed before these provider reads. No DB lock spans I/O.
        if authorized_account_id(connection) != ACCOUNT_ID:
            raise ValueError('manual_report_account_mismatch')
        report = fetch_report(access_token(connection), ACCOUNT_ID, kind='current',
            include_input_proof=True, input_proof_connection=connection, allow_shop_recovery=False)
        started = _date(report.get('started_at'))
        if started is None:
            raise ValueError('manual_report_time_unknown')
        result.update(upload_id=report['upload_id'], started_at=started.isoformat(),
            upload_status=report.get('upload_status'), complete=report.get('complete') is True,
            finished=report.get('upload_finished') is True)
        proof = report.get('input_copy_proof')
        if isinstance(proof, dict):
            result['input_copy_state'] = proof.get('state') if proof.get('state') in {
                'verified_xml', 'unavailable'} else None
            code = proof.get('code')
            result['input_copy_code'] = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', code) else None
        target = next((row for row in report.get('rows', []) if str(row.get('avito_id')) == '8343962135'), None)
        if target:
            result['target'] = {'avito_id': '8343962135', 'status': target.get('avito_status'),
                'messages': [{'type': item['type'], 'code': item['code']}
                             for item in target.get('messages', [])]}
        result['state'] = ('predates_publication' if started <= publication.version.created_at else
            'later_unfinished' if report.get('complete') is not True or report.get('upload_finished') is not True
            else 'later_completed')
        with transaction.atomic():
            current, current_feed, current_publication, current_stamp = _report_context(locked=True)
            if current_stamp != stamp:
                raise ValueError('manual_report_binding_changed')
            if result['state'] == 'later_completed':
                correlate_report(current, report)
            facts = publication_facts(current_publication, current_publication.attempts.first())
            metadata = dict(current.settings)
            saved = metadata.get(REPORT_ONCE_KEY)
            if not isinstance(saved, dict) or saved.get('stamp') != stamp or saved.get('state') != 'pending':
                raise ValueError('manual_report_reservation_changed')
            metadata[REPORT_ONCE_KEY] = {**saved, 'state': 'finished',
                'checked_at': timezone.now().isoformat(), 'result_state': result['state']}
            current.settings = metadata
            current.save(update_fields=['settings'])
    except Exception as exc:
        result['state'], result['error_code'] = 'unavailable', _safe_report_error(exc)
        facts = dict.fromkeys(facts, False)
        # Mark a failed read once only if the same context still holds.
        try:
            with transaction.atomic():
                current, unused_feed, unused_publication, current_stamp = _report_context(locked=True)
                metadata = dict(current.settings)
                saved = metadata.get(REPORT_ONCE_KEY)
                if (stamp is not None and current_stamp == stamp and isinstance(saved, dict)
                        and saved.get('stamp') == stamp and saved.get('state') == 'pending'):
                    facts = publication_facts(unused_publication, unused_publication.attempts.first())
                    metadata[REPORT_ONCE_KEY] = {**saved, 'state': 'finished',
                        'checked_at': timezone.now().isoformat(), 'result_state': 'unavailable'}
                    current.settings = metadata
                    current.save(update_fields=['settings'])
        except Exception:
            pass
    return result, facts


def record_owner_photo_report_once():
    """Protected deploy setup hook. Evidence success is independent of a provider read."""
    try:
        manual = record_owner_photo_report()
    except Exception:
        return {'schema_version': 1, 'state': 'ineligible', 'code': 'manual_photo_evidence_not_eligible',
                'connection_id': CONNECTION_ID, 'account_id': ACCOUNT_ID}
    report, facts = _read_owner_report_once(manual)
    return {'schema_version': 1, 'state': 'recorded', 'code': '',
        'connection_id': CONNECTION_ID, 'account_id': ACCOUNT_ID,
        'manual_photo': manual, 'provider_report': report, 'facts': facts}
