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


CONTACT_STAFF_ROLES = {"owner", "admin", "service", "installer", "manager"}


def readable_clients(user):
    from .models import OrganizationAccess
    qs = active_clients()
    if not user.is_authenticated or not user.is_active:
        return qs.none()
    if user.is_superuser:
        return qs
    return qs.filter(organization_id__in=OrganizationAccess.objects.filter(
        user=user, role__in=CONTACT_STAFF_ROLES,
    ).values("organization_id"))


def readable_company_links(user):
    from django.db.models import F
    from .client_crm_models import ClientCompanyLink
    allowed = readable_clients(user).values("pk")
    return ClientCompanyLink.objects.filter(
        company_id__in=allowed, person_id__in=allowed,
        company__organization_id=F("person__organization_id"),
        company__client_type="legal", person__client_type="private",
    )
