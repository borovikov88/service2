"""Explicit conversion contacts under the lead command's organization mutex."""
import re
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db.models import Q, F, Value, Case, When, CharField
from django.db.models.functions import Trim, Length, Substr, Concat
from pool_service.contact_directory import _phone_expression
from pool_service.models import Client, OrganizationAccess
from pool_service.client_crm_models import ClientBusinessRole, ClientContact, ClientCompanyLink
from pool_service.client_queries import active_clients, readable_clients
from pool_service.phone_utils import normalize_phone, canonical_phone_value


def normalize_contact(value):
    if type(value) is not dict or value.get('type') not in {'private','legal','both'} or not set(value)<= {'type','person_name','company_name','phone','inn'}:
        raise ValidationError('Выберите физлицо, компанию или оба контакта.')
    if any(not isinstance(item,str) for item in value.values()): raise ValidationError('Реквизиты должны быть текстом.')
    result={key:str(value.get(key,'')).strip() for key in ('type','person_name','company_name','phone','inn')}
    if any(len(result[k])>n for k,n in [('person_name',255),('company_name',255),('phone',20),('inn',20)]): raise ValidationError('Слишком длинные реквизиты.')
    if result['type'] in {'private','both'} and not result['person_name']: raise ValidationError('Укажите имя физлица.')
    if result['type'] in {'legal','both'} and not result['company_name']: raise ValidationError('Укажите название компании.')
    if result['phone'] and (not re.fullmatch(r'[+0-9 ().\-\s]+',result['phone']) or not normalize_phone(result['phone'])): raise ValidationError('Проверьте телефон.')
    if result['inn'] and (not result['inn'].isdigit() or len(result['inn']) not in {10,12}): raise ValidationError('Проверьте ИНН.')
    return result


def normalize_customer(value):
    """Independent person/company selection; old new_contact remains supported."""
    if type(value) is not dict or not set(value) <= {'contact', 'company'}:
        raise ValidationError('Выберите контакт и, при необходимости, компанию.')
    result = {}
    for kind in ('contact', 'company'):
        item = value.get(kind)
        if item is None:
            result[kind] = None
            continue
        if type(item) is not dict:
            raise ValidationError('Проверьте контакт и компанию.')
        if 'id' in item:
            if set(item) != {'id'} or type(item['id']) is not int or item['id'] <= 0:
                raise ValidationError('Выберите контакт из результатов поиска.')
            result[kind] = {'id': item['id']}
            continue
        allowed = {'name', 'phone', 'email', 'category'} if kind == 'contact' else {'name', 'inn', 'phone', 'email'}
        if not set(item) <= allowed or any(type(part) is not str for part in item.values()):
            raise ValidationError('Проверьте данные нового контакта или компании.')
        fresh = {key: item.get(key, '').strip() for key in sorted(allowed)}
        if not fresh['name'] or len(fresh['name']) > 255:
            raise ValidationError('Укажите имя контакта или название компании до 255 символов.')
        phone = fresh['phone']
        if phone and (len(phone) > 40 or not re.fullmatch(r'[+0-9 ().\-\s]+', phone) or not normalize_phone(phone)):
            raise ValidationError('Проверьте телефон.')
        if len(canonical_phone_value(phone)) > Client._meta.get_field('phone').max_length:
            raise ValidationError('Сократите запись телефона до 20 символов, убрав лишние пробелы и скобки.')
        if len(fresh['email']) > 254:
            raise ValidationError('Проверьте email.')
        if fresh['email']:
            try:
                validate_email(fresh['email'])
            except ValidationError as exc:
                raise ValidationError('Проверьте email.') from exc
        if kind == 'contact':
            if fresh['category'] and fresh['category'] not in dict(ClientBusinessRole.ROLE_CHOICES):
                raise ValidationError('Выберите доступную категорию контакта.')
        elif fresh['inn'] and (not fresh['inn'].isascii() or not fresh['inn'].isdigit() or len(fresh['inn']) not in {10, 12}):
            raise ValidationError('Проверьте ИНН.')
        result[kind] = fresh
    if not any(result.values()):
        raise ValidationError('Выберите или создайте контакт либо компанию.')
    return result


def _authorize(using, actor, organization):
    if actor.is_superuser or not OrganizationAccess.objects.using(using).select_for_update().filter(
        organization=organization,user=actor,role__in=('owner','admin','manager','service','installer')).exists(): raise PermissionDenied


