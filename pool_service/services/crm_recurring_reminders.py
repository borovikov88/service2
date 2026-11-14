"""Existing Friday missed-visit control, projected from dated contract terms.

No new scheduler, audience, payment inference or historical replay. Delivery
receipts prevent repeats, including push-only preferences. Uncertain push outcomes
are recorded, not blindly retried; durable in-app delivery is independent.
"""
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from pool_service.models import Pool, Organization, OrganizationAccess, Profile, WaterReading, ServiceVisitPlan
from pool_service.crm_recurring_models import CrmRecurringReminder, CrmRecurringConfirmation
from .crm_recurring_effective import confirmations, terms_as_of, due_dates
from .notifications import SERVICE_NOTIFICATION_ROLES, notify_users
from .push_notifications import send_push_to_users


def _records(pool, start, end, current):
    if not current:return confirmations(pool,start,end)
    qs=CrmRecurringConfirmation.objects.select_for_update().filter(pool=pool).select_related('revision__draft')
    before=qs.filter(effective_from__lte=start).order_by('-effective_from','-pk').first()
    rows=list(qs.filter(effective_from__gt=start,effective_from__lte=end).order_by('effective_from','pk'))
    if before:rows.insert(0,before)
    if not rows:
        future=qs.order_by('effective_from','pk').first()
        if future:rows=[future]
    return rows


def candidates(pool, today, bounds, *, current=False):
    # The previous six days cover the first Friday after a period/term boundary.
    # Longer coverage is the existing reporting period, not an arbitrary backfill.
    start=min(today.replace(month=1,day=1),today-timedelta(days=6))
    records=_records(pool,start,today,current)
    if not records or records[0].effective_from>today:return None
    if pool.service_suspended or pool.is_deleted:return []
    members=OrganizationAccess.objects.filter(organization_id=pool.organization_id)
    if current:members=members.select_for_update()
    users=list(members.values_list('user_id',flat=True))
    reading_qs=WaterReading.objects.filter(pool=pool,added_by_id__in=users,is_deleted=False,
        date__date__gte=start,date__date__lte=today,date__lte=timezone.now()).order_by('date','pk')
    if current:reading_qs=reading_qs.select_for_update()
    readings=list(reading_qs)
    from django.db.models import Q
    plan_qs=ServiceVisitPlan.objects.filter(pool=pool).filter(Q(week_start__gte=start-timedelta(days=6),week_start__lte=today)|Q(planned_date__gte=start,planned_date__lte=today))
    if current:plan_qs=plan_qs.select_for_update()
    plans={p.week_start:p for p in plan_qs}
    slots={}
    for day in due_dates(pool,start,today,records):
        week=day-timedelta(days=day.weekday())
        slots[week]=(day,terms_as_of(pool,day,records),records)
    for week,plan in plans.items():
        if week in slots:continue
        original_records=_records(pool,week,week+timedelta(days=6),current)
        original=due_dates(pool,week,week+timedelta(days=6),original_records)
        day=original[0] if len(original)==1 else plan.planned_date
        state=terms_as_of(pool,day,original_records if original else records)
        if state.get('revision') and state.get('frequency'):slots[week]=(day,state,original_records if original else records)
    items=[];used=set()
    for week,(day,state,source_records) in sorted(slots.items()):
        period_start,_,legacy_key=bounds(today,state['frequency'])
        planned=plans[week].planned_date if week in plans else day
        if planned>today or planned<min(period_start,today-timedelta(days=6)):continue
        # Resolve actual reading dates against the full current window, including
        # the immutable source revision for an older manually moved obligation.
        applicable=sorted({r.pk:r for r in source_records+records}.values(),key=lambda r:(r.effective_from,r.pk))
        available=[]
        for reading in readings:
            actual=timezone.localtime(reading.date).date()
            same_period=(actual-timedelta(days=actual.weekday())==planned-timedelta(days=planned.weekday())) if state['frequency'] in ('weekly','twice_monthly') else (actual.year,actual.month)==(planned.year,planned.month)
            if reading.pk not in used and same_period and terms_as_of(pool,actual,applicable).get('revision')==state['revision']:
                available.append((abs((actual-planned).days),actual,reading.pk))
        if available:
            used.add(min(available)[2]);continue
        shifted=terms_as_of(pool,planned,applicable).get('revision')!=state['revision']
        items.append(dict(key='visit:'+week.isoformat(),revision=state['revision'],due=planned,
            title='Проверьте перенос визита' if shifted else 'Нет сервисного посещения',text=('Перенос между договорными периодами требует сверки: ' if shifted else 'Нет подтверждённого посещения по плану на ')+planned.strftime('%d.%m.%Y'),legacy_key=legacy_key))
    current=terms_as_of(pool,today,records)
    if current['state']=='confirmed' and not current['frequency']:
        items.append(dict(key='schedule:'+today.strftime('%Y-%m'),revision=current['revision'],due=today.replace(day=1),
            title='Не задан график обслуживания',text='В договорном периоде не определён график визитов. Нужна проверка условий; пропуск посещения не установлен.',legacy_key=None))
    return items


