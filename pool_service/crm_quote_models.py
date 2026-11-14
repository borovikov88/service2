"""Commercial proposal references and append-only decisions, not execution or accounting."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .crm_commercial_models import LeadEvidenceModel


class CrmQuote(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'deal_id', 'version_id', 'variant', 'source_hash', 'scope', 'amount', 'created_by_id')
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    deal = models.ForeignKey('pool_service.CrmDeal', on_delete=models.PROTECT, related_name='quotes')
    version = models.ForeignKey('pool_service.PoolEstimateVersion', on_delete=models.PROTECT)
    variant = models.CharField(max_length=16, choices=[('economy','Эконом'),('standard','Стандарт'),('premium','Премиум')])
    source_hash = models.CharField(max_length=64)
    scope = models.JSONField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=20, default='prepared', choices=[('prepared','Подготовлено'),('sent','Отправлено — ручная отметка'),('sending_unknown','Результат отправки неизвестен'),('accepted','Принято'),('rejected','Отклонено'),('replaced','Заменено')])
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['deal','version','variant'], name='crm_quote_version_variant_uq')]

    def clean(self):
        super().clean()
        if self.version_id and self.version.estimate.organization_id != self.organization_id:
            raise ValidationError('Расчёт принадлежит другой организации.')
        if self.amount is not None and (not self.amount.is_finite() or self.amount < 0):
            raise ValidationError('Некорректная сумма предложения.')


class CrmQuoteEvent(LeadEvidenceModel):
    immutable_scope = ('organization_id','quote_id','command_id','actor_id','action','before','after','evidence','created_at')
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    quote = models.ForeignKey(CrmQuote, on_delete=models.PROTECT, related_name='events')
    command = models.ForeignKey('pool_service.CrmCommand', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=24)
    before = models.CharField(max_length=20, blank=True)
    after = models.CharField(max_length=20)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['quote','command'], name='crm_quote_event_command_uq')]

    def clean(self):
        super().clean()
        if self.quote_id and self.quote.organization_id != self.organization_id:
            raise ValidationError('История относится к другой организации.')
