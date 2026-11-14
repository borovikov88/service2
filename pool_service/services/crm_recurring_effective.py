"""Date-aware read projection. Stored Pool values remain legacy history, not a cache."""
from datetime import date, timedelta
from decimal import Decimal
from calendar import monthrange
from pool_service.crm_recurring_models import CrmRecurringConfirmation


def attach_terms(pools, start, end):
    from django.db.models import OuterRef, Subquery, F, Q
    pools=list(pools)
    ids=[p.pk for p in pools]
    if not ids:return
    scoped=CrmRecurringConfirmation.objects.filter(pool_id=OuterRef('pool_id'))
    first=scoped.order_by('effective_from','pk').values('pk')[:1]
    before=scoped.filter(effective_from__lte=start).order_by('-effective_from','-pk').values('pk')[:1]
    query=CrmRecurringConfirmation.objects.filter(pool_id__in=ids).annotate(first_pk=Subquery(first),before_pk=Subquery(before)).filter(
        Q(pk=F('first_pk'))|Q(pk=F('before_pk'))|Q(effective_from__gt=start,effective_from__lte=end)).select_related('revision__draft').order_by('effective_from','pk')
    grouped={pk:[] for pk in ids}
    for row in query:grouped[row.pool_id].append(row)
    for pool in pools:pool._recurring_range=(start,end,grouped[pool.pk])


def confirmations(pool, start=None, end=None):
    from django.utils import timezone
    start=start or timezone.localdate();end=end or start
    cached=getattr(pool,'_recurring_range',None)
    if cached is None or cached[0]>start or cached[1]<end:
        attach_terms([pool],start,end);cached=pool._recurring_range
    return cached[2]


def terms_as_of(pool, day, records=None):
    records=confirmations(pool,day,day) if records is None else records
    legacy=dict(confirmed=False,price=pool.service_monthly_price,frequency=pool.service_frequency,
                interval=pool.service_interval_days,label=pool.get_service_frequency_display(),state='legacy')
    if not records:return legacy
    rows=[r for r in records if r.effective_from<=day]
    if not rows:return dict(legacy,confirmed=True,state='future')
    r=rows[-1];p=r.revision.payload;draft=r.revision.draft
    if (r.organization_id!=pool.organization_id or draft.client_id!=pool.client_id
        or draft.object_scope!={'kind':'pool','pool':pool.pk,'client_object':None}):
        return dict(confirmed=True,price=None,frequency=None,interval=None,label='Требует сверки',state='scope_changed')
    month=day.replace(day=1).isoformat()
    row=next((x for x in p['periods'] if x['month']==month),None)
    if day<date.fromisoformat(p['start']) or (p['end'] and day>date.fromisoformat(p['end'])) or row is None:
        return dict(confirmed=True,price=None,frequency=None,interval=None,label='Нет договорного периода',state='outside_period')
    schedule=p['schedule'];frequency=schedule['frequency']
    return dict(confirmed=True,price=None if row['agreed_amount'] is None else Decimal(row['agreed_amount']),
        frequency=frequency,interval=None,label=dict(pool.SERVICE_FREQUENCY_CHOICES).get(frequency,'Не задана'),
        state='confirmed',revision=r.revision_id,first_visit=schedule['first_visit'])


def due_dates(pool, start, end, records=None):
    """Explicit anchor + existing frequency enum, each candidate checked at its date."""
    records=confirmations(pool,start,end) if records is None else records
    month_steps={'monthly':1,'bimonthly':2,'quarterly':3,'twice_yearly':6,'yearly':12}
    result=set()
    for r in records:
        p=r.revision.payload;s=p['schedule'];freq=s['frequency']
        if not freq or not s['first_visit']:continue
        anchor=date.fromisoformat(s['first_visit']);candidate=anchor;step=0
        while candidate<=end:
            if candidate>=start:
                active=terms_as_of(pool,candidate,records)
                if active.get('revision')==r.revision_id:result.add(candidate)
            step+=1
            if freq in ('weekly','twice_monthly'):
                try: candidate=anchor+timedelta(days=(7 if freq=='weekly' else 14)*step)
                except OverflowError: break
            elif freq in month_steps:
                months=anchor.year*12+anchor.month-1+month_steps[freq]*step
                year,month=divmod(months,12);month+=1
                if year>9999: break
                candidate=date(year,month,min(anchor.day,monthrange(year,month)[1]))
            else:break
            if step>12000:break
    return sorted(result)


def projected_pool(pool, day, records):
    from copy import copy
    state=terms_as_of(pool,day,records);projected=copy(pool)
    projected.service_frequency=state['frequency'];projected.service_interval_days=state['interval']
    projected._contract_revision=state.get('revision');projected._contract_records=records
    return projected


def reading_matches_segment(pool, day):
    if not hasattr(pool,'_contract_records'):return True
    return terms_as_of(pool,day,pool._contract_records).get('revision')==pool._contract_revision and pool._contract_revision is not None
