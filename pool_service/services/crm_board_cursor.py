"""Signed bounded keysets. Callers hold the existing organization mutex.

Epoch covers supported commercial saves/deletes, not every related source. Each
portion is authorized anew; this is a live view, not a retained database snapshot.
"""
import hashlib
import json
from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.utils.dateparse import parse_datetime
from pool_service.crm_board_models import CrmBoardEpoch

PAGE_SIZE = 50
SALT = 'crm-board-keyset-v1'
TRACKED = {'CrmLead', 'CrmDeal', 'CrmStage', 'CrmPipeline', 'CrmAccess'}


class BoardReset(ValidationError):
    def __init__(self):
        super().__init__('Доска изменилась. Обновите список, чтобы продолжить загрузку.')


def bump(organization_id, using='default'):
    manager = CrmBoardEpoch.objects.using(using)
    if not manager.filter(organization_id=organization_id).update(revision=F('revision')+1):
        manager.create(organization_id=organization_id, revision=1)


def epoch(organization_id, using='default'):
    return CrmBoardEpoch.objects.using(using).select_for_update().filter(organization_id=organization_id).values_list('revision',flat=True).first() or 0


def scope_hash(scope):
    return hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def cursor_time(cursor, default):
    """Only signed times may freeze temporal filter membership across portions."""
    if not cursor:
        return default
    try:
        if not isinstance(cursor,str) or len(cursor)>4096: raise BoardReset()
        token=signing.loads(cursor,salt=SALT,max_age=3600)
        instant=parse_datetime(token['as_of'])
        if instant is None or instant.tzinfo is None: raise BoardReset()
        return instant
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise BoardReset()


def portion(queryset, *, scope, column, revision, cursor='', chronological=False, as_of=None):
    context = scope_hash(scope)
    if cursor:
        if not isinstance(cursor,str) or len(cursor)>4096:
            raise BoardReset()
        try:
            token=signing.loads(cursor,salt=SALT,max_age=3600)
            if token['scope']!=context or token['column']!=str(column) or token['epoch']!=revision:
                raise BoardReset()
            pk=token['pk']
            if type(pk) is not int or pk<1: raise BoardReset()
            if chronological:
                stamp=parse_datetime(token['stamp'])
                if stamp is None or stamp.tzinfo is None: raise BoardReset()
                queryset=queryset.filter(Q(received_at__lt=stamp)|Q(received_at=stamp,pk__lt=pk))
            else:
                queryset=queryset.filter(pk__lt=pk)
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            raise BoardReset()
    rows=list(queryset.order_by(*(['-received_at','-pk'] if chronological else ['-pk']))[:PAGE_SIZE+1])
    more=len(rows)>PAGE_SIZE; rows=rows[:PAGE_SIZE]; next_cursor=None
    if more:
        last=rows[-1]; token={'scope':context,'column':str(column),'epoch':revision,'pk':last.pk}
        if chronological:token['stamp']=last.received_at.isoformat()
        if as_of is not None:token['as_of']=as_of.isoformat()
        next_cursor=signing.dumps(token,salt=SALT,compress=True)
    return rows,next_cursor
