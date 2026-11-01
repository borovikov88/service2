"""Draft-only regular service contract lineage; no operational activation."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .crm_commercial_models import LeadEvidenceModel


class CrmRecurringDraft(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'deal_id', 'client_id', 'payer_id', 'object_scope', 'contract_basis', 'created_by_id', 'created_at')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    deal = models.OneToOneField('CrmDeal', on_delete=models.PROTECT)
    client = models.ForeignKey('Client', on_delete=models.PROTECT, related_name='+')
    payer = models.ForeignKey('Client', on_delete=models.PROTECT, related_name='+')
    object_scope = models.JSONField()
    contract_basis = models.CharField(max_length=1000)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


    def clean(self):
        super().clean()
        from pool_service.models import Client
        if not Client.objects.filter(pk=self.payer_id, organization_id=self.organization_id).exists():
            raise ValidationError('Payer must belong to the contract organization.')


class CrmRecurringRevision(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'draft_id', 'revision', 'kind', 'payload', 'operation_key', 'payload_hash', 'actor_id', 'reason', 'created_at')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    draft = models.ForeignKey(CrmRecurringDraft, on_delete=models.PROTECT, related_name='versions')
    revision = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=[('prepare','Prepare'),('revise','Revise'),('renew','Renew')])
    payload = models.JSONField()
    operation_key = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['draft','revision'],name='crm_recurring_revision_uq'),
                       models.UniqueConstraint(fields=['organization','operation_key'],name='crm_recurring_operation_uq')]

    def clean(self):
        super().clean()
        if not CrmRecurringDraft.objects.filter(pk=self.draft_id, organization_id=self.organization_id).exists():
            raise ValidationError('Draft must belong to the revision organization.')
        if not isinstance(self.payload, dict) or self.payload.get('state') != 'draft' or self.payload.get('operational_effect') != 'none':
            raise ValidationError('Only inactive draft evidence can be stored.')