def _eligible(user, org, channel):
    from django.contrib.auth import get_user_model
    user=get_user_model().objects.select_for_update().get(pk=user.pk)
    if not user.is_active or not OrganizationAccess.objects.select_for_update().filter(user=user,organization=org,role__in=SERVICE_NOTIFICATION_ROLES).first():return False
    profile=Profile.objects.select_for_update().filter(user=user).first()
    return bool(getattr(org,'notify_missed_visits'+('_push' if channel=='push' else '')) and
                (profile is None or getattr(profile,'push_notifications_enabled' if channel=='push' else 'in_app_notifications_enabled')))


def _deliver_push(receipt_id, today, bounds):
    seed=CrmRecurringReminder.objects.get(pk=receipt_id)
    with transaction.atomic():
        org=Organization.objects.select_for_update().get(pk=seed.organization_id)
        pool=Pool.objects.select_for_update().get(pk=seed.pool_id)
        receipt=CrmRecurringReminder.objects.select_for_update().select_related('user').get(pk=receipt_id)
        if receipt.push_attempted_at:return
        remaining=candidates(pool,today,bounds,current=True) or []
        item=next((i for i in remaining if i['key']==receipt.occurrence_key),None)
        if not _eligible(receipt.user,org,'push') or not item:
            # No transport attempt: keep resumable if a future plan or preference
            # becomes eligible again. Completed visits are not candidates.
            return
        receipt.attempted_revision_id=item['revision'];receipt.attempted_due_date=item['due']
        receipt.push_attempted_at=timezone.now();receipt.push_result='attempting'
        receipt.save(update_fields=['push_result','push_attempted_at','attempted_revision','attempted_due_date'])
        user=receipt.user
    # Mark before crossing the transport boundary: a crash/timeout cannot cause
    # the scheduled command to resend an uncertain attempt on the next tick.
    try:
        sent=send_push_to_users([user],title=item['title'],message=item['text'],action_url=reverse('pool_detail',kwargs={'pool_uuid':pool.uuid}))
        result='sent' if sent else 'no_delivery_confirmed'
    except Exception:
        result='uncertain'
    CrmRecurringReminder.objects.filter(pk=receipt_id).update(push_result=result)


def _pool_org_id(pool_id):
    return Pool.objects.values_list('organization_id',flat=True).get(pk=pool_id)


def generate_for_pool(pool_id, today, bounds):
    org_id=_pool_org_id(pool_id)
    with transaction.atomic():
        return _generate_locked(pool_id,org_id,today,bounds)


def _generate_locked(pool_id, org_id, today, bounds):
    org=Organization.objects.select_for_update().get(pk=org_id)
    pool=Pool.objects.select_for_update().select_related('client').get(pk=pool_id,organization=org)
    items=candidates(pool,today,bounds,current=True)
    if items is None:return False  # preserve the existing legacy branch
    from django.contrib.auth import get_user_model
    from pool_service.models import Notification
    users=get_user_model().objects.select_for_update().filter(is_active=True,organizationaccess__organization=org,
        organizationaccess__role__in=SERVICE_NOTIFICATION_ROLES).distinct()
    for user in users:
        in_app=_eligible(user,org,'in_app');push=_eligible(user,org,'push')
        if not (in_app or push):continue
        for item in items:
            receipt,created=CrmRecurringReminder.objects.get_or_create(pool=pool,user=user,occurrence_key=item['key'],defaults=dict(
                organization=org,revision_id=item['revision'],due_date=item['due']))
            if not created:
                if push and receipt.push_result=='pending' and receipt.push_attempted_at is None:
                    transaction.on_commit(lambda pk=receipt.pk: _deliver_push(pk,today,bounds))
                continue
            # A legacy broad-period notice already covers this reporting period.
            if item['legacy_key'] and Notification.objects.filter(user=user,pool=pool,dedupe_key=f"missed_visit:{pool.pk}:{item['legacy_key']}").exists():
                receipt.push_result='covered_legacy';receipt.push_attempted_at=timezone.now();receipt.save(update_fields=['push_result','push_attempted_at']);continue
            label=pool.client.name if pool.client else pool.address
            if in_app:
                notify_users([user],title=item['title'],message=f"{label}: {item['text']}",kind='missed_visit',level='warning',
                    organization=org,pool=pool,action_url=reverse('pool_detail',kwargs={'pool_uuid':pool.uuid}),
                    dedupe_key=f"recurring:{pool.pk}:{item['key']}",send_in_app=True,send_push=False)
            if push:transaction.on_commit(lambda pk=receipt.pk: _deliver_push(pk,today,bounds))
            else:
                receipt.push_result='disabled';receipt.save(update_fields=['push_result'])
    return True
