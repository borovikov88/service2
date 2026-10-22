"""Preserve Avito's XLSX category fields; only edit requested content fields.

Imports and drafts never change the publicly served published snapshot.
No provider URL is fetched from a workbook and no raw workbook is persisted.
"""
from copy import deepcopy
from io import BytesIO
import re
from urllib.parse import urlsplit
from zipfile import ZipFile, BadZipFile
from xml.etree.ElementTree import ParseError
from openpyxl.utils.exceptions import InvalidFileException

from openpyxl import load_workbook, Workbook
from openpyxl.utils.cell import column_index_from_string
from django import forms
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .avito_feed_models import AvitoServicesFeed
from .avito_services_xml import xml_bytes
from .avito_management import _scope
from .communication_models import ChannelConnection

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 200
MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def images(value):
    urls = [part.strip() for part in re.split(r'[|\r\n]+', value) if part.strip()]
    if not 1 <= len(urls) <= 30:
        raise ValueError('Укажите от 1 до 30 ссылок на фотографии.')
    for url in urls:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {'https', 'http'} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or len(url) > 2000 or any(c.isspace() for c in url):
            raise ValueError('Некорректная ссылка на фотографию.')
    result = ' | '.join(urls)
    if len(result) > 32767:
        raise ValueError('Список фотографий длиннее 32767 символов.')
    return result


