"""Typed source provenance for existing CRM cards; not a lead or order."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class CrmSourceLink(models.Model):
    QUALITY_CHOICES = [("unknown", "Не оценено"), ("target", "Целевое"), ("non_target", "Нецелевое")]
    AD_SOURCE_CHOICES = [("unknown", "Источник неизвестен"), ("provider", "Контекст Авито"),
                         ("manual", "Подтверждено менеджером")]
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.CASCADE)
    avito_connection = models.ForeignKey("pool_service.ChannelConnection", on_delete=models.PROTECT)
    conversation = models.OneToOneField("pool_service.Conversation", null=True, blank=True,
        on_delete=models.PROTECT, related_name="crm_source_link")
    phone_call = models.OneToOneField("pool_service.PhoneCall", null=True, blank=True,
        on_delete=models.PROTECT, related_name="crm_source_link")
    crm_item = models.ForeignKey("pool_service.CrmItem", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="source_links")
    next_task = models.ForeignKey("pool_service.ServiceTask", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="crm_source_followups")
    call_origin = models.CharField(max_length=16, choices=[("unknown", "Источник звонка неизвестен"), ("manual", "Авито: подтверждено менеджером")], default="unknown")
    order_guid = models.UUIDField(null=True, blank=True)
    order_evidence = models.JSONField(default=dict, blank=True, editable=False)
    avito_item_id = models.CharField(max_length=64, blank=True)
    ad_source = models.CharField(max_length=16, choices=AD_SOURCE_CHOICES, default="unknown")
    quality = models.CharField(max_length=16, choices=QUALITY_CHOICES, default="unknown")
    reason = models.CharField(max_length=500, blank=True)
    evidence = models.JSONField(default=dict, blank=True, editable=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="confirmed_crm_sources")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    last_operation = models.UUIDField(null=True, blank=True, editable=False)
    last_operation_hash = models.CharField(max_length=64, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "avito_connection"], name="crm_source_scope_idx")]
        constraints = [models.CheckConstraint(
            condition=(models.Q(conversation__isnull=False, phone_call__isnull=True)
                | models.Q(conversation__isnull=True, phone_call__isnull=False)),
            name="crm_source_exactly_one")]

    def clean(self):
        if bool(self.conversation_id) == bool(self.phone_call_id):
            raise ValidationError("Нужен ровно один источник.")
        if self.avito_connection_id and self.organization_id != self.avito_connection.channel.organization_id:
            raise ValidationError("Подключение принадлежит другой организации.")
        for value in (self.conversation if self.conversation_id else None,
                      self.phone_call if self.phone_call_id else None,
                      self.crm_item if self.crm_item_id else None,
                      self.next_task if self.next_task_id else None):
            if value is not None and value.organization_id != self.organization_id:
                raise ValidationError("Источник и CRM должны принадлежать одной организации.")
        if self.conversation_id and self.conversation.connection_id != self.avito_connection_id:
            raise ValidationError("Диалог принадлежит другому подключению.")
        if self.next_task_id and (not self.crm_item_id or self.next_task.crm_item_id != self.crm_item_id):
            raise ValidationError("Следующий шаг должен относиться к выбранной заявке.")
