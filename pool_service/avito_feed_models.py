"""Account-bound, explicitly published services feed snapshots."""
import uuid
from django.conf import settings
from django.db import models


class AvitoServicesFeed(models.Model):
    connection = models.OneToOneField('pool_service.ChannelConnection', on_delete=models.CASCADE)
    account_id = models.CharField(max_length=255)
    organization_id_at_import = models.PositiveBigIntegerField()
    draft = models.JSONField(default=list)
    published = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    published_revision = models.PositiveIntegerField(default=0)
    public_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
