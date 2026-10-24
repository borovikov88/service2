"""Account-bound, explicitly published services feed snapshots."""
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
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
    # Existing public snapshots keep their former contract; every new publication sets10.
    published_photo_limit = models.PositiveSmallIntegerField(default=30)
    latest_observations = models.JSONField(default=list)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class AvitoServicesFeedVersion(models.Model):
    """Application append-only events; no endpoint updates/deletes an existing event."""
    ACTIONS = [
        ('baseline', 'Снимок прежнего черновика'), ('legacy_published', 'Снимок прежнего файла'),
        ('import', 'Импорт черновика'), ('edit', 'Изменение черновика'),
        ('restore', 'Восстановление в новый черновик'), ('publish', 'Публикация файла'),
        ('verify', 'Проверка статусов Авито'), ('disable', 'Отключение файла'),
        ('policy', 'Политика возврата объявления'),
    ]
    feed = models.ForeignKey(AvitoServicesFeed, on_delete=models.CASCADE, related_name='versions')
    revision = models.PositiveIntegerField()
    published_revision = models.PositiveIntegerField(default=0)
    action = models.CharField(max_length=24, choices=ACTIONS)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=500, blank=True)
    snapshot = models.JSONField(default=list)
    changes = models.JSONField(default=list)
    observations = models.JSONField(default=list)
    file_digest = models.CharField(max_length=64, blank=True)
    source_version = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['-pk']
        indexes = [models.Index(fields=['feed', 'created_at'], name='avito_feed_history_idx')]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ValidationError('Сохранённая версия неизменяема. Создайте новую версию.')
        return super().save(*args, **kwargs)
