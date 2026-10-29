"""Document links and manual period allocations, never an accounting ledger."""
from django.conf import settings
from django.db import models


class ServiceContractBinding(models.Model):
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    deal = models.OneToOneField('CrmDeal', on_delete=models.PROTECT)
    client = models.ForeignKey('Client', on_delete=models.PROTECT)
    source_id = models.CharField(max_length=64)
    external_organization = models.CharField(max_length=36)
    external_client = models.CharField(max_length=36)
    contract_ref = models.CharField(max_length=36)
    object_kind = models.CharField(max_length=16)
    pool = models.ForeignKey("Pool", null=True, on_delete=models.PROTECT)
    client_object = models.ForeignKey("ClientOwnedObject", null=True, on_delete=models.PROTECT)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization','source_id','external_organization','contract_ref'],name='service_contract_scope_unique'),
            models.CheckConstraint(condition=(models.Q(object_kind='pool',pool__isnull=False,client_object__isnull=True) | models.Q(object_kind='client_object',pool__isnull=True,client_object__isnull=False)),name='service_contract_object_shape')]


class ServiceBoundDocument(models.Model):
    contract = models.ForeignKey(ServiceContractBinding, on_delete=models.PROTECT)
    document_ref = models.CharField(max_length=36)
    revision = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, default='requires_reconciliation')
    current_snapshot_hash = models.CharField(max_length=64)
    current_snapshot = models.JSONField()
    active_version = models.ForeignKey('ServiceAllocationVersion', null=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['contract','document_ref'],name='service_bound_doc_unique')]


class ServiceAllocationVersion(models.Model):
    document = models.ForeignKey(ServiceBoundDocument, on_delete=models.PROTECT, related_name='versions')
    revision = models.PositiveIntegerField()
    snapshot_hash = models.CharField(max_length=64)
    snapshot = models.JSONField()
    allocations = models.JSONField()
    reason = models.CharField(max_length=1000)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['document','revision'],name='service_alloc_revision_unique')]


class ServiceBindingEvent(models.Model):
    organization = models.ForeignKey('Organization', on_delete=models.PROTECT)
    document = models.ForeignKey(ServiceBoundDocument, on_delete=models.PROTECT)
    operation_key = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    kind = models.CharField(max_length=32)
    evidence = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization','operation_key'],name='service_binding_operation_unique')]
