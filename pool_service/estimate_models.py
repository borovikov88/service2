"""Versioned commercial calculations; no connection to accounting or 1C writes."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class PoolEstimate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class PoolEstimateVersion(models.Model):
    estimate = models.ForeignKey(PoolEstimate, related_name="versions", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["estimate", "number"], name="pool_estimate_version_uniq")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённая версия неизменяема. Создайте новую версию.")
        return super().save(*args, **kwargs)


class PoolEstimateWorkRate(models.Model):
    """Highest confirmed manual rate, isolated by organization and quote tier."""
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.CASCADE)
    variant = models.CharField(max_length=16)
    identity = models.CharField(max_length=64)
    work_code = models.CharField(max_length=80)
    unit = models.CharField(max_length=20)
    finish_type = models.CharField(max_length=20, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization', 'variant', 'identity'], name='pool_work_rate_org_tier_uniq'),
            models.CheckConstraint(condition=models.Q(price__gte=0), name='pool_work_rate_nonnegative'),
        ]
