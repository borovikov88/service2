"""Operational custody evidence, never a warehouse or a financial ledger."""
from contextlib import contextmanager
from contextvars import ContextVar
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .crm_commercial_models import CommercialModel, LeadEvidenceModel


_writer=ContextVar('crm_material_writer',default=False)
@contextmanager
def _write_scope():
    token=_writer.set(True)
    try:yield
    finally:_writer.reset(token)


class CrmMaterialAccess(CommercialModel):
    immutable_scope=('organization_id','user_id')
    organization=models.ForeignKey('Organization',on_delete=models.PROTECT)
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    record_own=models.BooleanField(default=False)
    review_team=models.BooleanField(default=False)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['organization','user'],name='crm_material_access_once')]


class CrmMaterialIssue(LeadEvidenceModel):
    organization=models.ForeignKey('Organization',on_delete=models.PROTECT)
    pool=models.ForeignKey('Pool',on_delete=models.PROTECT)
    client=models.ForeignKey('Client',on_delete=models.PROTECT)
    deal=models.ForeignKey('CrmDeal',null=True,blank=True,on_delete=models.PROTECT)
    holder=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(default=timezone.now)
    occurred_on=models.DateField()
    item_name=models.CharField(max_length=240)
    unit=models.CharField(max_length=40)
    packaging=models.CharField(max_length=120)
    quantity=models.DecimalField(max_digits=14,decimal_places=3)
    source=models.CharField(max_length=500)
    operation_key=models.CharField(max_length=64)
    payload_hash=models.CharField(max_length=64)
    revision=models.PositiveIntegerField(default=0)
    immutable_scope=('organization_id','pool_id','client_id','deal_id','holder_id','created_by_id','created_at','occurred_on','item_name','unit','packaging','quantity','source','operation_key','payload_hash')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['organization','created_by','operation_key'],name='crm_material_issue_once'),models.CheckConstraint(condition=models.Q(quantity__gt=0),name='crm_material_issue_positive')]
        indexes=[models.Index(fields=['organization','holder','id'],name='crm_material_holder_idx')]
    def save(self,*args,**kwargs):
        if not _writer.get():raise ValidationError('Используйте защищённый сервис журнала.')
        return super().save(*args,**kwargs)
    def clean(self):
        super().clean()
        if self.pool.organization_id!=self.organization_id or self.pool.client_id!=self.client_id:
            raise ValidationError('Несогласованная область объекта.')
        if self.deal_id and (self.deal.pool_id!=self.pool_id or self.deal.client_id!=self.client_id):
            raise ValidationError('Сделка не соответствует объекту.')


class CrmMaterialMovement(LeadEvidenceModel):
    organization=models.ForeignKey('Organization',on_delete=models.PROTECT)
    issue=models.ForeignKey(CrmMaterialIssue,on_delete=models.PROTECT,related_name='movements')
    kind=models.CharField(max_length=12,choices=[('delivery','Доставлено'),('return','Возвращено в офис'),('correction','Отмена ошибочной записи'),('void','Выдача отменена как ошибочная')])
    quantity=models.DecimalField(max_digits=14,decimal_places=3)
    pool=models.ForeignKey('Pool',on_delete=models.PROTECT)
    client=models.ForeignKey('Client',on_delete=models.PROTECT)
    deal=models.ForeignKey('CrmDeal',null=True,blank=True,on_delete=models.PROTECT)
    occurred_on=models.DateField()
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)
    source=models.CharField(max_length=500)
    reason=models.CharField(max_length=500,blank=True)
    reverses=models.OneToOneField('self',null=True,blank=True,on_delete=models.PROTECT,related_name='reversal')
    operation_key=models.CharField(max_length=64)
    payload_hash=models.CharField(max_length=64)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['organization','created_by','operation_key'],name='crm_material_move_once'),models.CheckConstraint(condition=models.Q(quantity__gt=0),name='crm_material_move_positive'),models.CheckConstraint(condition=models.Q(kind='correction',reverses__isnull=False)|models.Q(kind__in=['delivery','return','void'],reverses__isnull=True),name='crm_material_move_kind')]
        indexes=[models.Index(fields=['organization','occurred_on','id'],name='crm_material_month_idx')]
    def save(self,*args,**kwargs):
        if not _writer.get():raise ValidationError('Используйте защищённый сервис журнала.')
        if self.pk:raise ValidationError('История неизменяема; используйте отдельную корректировку.')
        return super().save(*args,**kwargs)
    def clean(self):
        super().clean()
        if self.issue.organization_id!=self.organization_id or self.pool.organization_id!=self.organization_id or self.pool.client_id!=self.client_id:
            raise ValidationError('Несогласованная область движения.')
        if self.deal_id and (self.deal.pool_id!=self.pool_id or self.deal.client_id!=self.client_id):raise ValidationError('Сделка не соответствует объекту.')
        if self.reverses_id and (self.reverses.issue_id!=self.issue_id or self.reverses.kind=='correction' or self.reverses.quantity!=self.quantity):raise ValidationError('Некорректное основание отмены.')
