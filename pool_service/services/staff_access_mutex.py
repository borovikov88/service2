"""Shared organization/user mutex for legacy membership actions.

The wrapped view retains authorization. Lock before it reads authorization or
performs any membership/signal write, not in post_save after the fact.
"""
from functools import wraps
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from pool_service.models import Organization, OrganizationAccess


def serialized_staff_action(view):
    @wraps(view)
    def wrapped(request, access_id, *args, **kwargs):
        if request.method != 'POST':
            return view(request, access_id, *args, **kwargs)
        peek = OrganizationAccess.objects.filter(pk=access_id).values('organization_id', 'user_id').first()
        if peek is None:
            raise Http404
        with transaction.atomic():
            org = Organization.objects.select_for_update().get(pk=peek['organization_id'])
            users = {user.pk: user for user in get_user_model().objects.select_for_update().filter(
                pk__in=sorted({request.user.pk, peek['user_id']})).order_by('pk')}
            from .staff_lifecycle import manages_staff
            actor = users.get(request.user.pk)
            if not actor or not manages_staff(actor, org):
                raise PermissionDenied
            request.user = actor
            current = OrganizationAccess.objects.filter(pk=access_id).values('organization_id', 'user_id').first()
            if current != peek:
                raise Http404
            # In opt-in mode every role edit must reach the reviewed owner flow.
            # Its own scope check rejects unsupported/admin/technical targets.
            from pool_service.crm_role_models import CrmRolePolicy
            managed = CrmRolePolicy.objects.filter(organization_id=peek['organization_id'], user_id=peek['user_id']).exists()
            if view.__name__ == 'staff_change_role' and (managed or getattr(settings, 'CRM_ROLE_TRANSITION_ENABLED', False) is True):
                from .crm_role_transition import prepare_role_transition
                prepare_role_transition(actor_id=request.user.pk, organization_id=peek['organization_id'], target_id=peek['user_id'])
                return redirect('crm_role_review', organization_id=peek['organization_id'], target_id=peek['user_id'])
            return view(request, access_id, *args, **kwargs)
    return wrapped
