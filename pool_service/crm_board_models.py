"""Invalidation epoch, not a snapshot or a business event."""
from django.db import models


class CrmBoardEpoch(models.Model):
    organization = models.OneToOneField('pool_service.Organization', on_delete=models.CASCADE, primary_key=True)
    revision = models.PositiveBigIntegerField(default=0)
