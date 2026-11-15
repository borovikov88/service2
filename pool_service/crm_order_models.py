"""Saved 1C order evidence and explicit commercial links; not an accounting ledger."""
from django.conf import settings
from django.db import models
from django.utils import timezone
from .crm_commercial_models import LeadEvidenceModel


class CrmOrderSyncState(models.Model):
    organization = models.OneToOneField('Organization', on_delete=models.PROTECT)
    source_id = models.CharField(max_length=64)
    source_at = models.DateTimeField()
    fetched_at = models.DateTimeField()
    failed = models.BooleanField(default=False)


class CrmOneCOrder(models.Model):
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    source_id = models.CharField(max_length=64)
    external_organization = models.CharField(max_length=36)
    order_guid = models.CharField(max_length=36)
    customer_guid = models.CharField(max_length=36)
    agreement_guid = models.CharField(max_length=36, blank=True)
    currency_guid = models.CharField(max_length=36, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    number = models.CharField(max_length=80)
    order_date = models.DateTimeField()
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    realized_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    paid_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    realization_complete = models.BooleanField(default=False)
    payment_complete = models.BooleanField(default=False)
    order_valid = models.BooleanField(default=False)
    cancelled = models.BooleanField(default=False)
    missing = models.BooleanField(default=False)
    blockers = models.JSONField(default=list)
    documents = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64)
    source_at = models.DateTimeField()
    fetched_at = models.DateTimeField()
    current_snapshot = models.ForeignKey('CrmOrderEvidenceSnapshot', null=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'source_id', 'external_organization', 'order_guid'], name='crm_onec_order_scope_uq')]
        indexes = [models.Index(fields=['organization', 'customer_guid'], name='crm_order_customer_idx')]


class CrmOrderEvidenceSnapshot(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'order_id', 'fingerprint', 'source_at', 'fetched_at', 'data')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    order = models.ForeignKey(CrmOneCOrder, on_delete=models.PROTECT, related_name='snapshots')
    fingerprint = models.CharField(max_length=64)
    source_at = models.DateTimeField()
    fetched_at = models.DateTimeField()
    data = models.JSONField()


class CrmDealOrderLink(models.Model):
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    deal = models.OneToOneField('CrmDeal', on_delete=models.PROTECT, related_name='onec_order_link')
    order = models.OneToOneField(CrmOneCOrder, on_delete=models.PROTECT, related_name='deal_link')
    client = models.ForeignKey('Client', on_delete=models.PROTECT)
    object_scope = models.JSONField()
    scope_fingerprint = models.CharField(max_length=64)
    recurring = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(default=timezone.now)
    completed_automatically = models.BooleanField(default=False)
    completion_snapshot = models.ForeignKey(CrmOrderEvidenceSnapshot, null=True, on_delete=models.PROTECT, related_name='+')
    requires_reconciliation = models.BooleanField(default=False)
    review_reason = models.CharField(max_length=500, blank=True)


class CrmOrderLinkEvent(LeadEvidenceModel):
    immutable_scope = ('organization_id', 'deal_id', 'order_id', 'snapshot_id', 'command_id', 'actor_id', 'kind', 'recurring', 'object_scope', 'scope_fingerprint', 'created_at')
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    deal = models.ForeignKey('CrmDeal', on_delete=models.PROTECT)
    order = models.ForeignKey(CrmOneCOrder, on_delete=models.PROTECT)
    snapshot = models.ForeignKey(CrmOrderEvidenceSnapshot, null=True, on_delete=models.PROTECT)
    command = models.OneToOneField('CrmCommand', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    recurring = models.BooleanField()
    object_scope = models.JSONField()
    scope_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
