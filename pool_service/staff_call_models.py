"""Organization-bound staff identity and durable communication decisions."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, F
from django.utils import timezone


class StaffAccount(models.Model):
    KINDS = [('unknown', 'Не проверена'), ('person', 'Сотрудник'),
             ('service', 'Служебная'), ('system', 'Системная')]
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=KINDS, default='unknown')
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'user'], name='staff_account_org_user')]


class EmployeeTelephoneAssignment(models.Model):
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    connection = models.ForeignKey('pool_service.TelephonyConnection', on_delete=models.PROTECT)
    employee = models.ForeignKey('pool_service.Employee', null=True, blank=True, on_delete=models.PROTECT)
    phone_key = models.CharField(max_length=16, blank=True)
    extension = models.CharField(max_length=64, blank=True)
    provider_user = models.CharField(max_length=255, blank=True)
    shared = models.BooleanField(default=False)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(valid_to__isnull=True) | Q(valid_to__gt=F('valid_from')), name='staff_phone_period'),
            models.CheckConstraint(condition=~Q(phone_key='', extension='', provider_user=''), name='staff_phone_identifier'),
            models.CheckConstraint(condition=Q(shared=True, employee__isnull=True) | Q(shared=False, employee__isnull=False), name='staff_phone_holder'),
        ]
        indexes = [models.Index(fields=['organization', 'connection', 'valid_from'], name='staff_phone_scope_idx')]

    def clean(self):
        if self.connection_id and self.connection.organization_id != self.organization_id:
            raise ValidationError('АТС относится к другой организации.')
        if self.employee_id and self.employee.organization_id != self.organization_id:
            raise ValidationError('Сотрудник относится к другой организации.')


class CallPrivacyHold(models.Model):
    """A removed number must not reopen previously protected history."""
    call = models.OneToOneField('pool_service.PhoneCall', on_delete=models.PROTECT, related_name='privacy_hold')
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.call_id and self.call.organization_id != self.organization_id:
            raise ValidationError('Разговор относится к другой организации.')


class CallWorkContext(models.Model):
    call = models.OneToOneField('pool_service.PhoneCall', on_delete=models.PROTECT, related_name='work_context')
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    lead = models.ForeignKey('pool_service.CrmLead', null=True, blank=True, on_delete=models.PROTECT)
    deal = models.ForeignKey('pool_service.CrmDeal', null=True, blank=True, on_delete=models.PROTECT)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(default=timezone.now)
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(lead__isnull=False, deal__isnull=True) | Q(lead__isnull=True, deal__isnull=False), name='call_context_one_subject')]

    def clean(self):
        for name in ('call', 'lead', 'deal'):
            field = self._meta.get_field(name)
            pk = getattr(self, field.attname)
            if pk and not field.remote_field.model.objects.filter(pk=pk, organization_id=self.organization_id).exists():
                raise ValidationError('Связь относится к другой организации.')
