"""Evidence of an explicit service handoff; never another operational task."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .crm_commercial_models import LeadEvidenceModel


class CrmHandoff(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'deal_id', 'quote_id', 'item_id',
                       'recipient_id', 'submitted_by_id', 'basis', 'created_at')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    deal = models.OneToOneField('CrmDeal', on_delete=models.PROTECT)
    quote = models.ForeignKey('CrmQuote', on_delete=models.PROTECT)
    item = models.OneToOneField('CrmItem', on_delete=models.PROTECT)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    basis = models.JSONField()
    status = models.CharField(max_length=16, default='pending', choices=[
        ('pending', 'Ожидает приёмки'), ('accepted', 'Пакет принят'), ('declined', 'Приёмка отклонена')])
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'

    def clean(self):
        super().clean()
        if self.quote_id and (self.quote.deal_id != self.deal_id or self.quote.organization_id != self.organization_id):
            raise ValidationError('КП не относится к этой сделке.')
        if self.item_id and self.item.organization_id != self.organization_id:
            raise ValidationError('Заявка другой организации.')


class CrmHandoffTask(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'handoff_id', 'task_id', 'snapshot')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    handoff = models.ForeignKey(CrmHandoff, on_delete=models.PROTECT, related_name='task_links')
    task = models.OneToOneField('ServiceTask', on_delete=models.PROTECT)
    snapshot = models.JSONField()

    class Meta:
        base_manager_name = 'objects'

    def clean(self):
        super().clean()
        if self.handoff_id and self.handoff.organization_id != self.organization_id:
            raise ValidationError('Передача другой организации.')
        if self.task_id and (self.task.organization_id != self.organization_id or self.task.crm_item_id != self.handoff.item_id):
            raise ValidationError('Задача не принадлежит операционной заявке.')


class CrmHandoffEvent(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'handoff_id', 'actor_id', 'operation_key',
                       'payload_hash', 'action', 'evidence', 'created_at')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    handoff = models.ForeignKey(CrmHandoff, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    operation_key = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    action = models.CharField(max_length=16)
    evidence = models.CharField(max_length=1000)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['organization', 'operation_key'], name='crm_handoff_operation_uq')]

    def clean(self):
        super().clean()
        if self.handoff_id and self.handoff.organization_id != self.organization_id:
            raise ValidationError('История другой организации.')
