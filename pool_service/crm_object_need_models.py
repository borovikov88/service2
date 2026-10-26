"""Object-need anchors and immutable source/review history; execution stays in tasks."""
import hashlib
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

MODES = {
    'included': 'Включено в обслуживание',
    'chemistry_monthly': 'Химия с месячным расчётом',
    'approval_required': 'Требуется согласование',
    'warranty': 'Гарантия', 'error': 'Ошибка записи',
    'cancelled': 'Потребность отменена', 'already_supplied': 'Уже обеспечено',
}


def validate_positions(value):
    if type(value) is not list or not 1 <= len(value) <= 50:
        raise ValidationError('Укажите от 1 до 50 позиций.')
    tokens = set()
    for line in value:
        if type(line) is not dict or set(line) != {'line_token', 'description', 'mode', 'reason'}:
            raise ValidationError('Неизвестные поля позиции.')
        token = line['line_token']
        if type(token) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', token) or token in tokens:
            raise ValidationError('Позициям нужны уникальные устойчивые идентификаторы.')
        tokens.add(token)
        if type(line['mode']) is not str or line['mode'] not in MODES:
            raise ValidationError('Выберите допустимый режим или исключение.')
        for field, bound in (('description', 1000), ('reason', 500)):
            text = line[field]
            if type(text) is not str or not text.strip() or text != text.strip() or len(text) > bound:
                raise ValidationError('Описание и основание обязательны и ограничены по длине.')


class NeedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Use the object-need service.')

    def bulk_update(self, *args, **kwargs):
        raise ValidationError('Use the object-need service.')

    def bulk_create(self, *args, **kwargs):
        raise ValidationError('Use the object-need service.')

    def delete(self):
        raise ValidationError('Object need history is retained.')


class NeedModel(models.Model):
    objects = NeedQuerySet.as_manager()
    immutable = False

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ValidationError('Object need history is retained.')

    def save(self, *args, **kwargs):
        # A trusted low-level ORM boundary, not application authorization.
        # Callers lock the source graph first; never introduce org locks here.
        with transaction.atomic():
            if self.pk and type(self).objects.filter(pk=self.pk).exists():
                if self.immutable:
                    raise ValidationError('Object need history is immutable.')
                previous = type(self).objects.get(pk=self.pk)
                for name in ('organization_id', 'reading_id', 'task_id', 'crm_item_id'):
                    if getattr(previous, name) != getattr(self, name):
                        raise ValidationError('Object need identity is immutable.')
            self.full_clean()
            return super().save(*args, **kwargs)


class CrmObjectNeed(NeedModel):
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    reading = models.OneToOneField('pool_service.WaterReading', on_delete=models.PROTECT, related_name='crm_need')
    task = models.OneToOneField('pool_service.ServiceTask', on_delete=models.PROTECT, related_name='object_need')
    crm_item = models.OneToOneField('pool_service.CrmItem', on_delete=models.PROTECT, related_name='object_need')
    source_revision = models.PositiveIntegerField(default=0)
    review_revision = models.PositiveIntegerField(default=0)

    class Meta:
        base_manager_name = 'objects'
        indexes = [models.Index(fields=['organization', 'id'], name='crm_need_org_idx')]

    def clean(self):
        from pool_service.services.crm_object_needs import validate_graph
        validate_graph(self.reading, self.task, self.crm_item, self.organization_id)


class CrmObjectNeedSourceRevision(NeedModel):
    immutable = True
    need = models.ForeignKey(CrmObjectNeed, on_delete=models.PROTECT, related_name='sources')
    sequence = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    materials = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['need', 'sequence'], name='crm_need_source_sequence')]
        ordering = ['sequence']

    def clean(self):
        if self.sequence < 1 or self.content_hash != hashlib.sha256(self.materials.encode()).hexdigest():
            raise ValidationError('Invalid source revision.')


class CrmObjectNeedReview(NeedModel):
    immutable = True
    need = models.ForeignKey(CrmObjectNeed, on_delete=models.PROTECT, related_name='reviews')
    source = models.ForeignKey(CrmObjectNeedSourceRevision, on_delete=models.PROTECT, related_name='reviews')
    sequence = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    command = models.OneToOneField('pool_service.CrmCommand', on_delete=models.PROTECT, related_name='object_need_review')
    positions = models.JSONField(validators=[validate_positions])
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = 'objects'
        constraints = [models.UniqueConstraint(fields=['need', 'sequence'], name='crm_need_review_sequence')]
        ordering = ['sequence']

    def clean(self):
        if (self.sequence < 1 or self.source.need_id != self.need_id
                or self.command.organization_id != self.need.organization_id
                or self.command.actor_id != self.actor_id
                or self.command.command_type != 'object_need.review'):
            raise ValidationError('Invalid review source/command graph.')
        validate_positions(self.positions)