def parse_export(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('Файл больше 4 МБ.')
    try:
        with ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 500 or sum(i.file_size for i in infos) > 20 * MAX_BYTES:
                raise ValueError('Превышен допустимый размер распакованного файла.')
            # Reject DTD/entity expansion even when optional XML hardening is absent.
            for info in infos:
                if info.filename.endswith('.xml'):
                    content = archive.read(info).replace(b'\x00', b'').upper()
                    if info.filename.startswith('xl/worksheets/'):
                        for column, row in re.findall(rb'<(?:[A-Z0-9_]+:)?C\b[^>]*\bR=[\"\']([A-Z]+)([0-9]+)[\"\']', content):
                            if len(column) > 3 or column_index_from_string(column.decode()) > 150 or int(row) > 2000:
                                raise ValueError('Превышены размеры листа.')
                    if b'<!DOCTYPE' in content or b'<!ENTITY' in content:
                        raise ValueError('XML с DTD не поддерживается.')
        book = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        result, ids, avito_ids = [], set(), set()
        if len(book.sheetnames) > 50:
            book.close()
            raise ValueError('Слишком много листов.')
        total_cells = 0
        try:
            for sheet in book:
                # Avito reverse exports may declare A1:A1 despite containing full
                # sheets. Ignore the dimension hint, but bound actual iteration.
                sheet.reset_dimensions()
                rows = list(sheet.iter_rows(max_row=2001, max_col=151, values_only=True))
                while rows and not any(v is not None for v in rows[-1]):
                    rows.pop()
                total_cells += sum(sum(v is not None for v in row) for row in rows)
                if total_cells > 100000 or len(rows) > 2000 or any(row[150] is not None for row in rows):
                    raise ValueError('Превышены размеры листа.')
                width = max((i + 1 for row in rows for i, v in enumerate(row) if v is not None), default=0)
                rows = [row[:width] for row in rows]
                if len(rows) < 5 or 'AvitoId' not in rows[1]:
                    continue
                headers = [str(h) if h is not None else '' for h in rows[1]]
                named = [h for h in headers if h]
                if len(named) != len(set(named)) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', h) for h in named):
                    raise ValueError('Некорректные заголовки столбцов.')
                entries = []
                for row in rows[4:]:
                    if not any(v is not None for v in row):
                        continue
                    data = {h: str(v) if v is not None else '' for h, v in zip(headers, row) if h}
                    if any(len(v) > 32767 for v in data.values()):
                        raise ValueError('Значение ячейки длиннее 32767 символов.')
                    if any(v.startswith('=') for v in data.values()):
                        raise ValueError('Формулы в данных объявлений не поддерживаются.')
                    if data.get('Category') != 'Предложение услуг' or data.get('AvitoStatus') != 'Активно':
                        raise ValueError('Импорт поддерживает только активные объявления услуг.')
                    if not re.fullmatch(r'[0-9]{1,20}', data.get('AvitoId', '')) or not data.get('Id'):
                        raise ValueError('В каждой строке нужны Id и AvitoId существующего объявления.')
                    if data['Id'] in ids or data['AvitoId'] in avito_ids:
                        raise ValueError('Повторяется Id или AvitoId.')
                    ids.add(data['Id']); avito_ids.add(data['AvitoId'])
                    if len(ids) > MAX_ROWS:
                        raise ValueError('Поддерживается не более 200 объявлений.')
                    if not data.get('Title') or not data.get('Description'):
                        raise ValueError('Отсутствует заголовок или описание.')
                    data['ImageUrls'] = images(data.get('ImageUrls', ''))
                    if data.get('ImageNames'):
                        raise ValueError('Используйте ссылки на фото, а не локальные имена файлов.')
                    if data.get('ListingFee') not in {'', 'Package'}:
                        raise ValueError('Разрешено только размещение из пакета (Package).')
                    entries.append(data)
                if entries:
                    result.append({'name': sheet.title, 'intro': str(rows[0][0] or ''),
                                   'headers': headers, 'required': [str(v) if v is not None else None for v in rows[2]],
                                   'hints': [str(v) if v is not None else None for v in rows[3]], 'rows': entries})
        finally:
            book.close()
    except (BadZipFile, KeyError, TypeError, OSError, ParseError, InvalidFileException, IndexError) as error:
        raise ValueError('Не удалось прочитать экспорт Авито XLSX.') from error
    if not result:
        raise ValueError('В файле нет активных объявлений услуг.')
    return result


def workbook_bytes(sheets):
    if any(len(str(value)) > 32767 for row in entries(sheets) for value in row.values()):
        raise ValueError('Значение ячейки длиннее 32767 символов.')
    book = Workbook()
    book.remove(book.active)
    for data in sheets:
        sheet = book.create_sheet(data['name'])
        sheet.append([data['intro']])
        sheet.append(data['headers'])
        sheet.append(data['required'])
        sheet.append(data['hints'])
        for entry in data['rows']:
            sheet.append([entry.get(h, '') for h in data['headers']])
        # User strings must never become formulas or external workbook links.
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
    output = BytesIO()
    book.save(output)
    return output.getvalue()


class ContentForm(forms.Form):
    title = forms.CharField(label='Заголовок', max_length=100)
    description = forms.CharField(label='Описание', max_length=32767, widget=forms.Textarea(attrs={'rows': 12}))
    image_urls = forms.CharField(label='Фотографии: одна ссылка на строку', max_length=32767, widget=forms.Textarea(attrs={'rows': 7}))

    def clean_image_urls(self):
        try:
            return images(self.cleaned_data['image_urls'])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error


def connection_for(request, connection_id):
    return get_object_or_404(ChannelConnection, pk=connection_id, channel__organization=_scope(request),
                            channel__kind='avito', is_active=True, channel__is_active=True)


def bound(feed, connection):
    return feed.account_id == connection.external_id and feed.organization_id_at_import == connection.channel.organization_id


def entries(sheets):
    return [row for sheet in sheets for row in sheet['rows']]


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def services_feed(request, connection_id):
    connection = connection_for(request, connection_id)
    feed = AvitoServicesFeed.objects.filter(connection=connection).first()
    error = ''
    if feed and not bound(feed, connection):
        return HttpResponse('Аккаунт подключения изменился. Старый источник заблокирован.', status=409)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            imported = None
            if action == 'import':
                upload = request.FILES.get('file')
                if not upload or upload.size > MAX_BYTES:
                    raise ValueError('Выберите экспорт Авито XLSX размером до 4 МБ.')
                # Reverse export filenames contain the account, but this is only a
                # convenience check; publication is separately gated by provider ownership.
                if not upload.name.startswith(connection.external_id + '_'):
                    raise ValueError('Имя экспорта должно начинаться с ID выбранного аккаунта и подчёркивания.')
                imported = parse_export(upload.read(MAX_BYTES + 1))
            with transaction.atomic():
                connection = ChannelConnection.objects.select_for_update().get(pk=connection.pk)
                connection_for(request, connection_id)
                feed = AvitoServicesFeed.objects.select_for_update().filter(connection=connection).first()
                if feed and (not bound(feed, connection) or str(feed.revision) != request.POST.get('revision')):
                    raise ValueError('Данные изменились. Обновите страницу перед сохранением.')
                if action == 'import':
                    if feed:
                        old = {r['AvitoId']: r['Id'] for r in entries(feed.draft)}
                        old.update({r['AvitoId']: r['Id'] for r in entries(feed.published)})
                        if any(r['AvitoId'] in old and old[r['AvitoId']] != r['Id'] for r in entries(imported)):
                            raise ValueError('Нельзя менять Id уже импортированного AvitoId.')
                        feed.draft = imported
                    else:
                        feed = AvitoServicesFeed(connection=connection, account_id=connection.external_id,
                            organization_id_at_import=connection.channel.organization_id, draft=imported)
                elif action == 'disable' and feed:
                    feed.enabled = False
                else:
                    raise ValueError('Неизвестное действие.')
                feed.revision += 1
                feed.updated_by = request.user
                feed.save()
            return redirect('avito_services_feed', connection_id=connection_id)
        except ValueError as exc:
            error = str(exc)
    return render(request, 'pool_service/avito/services_feed.html', {
        'connection': connection, 'feed': feed, 'rows': entries(feed.draft) if feed else [], 'error': error,
        'feed_url': request.build_absolute_uri(reverse('avito_services_feed_public', args=[feed.public_key])) if feed and feed.enabled else '',
    })


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def services_feed_edit(request, connection_id, avito_id):
    connection = connection_for(request, connection_id)
    feed = get_object_or_404(AvitoServicesFeed, connection=connection)
    if not bound(feed, connection):
        raise Http404
    if request.method == 'POST' and request.POST.get('revision') != str(feed.revision):
        return HttpResponse('Данные изменились. Откройте объявление заново перед редактированием.', status=409)
    row = next((r for r in entries(feed.draft) if r['AvitoId'] == avito_id), None)
    if row is None:
        raise Http404
    form = ContentForm(request.POST or None, initial={'title': row['Title'], 'description': row['Description'],
                                                    'image_urls': row['ImageUrls'].replace(' | ', '\n')})
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            locked = AvitoServicesFeed.objects.select_for_update().get(pk=feed.pk)
            connection = connection_for(request, connection_id)
            if not bound(locked, connection) or request.POST.get('revision') != str(locked.revision):
                return HttpResponse('Данные изменились. Откройте объявление заново перед редактированием.', status=409)
            else:
                updated = deepcopy(locked.draft)
                target = next(r for r in entries(updated) if r['AvitoId'] == avito_id)
                target.update(Title=form.cleaned_data['title'], Description=form.cleaned_data['description'],
                              ImageUrls=form.cleaned_data['image_urls'])
                locked.draft = updated
                locked.revision += 1
                locked.updated_by = request.user
                locked.save()
                return redirect('avito_services_feed', connection_id=connection_id)
    return render(request, 'pool_service/avito/services_edit.html', {'form': form, 'feed': feed, 'connection': connection, 'row': row})


@login_required
@never_cache
@require_GET
def services_feed_download(request, connection_id):
    connection = connection_for(request, connection_id)
    feed = get_object_or_404(AvitoServicesFeed, connection=connection)
    if not bound(feed, connection):
        raise Http404
    response = HttpResponse(workbook_bytes(feed.draft), content_type=MIME)
    response['Content-Disposition'] = 'attachment; filename="avito-services-draft.xlsx"'
    return response


def verify_inventory(connection, sheets):
    """Fail closed unless this file covers exactly the account's active inventory."""
    from .communication_avito import access_token, authorized_account_id, AvitoError
    from .avito_workspace import _get, fetch_item_detail
    account_id = authorized_account_id(connection)
    if str(account_id) != connection.external_id:
        raise AvitoError('provider_account_mismatch')
    token = access_token(connection)
    active = set()
    for page in range(1, 4):
        response = _get(token, f'/core/v1/items?per_page=100&page={page}&status=active')
        resources = response.get('resources')
        if not isinstance(resources, list) or len(resources) > 100:
            raise AvitoError('provider_data_invalid')
        for item in resources:
            if not isinstance(item, dict) or not isinstance(item.get('id'), int):
                raise AvitoError('provider_data_invalid')
            active.add(str(item['id']))
        if len(resources) < 100:
            break
    else:
        raise ValueError('Слишком много активных объявлений для проверки этого файла.')
    rows = entries(sheets)
    if active != {row['AvitoId'] for row in rows}:
        raise ValueError('Набор объявлений в файле отличается от активных в аккаунте. Нужна свежая полная выгрузка.')
    for row in rows:
        detail = fetch_item_detail(token, account_id, row['AvitoId'])
        if detail['status'] != 'active' or detail.get('autoload_item_id') not in {'', row['Id']}:
            raise ValueError('Статус или привязка Id объявления изменились. Обновите экспорт.')


@login_required
@never_cache
@require_http_methods(['POST'])
def services_feed_publish(request, connection_id):
    from .communication_avito import AvitoError
    connection = connection_for(request, connection_id)
    feed = get_object_or_404(AvitoServicesFeed, connection=connection)
    if not bound(feed, connection) or request.POST.get('revision') != str(feed.revision):
        return HttpResponse('Данные изменились. Обновите страницу.', status=409)
    try:
        xml_bytes(feed.draft)
        verify_inventory(connection, feed.draft)
    except (ValueError, AvitoError) as error:
        if isinstance(error, ValueError):
            message = str(error)
        else:
            code = str(error)
            safe_code = code if re.fullmatch(r'provider_[a-z0-9_]{1,80}', code) else 'provider_check_failed'
            message = f'Не удалось проверить объявления через API Авито ({safe_code}). Источник не изменён.'
        return render(request, 'pool_service/avito/services_feed.html', {
            'connection': connection, 'feed': feed, 'rows': entries(feed.draft), 'error': message,
        }, status=400)
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().get(pk=connection.pk)
        connection_for(request, connection_id)
        locked = AvitoServicesFeed.objects.select_for_update().get(pk=feed.pk)
        if not bound(locked, connection) or locked.revision != feed.revision:
            return HttpResponse('Данные изменились. Обновите страницу.', status=409)
        locked.published = deepcopy(locked.draft)
        locked.published_revision = locked.revision
        locked.enabled = True
        locked.updated_by = request.user
        locked.save()
    return redirect('avito_services_feed', connection_id=connection_id)


@never_cache
@require_GET
def services_feed_public(request, public_key, output_format="xml"):
    feed = get_object_or_404(AvitoServicesFeed.objects.select_related('connection__channel'), public_key=public_key, enabled=True)
    connection = feed.connection
    if not feed.published or not bound(feed, connection) or not connection.is_active or not connection.channel.is_active or connection.channel.kind != 'avito':
        raise Http404
    try:
        content = workbook_bytes(feed.published) if output_format == 'xlsx' else xml_bytes(feed.published)
    except ValueError:
        return HttpResponse('Источник временно недоступен: требуется проверка данных.', status=503)
    response = HttpResponse(content, content_type=MIME if output_format == 'xlsx' else 'application/xml; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="avito-services.{output_format}"'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response