def _create_identity(using, actor, organization, kind, value):
    if value.get('category') and not OrganizationAccess.objects.using(using).select_for_update().filter(
            organization=organization, user=actor, role__in=('owner', 'admin')).exists():
        raise PermissionDenied('Contact classification requires owner or administrator access.')
    if len(canonical_phone_value(value['phone'])) > Client._meta.get_field('phone').max_length:
        raise ValidationError('Сократите запись телефона до 20 символов, убрав лишние пробелы и скобки.')
    existing=active_clients(Client.objects.using(using).select_for_update()).filter(organization=organization)
    key=normalize_phone(value['phone']) if value['phone'] else ''
    if key:
        primary=_phone_matches(existing, 'phone', key)
        extra=_phone_matches(ClientContact.objects.using(using).filter(client_id__in=existing.values('pk'),kind='phone'), 'value', key)
        if primary.exists() or extra.exists():
            raise ValidationError('Найден возможный существующий контакт. Выберите его явно; автоматическое объединение не выполняется.')
    if existing.filter(client_type=kind, name__iexact=value['name']).exists():
        raise ValidationError('Найден одноимённый контакт. Проверьте справочник и выберите существующий.')
    if value.get('inn') and existing.filter(inn=value['inn']).exists():
        raise ValidationError('Контакт с таким ИНН уже существует. Выберите его явно.')
    if value.get('email') and (existing.filter(email__iexact=value['email']).exists() or
            ClientContact.objects.using(using).filter(client_id__in=existing.values('pk'), kind='email', value__iexact=value['email']).exists()):
        raise ValidationError('Найден контакт с таким email. Выберите его из справочника.')
    contact = Client.objects.using(using).create(organization=organization, client_type=kind,
        name=value['name'], company_name=value['name'] if kind == 'legal' else '',
        inn=value.get('inn') or None, email=value.get('email') or '')
    if key:
        contact.phone=canonical_phone_value(value['phone']);contact.save(using=using, update_fields=['phone'])
        ClientContact.objects.using(using).create(client=contact,kind='phone',value=contact.phone,match_value=key,is_primary=True,phone_type='other',source_reference='crm-explicit-conversion')
    if value.get('email'):
        ClientContact.objects.using(using).create(client=contact, kind='email', value=value['email'],
            match_value=value['email'].casefold(), is_primary=True, sources=['manual'], source_reference='crm-explicit-conversion')
    if value.get('category'):
        from .contact_roles import _apply
        _apply(contact, actor, True, role_code=value['category'], using=using)
    return contact


def resolve_conversion_customer(using, actor, organization, value):
    """Return (primary client, selected person), within the lead transaction."""
    _authorize(using, actor, organization)
    selected = {}
    for key, kind in (('contact', 'private'), ('company', 'legal')):
        item = value.get(key)
        if not item:
            selected[key] = None
        elif 'id' in item:
            selected[key] = readable_clients(actor).using(using).select_for_update().filter(
                pk=item['id'], organization=organization, client_type=kind).first()
            if selected[key] is None:
                raise PermissionDenied('Selected contact is unavailable in this organization.')
        else:
            selected[key] = _create_identity(using, actor, organization, kind, item)
    person, company = selected['contact'], selected['company']
    if not (person or company):
        raise ValidationError('Выберите контакт или компанию.')
    if person and company:
        ClientCompanyLink.objects.using(using).get_or_create(person=person, company=company,
            defaults={'automatic': False, 'source': 'manual'})
    return company or person, person


def create_conversion_contact(using, actor, organization, value, *, return_pair=False):
    # Keep the legacy command payload/hash and return contract stable.
    person = {'name': value['person_name'], 'phone': value['phone'], 'email': '', 'category': ''} if value['type'] in {'private', 'both'} else None
    company = {'name': value['company_name'], 'phone': value['phone'] if person is None else '',
        'email': '', 'inn': value['inn']} if value['type'] in {'legal', 'both'} else None
    client, contact = resolve_conversion_customer(using, actor, organization, {'contact': person, 'company': company})
    return (client, contact) if return_pair else client


def _phone_matches(queryset, field, key):
    """Bounded SQL existence check for canonical and historical formatted phones."""
    queryset=queryset.annotate(_raw=Trim(F(field)),_digits=_phone_expression(field)).annotate(_length=Length('_digits'))
    return queryset.annotate(_key=Case(
        When(_raw__startswith='+',_length=11,_digits__startswith='7',then=Substr('_digits',2)),
        When(_raw__startswith='+',then=Concat(Value('+'),F('_digits'))),
        When(Q(_length=11)&(Q(_digits__startswith='7')|Q(_digits__startswith='8')),then=Substr('_digits',2)),
        When(_length=10,then=F('_digits')),
        default=Concat(Value('+'),F('_digits')),output_field=CharField())).filter(_key=key)
