"""Scoped publication proofs and append-only provider report observations."""
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError

class AvitoServicesPublication(models.Model):
    version = models.OneToOneField('pool_service.AvitoServicesFeedVersion',
        on_delete=models.CASCADE, related_name='publication')
    account_id = models.CharField(max_length=255)
    organization_id = models.PositiveBigIntegerField()
    immutable_source_sha256 = models.CharField(max_length=64)
    stable_source_sha256 = models.CharField(max_length=64)
    file_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ValidationError('Привязка опубликованной версии неизменяема.')
        return super().save(*args, **kwargs)

class AvitoServicesPublicationAttempt(models.Model):
    publication = models.ForeignKey(AvitoServicesPublication, on_delete=models.CASCADE, related_name='attempts')
    upload_id = models.CharField(max_length=32)
    report_sha256 = models.CharField(max_length=64)
    report_started_at = models.DateTimeField(null=True)
    checked_at = models.DateTimeField(auto_now_add=True)
    state = models.CharField(max_length=40)
    upload_status = models.CharField(max_length=40, blank=True)
    source_confirmed = models.BooleanField(default=False)
    content_verified = models.BooleanField(default=False)
    rows = models.JSONField(default=list)

    class Meta:
        ordering = ['-report_started_at', '-pk']
        constraints = [models.UniqueConstraint(fields=['publication', 'upload_id', 'report_sha256'],
                                                name='avito_pub_report_unique')]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ValidationError('Результат загрузки неизменяем. Сохраните новое наблюдение.')
        return super().save(*args, **kwargs)

class AvitoServicesListingIntent(models.Model):
    feed = models.ForeignKey('pool_service.AvitoServicesFeed', on_delete=models.CASCADE, related_name='listing_intents')
    avito_id = models.CharField(max_length=32)
    account_id = models.CharField(max_length=255)
    organization_id = models.PositiveBigIntegerField()
    owner_withdrawn = models.BooleanField(default=False)
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    marked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['feed', 'avito_id'], name='avito_feed_intent_unique')]
