"""Services-only, read-only assistant authority; no new user permissions."""
from django.contrib.auth.models import User
from pool_service.communication_models import ChannelConnection
from pool_service.communication_services import conversation_capability
from pool_service.models import OrganizationAccess
from pool_service.avito_advance import _credential_stamp

CONNECTION_ID = 4
ACCOUNT_ID = "446312402"


def scoped_connection():
    connection = ChannelConnection.objects.select_related("channel__organization").filter(
        pk=CONNECTION_ID, external_id=ACCOUNT_ID, is_active=True,
        channel__is_active=True, channel__kind="avito",
    ).first()
    return connection if connection and _credential_stamp(connection) else None


def can_access_avito_mcp(user, organization):
    if not user or not getattr(user, "is_authenticated", False) or not organization:
        return False
    # Re-read mutable identity flags. A token retains no previous user's rights.
    current = User.objects.filter(pk=user.pk, is_active=True, is_superuser=False).first()
    connection = scoped_connection()
    return bool(current and connection and connection.channel.organization_id == organization.pk
        and OrganizationAccess.objects.filter(user=current, organization=organization,
                                              role__in=("owner", "admin")).exists()
        and conversation_capability(current, "can_manage_channels", organization))
