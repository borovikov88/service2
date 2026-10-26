"""Current legacy-object read policy, shared by lookup, search and commands.

ClientOwnedObject uses client_detail's CLIENT_CARD_ROLES. Pool uses pool_detail's
organization/client/pool read alternatives. Commercial read/write authorization
is an additional prerequisite, never replaced by this object policy.
"""
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from pool_service.client_crm_models import ClientOwnedObject
from pool_service.models import Client, ClientAccess, OrganizationAccess, Pool, PoolAccess

CLIENT_ROLES = {"owner", "admin", "manager", "service", "installer"}
POOL_ROLES = CLIENT_ROLES | {"accountant"}


def object_querysets(user, organization_id, *, using="default", lock=False):
    memberships = OrganizationAccess.objects.using(using).filter(user_id=user.pk, organization_id=organization_id)
    if lock:
        memberships = memberships.select_for_update()
    roles = set(memberships.values_list("role", flat=True))
    owned = ClientOwnedObject.objects.using(using).filter(client__organization_id=organization_id)
    pools = Pool.objects.using(using).filter(organization_id=organization_id,
        client__organization_id=organization_id, is_deleted=False)
    if not (user.is_superuser or roles & CLIENT_ROLES):
        owned = owned.none()
    if not (user.is_superuser or roles & POOL_ROLES):
        client_access = ClientAccess.objects.using(using).filter(user_id=user.pk)
        pool_access = PoolAccess.objects.using(using).filter(user_id=user.pk)
        if lock:
            # Lock the actual grants, rather than relying on a snapshot subquery.
            client_ids = list(client_access.select_for_update().values_list("client_id", flat=True))
            pool_ids = list(pool_access.select_for_update().values_list("pool_id", flat=True))
        else:
            client_ids = client_access.values("client_id")
            pool_ids = pool_access.values("pool_id")
        pools = pools.filter(Q(client__user_id=user.pk) | Q(client_id__in=client_ids) | Q(pk__in=pool_ids))
    return pools, owned


def validate_deal_object(using, actor, deal):
    pools, owned = object_querysets(actor, deal.organization_id, using=using, lock=True)
    if deal.pool_id and not pools.select_for_update().filter(pk=deal.pool_id, client_id=deal.client_id).exists():
        raise PermissionDenied("Объект сделки сейчас недоступен. Обновите карточку объекта.")
    if deal.client_object_id and not owned.select_for_update().filter(pk=deal.client_object_id, client_id=deal.client_id).exists():
        raise PermissionDenied("Объект сделки сейчас недоступен. Обновите карточку объекта.")
