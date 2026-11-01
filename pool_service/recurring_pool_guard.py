"""Central ORM guard; no raw-SQL permission is implied."""
from django.core.exceptions import ValidationError
from django.db import models, transaction

FIELDS = frozenset({'service_monthly_price','service_frequency','service_interval_days'})
SCOPE = frozenset({'organization','organization_id','client','client_id'})


def protected(pool_id, using='default', lock=True):
    from .crm_recurring_models import CrmRecurringConfirmation
    if not lock:
        return CrmRecurringConfirmation.objects.using(using).filter(pool_id=pool_id).exists()
    with transaction.atomic(using=using):
        return CrmRecurringConfirmation.objects.using(using).select_for_update().filter(pool_id=pool_id).first() is not None


def check_change(pool, old, fields, *, lock=True):
    names = {'organization_id' if n=='organization' else 'client_id' if n=='client' else n for n in fields}
    changed = [n for n in names & (FIELDS | {'organization_id','client_id'}) if getattr(pool,n)!=old[n]]
    if changed and protected(pool.pk, pool._state.db or 'default', lock=lock):
        raise ValidationError('Цена и периодичность подтверждённого договора меняются через датированную редакцию. Перенос клиента/организации требует сверки.')


class PoolQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if not (set(kwargs) & (FIELDS | SCOPE)):
            return super().update(**kwargs)
        with transaction.atomic(using=self.db):
            ids=list(self.order_by('pk').select_for_update().values_list('pk',flat=True))
            from .crm_recurring_models import CrmRecurringConfirmation
            protected_ids=set(CrmRecurringConfirmation.objects.using(self.db).select_for_update().filter(pool_id__in=ids).values_list('pool_id',flat=True))
            for row in self.model.objects.using(self.db).filter(pk__in=protected_ids).values(*FIELDS,'organization_id','client_id'):
                for name,value in kwargs.items():
                    field='organization_id' if name=='organization' else 'client_id' if name=='client' else name
                    if field in FIELDS | {'organization_id','client_id'}:
                        value=getattr(value,'pk',value)
                        if hasattr(value,'resolve_expression') or value!=row[field]:
                            raise ValidationError('Массовое изменение договорных полей запрещено для подтверждённых объектов.')
            return super(PoolQuerySet, self.filter(pk__in=ids)).update(**kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        # Django bulk_update routes through guarded update in this QuerySet.
        with transaction.atomic(using=self.db):
            return super().bulk_update(objs, fields, **kwargs)

    def bulk_create(self, objs, **kwargs):
        if kwargs.get('update_conflicts') and set(kwargs.get('update_fields') or ()) & (FIELDS | SCOPE):
            raise ValidationError('Обновление договорных полей через upsert недоступно.')
        return super().bulk_create(objs, **kwargs)


def protect_form_fields(form):
    """Keep legacy stored values unchanged; the separate summary shows dated terms."""
    if form.instance.pk and protected(form.instance.pk, lock=False):
        for name in FIELDS:
            if name in form.fields:
                form.initial[name] = getattr(form.instance, name)
                form.fields[name].disabled = True
                form.fields[name].help_text = 'Сохранённое прежнее значение. Текущие условия — в договоре; изменение только через новую редакцию.'
