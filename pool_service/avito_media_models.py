"""Scoped immutable photographs used by the services feed."""
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from .avito_media_storage import avito_image_storage


class AvitoServicesImage(models.Model):
    connection = models.ForeignKey('pool_service.ChannelConnection', on_delete=models.CASCADE)
    account_id = models.CharField(max_length=255)
    organization_id_at_upload = models.PositiveBigIntegerField()
    public_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    public_url = models.CharField(max_length=2000)
    file = models.FileField(storage=avito_image_storage, max_length=255)
    sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    published_in_feed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['connection', 'account_id', 'organization_id_at_upload', 'sha256'],
            name='avito_image_scope_digest_uniq')]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ValidationError('Сохранённое изображение неизменяемо. Загрузите новое.')
        return super().save(*args, **kwargs)
