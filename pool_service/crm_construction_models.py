"""Personal launch authority and immutable evidence; no default grants."""
from django.conf import settings
from django.db import models
from django.utils import timezone
from .crm_commercial_models import LeadEvidenceModel


class CrmConstructionAuthority(models.Model):
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'user'], name='crm_construction_authority_uq')]


class CrmConstructionLaunch(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'deal_id', 'project_id', 'quote_id', 'actor_id',
        'recipient_id', 'basis', 'evidence', 'operation_key', 'payload_hash', 'created_at')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    deal = models.OneToOneField('CrmDeal', on_delete=models.PROTECT)
    project = models.OneToOneField('CrmItem', on_delete=models.PROTECT)
    quote = models.ForeignKey('CrmQuote', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    basis = models.JSONField()
    evidence = models.CharField(max_length=1000)
    operation_key = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['organization', 'operation_key'], name='crm_construction_operation_uq')]
