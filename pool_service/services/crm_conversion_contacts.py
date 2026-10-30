"""Explicit conversion contacts under the lead command's organization mutex."""
import re
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, F, Value, Case, When, CharField
from django.db.models.functions import Trim, Length, Substr, Concat
from pool_service.contact_directory import _phone_expression
from pool_service.models import Client, OrganizationAccess
from pool_service.client_crm_models import ClientContact, ClientCompanyLink
from pool_service.client_queries import active_clients
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


def create_conversion_contact(using, actor, organization, value):
    if actor.is_superuser or not OrganizationAccess.objects.using(using).select_for_update().filter(
        organization=organization,user=actor,role__in=('owner','admin','manager','service','installer')).exists(): raise PermissionDenied
    existing=active_clients(Client.objects.using(using).select_for_update()).filter(organization=organization)
    key=normalize_phone(value['phone']) if value['phone'] else ''
    if key:
        primary=_phone_matches(existing, 'phone', key)
        extra=_phone_matches(ClientContact.objects.using(using).filter(client_id__in=existing.values('pk'),kind='phone'), 'value', key)
        if primary.exists() or extra.exists():
            raise ValidationError('Найден возможный существующий контакт. Выберите его явно; автоматическое объединение не выполняется.')
    for kind, name in (('private',value['person_name']),('legal',value['company_name'])):
        if name and existing.filter(client_type=kind,name__iexact=name).exists(): raise ValidationError('Найден одноимённый контакт. Проверьте справочник и выберите существующий.')
    if value['inn'] and existing.filter(inn=value['inn']).exists(): raise ValidationError('Контакт с таким ИНН уже существует. Выберите его явно.')
    person=company=None
    if value['type'] in {'private','both'}:
        person=Client.objects.using(using).create(organization=organization,client_type='private',name=value['person_name'])
    if value['type'] in {'legal','both'}:
        company=Client.objects.using(using).create(organization=organization,client_type='legal',name=value['company_name'],company_name=value['company_name'],inn=value['inn'] or None)
    contact=person or company
    if key:
        contact.phone=canonical_phone_value(value['phone']);contact.save(update_fields=['phone'])
        ClientContact.objects.using(using).create(client=contact,kind='phone',value=contact.phone,match_value=key,is_primary=True,phone_type='other',source_reference='crm-explicit-conversion')
    if person and company: ClientCompanyLink.objects.using(using).create(person=person,company=company,automatic=False,source='manual')
    return company or person


def _phone_matches(queryset, field, key):
    """Bounded SQL existence check for canonical and historical formatted phones."""
    queryset=queryset.annotate(_raw=Trim(F(field)),_digits=_phone_expression(field)).annotate(_length=Length('_digits'))
    return queryset.annotate(_key=Case(
        When(_raw__startswith='+',_length=11,_digits__startswith='7',then=Substr('_digits',2)),
        When(_raw__startswith='+',then=Concat(Value('+'),F('_digits'))),
        When(Q(_length=11)&(Q(_digits__startswith='7')|Q(_digits__startswith='8')),then=Substr('_digits',2)),
        When(_length=10,then=F('_digits')),
        default=Concat(Value('+'),F('_digits')),output_field=CharField())).filter(_key=key)
