from django.db.models import Q

from .models import Client


def active_clients(queryset=None):
    """Return clients that are still active CRM identities.

    Legacy cards retained after a merge have ClientCRMProfile.merged_into set
    and must never be selectable for new operational records.
    """
    queryset = queryset if queryset is not None else Client.objects.all()
    return queryset.filter(
        Q(crm_profile__isnull=True)
        | Q(crm_profile__merged_into__isnull=True)
    )
