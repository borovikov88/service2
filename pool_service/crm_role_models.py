"""Reviewed CRM role metadata. No backfill or automatic permission grants."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from .crm_commercial_models import CommercialModel, CommercialQuerySet
from .services.crm_role_policy_preview import CAPABILITIES, ROLES


class RolePolicyQuerySet(CommercialQuerySet):
    def delete(self):
        raise ValidationError('Reviewed role policy cannot be silently removed.')


class CrmRolePolicy(CommercialModel):
    objects = RolePolicyQuerySet.as_manager()

    def save(self, *args, **kwargs):
        from .services.crm_role_runtime import is_reviewed_write
        if not is_reviewed_write(self.organization_id, self.user_id):
            raise ValidationError('Role policy writes require reviewed transition.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Reviewed role policy cannot be silently removed.')

    organization = models.ForeignKey('pool_service.Organization', on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    version = models.PositiveIntegerField(default=1)
    revision = models.PositiveIntegerField(default=1)
    roles = models.JSONField(default=list)
    overrides = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
    immutable_scope = ('organization_id', 'user_id')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'user'], name='crm_role_policy_org_user')]

    def clean(self):
        super().clean()
        if self.version != 1 or type(self.roles) is not list or not self.roles or any(
                type(r) is not str or r not in ROLES for r in self.roles):
            raise ValidationError('Invalid CRM role policy version or roles.')
        if self.roles != sorted(set(self.roles)):
            raise ValidationError('CRM policy roles must be canonical.')
        if type(self.overrides) is not dict or set(self.overrides) != set(CAPABILITIES) or any(
                type(v) is not str or v not in ('inherit', 'allow', 'deny') for v in self.overrides.values()):
            raise ValidationError('A complete reviewed override map is required.')
