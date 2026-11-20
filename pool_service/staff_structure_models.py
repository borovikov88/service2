"""Organization hierarchy and dated appointments shared by Service2 modules."""
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class StaffDepartment(models.Model):
    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    parent = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'name'], name='staff_department_name')]


class DepartmentAssignment(models.Model):
    POSITIONS = [('member', 'Сотрудник отдела'), ('sales_head', 'Руководитель отдела продаж')]
    department = models.ForeignKey(StaffDepartment, related_name='assignments', on_delete=models.PROTECT)
    employee = models.ForeignKey('pool_service.Employee', on_delete=models.PROTECT)
    user_at_assignment = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    position = models.CharField(max_length=16, choices=POSITIONS)
    includes_children = models.BooleanField(default=False)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+', null=True)

    class Meta:
        indexes = [models.Index(fields=['department', 'valid_to'], name='staff_dept_active'),
                   models.Index(fields=['employee', 'valid_from'], name='staff_dept_person')]
        constraints = [models.CheckConstraint(condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F('valid_from')),
                                            name='staff_department_period')]
