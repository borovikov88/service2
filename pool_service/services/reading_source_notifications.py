"""Existing source pushes after commit, with bounded current decision snapshots.

No parent Org/Pool/Client locks: restore notifications take parent FK locks while
holding Reading. Full task/CRM graph -> Reading -> recipient matches those paths.
Parent metadata is re-read after waits, not serialized through external delivery.
"""
import logging

from django.db import transaction
from django.contrib.auth import get_user_model
from django.urls import reverse

from pool_service.models import ClientAccess, Employee, Notification, OrganizationAccess, Pool, ServiceTask, WaterReading
from pool_service.services.task_read_permissions import readable_tasks

logger = logging.getLogger(__name__)


def _read_committed_session():
    connection = transaction.get_connection()
    if connection.vendor != 'mysql':
        return True
    # Inspect the actual session default too, not only Django's configured
    # isolation_level. Out-of-band per-transaction overrides are not supported.
    variable = 'tx_isolation' if connection.mysql_is_mariadb else 'transaction_isolation'
    with connection.cursor() as cursor:
        cursor.execute('SELECT @@SESSION.' + variable)
        isolation = str(cursor.fetchone()[0]).lower().replace('-', ' ')
    return isolation == 'read committed'


def _recipient(organization, user_id):
    from pool_service.operations_mcp_views import OPERATIONAL_STAFF_ROLES
    # Notification INSERTs hold shared recipient FK locks while bulk restore
    # proceeds to its next Reading. An exclusive User lock here would reverse
    # that order. Never call _locked_staff_user after this shared lock: that
    # would upgrade it back to the same unsafe exclusive lock.
    user_model = get_user_model()
    connection = transaction.get_connection(using=user_model.objects.db)
    if connection.vendor in ('mysql', 'postgresql'):
        table = connection.ops.quote_name(user_model._meta.db_table)
        column = connection.ops.quote_name(user_model._meta.pk.column)
        clause = 'LOCK IN SHARE MODE' if connection.vendor == 'mysql' else 'FOR SHARE'
        actor = next(iter(user_model.objects.raw(
            f'SELECT * FROM {table} WHERE {column} = %s {clause}', [user_id])), None)
    elif connection.vendor == 'sqlite':
        actor = user_model.objects.filter(pk=user_id).first()
    else:
        return None
    if not actor or not actor.is_active:
        return None
    roles = list(OrganizationAccess.objects.select_for_update().filter(organization=organization, user_id=user_id).order_by('pk'))
    employees = list(Employee.objects.select_for_update().filter(organization=organization, user_id=user_id).order_by('pk'))
    if (not any(row.role in OPERATIONAL_STAFF_ROLES for row in roles)
            or any(not row.is_active or row.employment_status == Employee.STATUS_DISMISSED for row in employees)):
        return None
    return actor


def _parents(reading):
    # Deliberately no select_for_update. On supported MySQL READ COMMITTED this
    # statement sees committed metadata after the preceding source/actor waits.
    pool = Pool.objects.select_related('client', 'organization').filter(pk=reading.pool_id).first()
    if (not pool or pool.is_deleted or not pool.organization_id
            or pool.client.organization_id != pool.organization_id):
        return None
    return pool, pool.client, pool.organization


def _parent_identity(parents):
    pool, client, org = parents
    return pool.pk, pool.organization_id, pool.client_id, client.organization_id, org.pk


@transaction.atomic
def deliver_source_push(kind, source_id, user_id, notification_id=None):
    from pool_service.services import notifications
    from pool_service.services.crm_object_needs import (
        NeedConflict, editable, lock_source_graphs, source_graph_signature, validate_graph,
    )
    if not _read_committed_session():
        logger.info('Source push suppressed: callback requires READ COMMITTED session isolation.')
        return 'unsupported_isolation'
    if kind == 'task':
        seed = ServiceTask.objects.filter(pk=source_id, task_type=ServiceTask.TYPE_SUPPLY_REQUEST).values('water_reading_id').first()
        reading_id = seed['water_reading_id'] if seed else None
    elif kind == 'reading':
        reading_id = source_id
    else:
        return
    if not reading_id:
        return
    graph, signature = lock_source_graphs([reading_id], with_signature=True)
    candidates = graph.get(reading_id, [])
    if len(candidates) > 1:
        return
    task = candidates[0] if candidates else None  # exactly one, never a selection heuristic
    if kind == 'task' and (not task or task.pk != source_id):
        return
    reading = WaterReading.objects.select_for_update().filter(pk=reading_id).first()
    if not reading or reading.is_deleted:
        return
    # Includes empty -> nonempty and shared-item graph growth. Never acquire a
    # newly discovered task lock after Reading, particularly before User waits.
    if source_graph_signature([reading_id]) != signature:
        return 'graph_changed'
    parents = _parents(reading)
    if not parents:
        return
    pool, client, org = parents
    reading.pool = pool
    if task:
        if not task.crm_item_id:
            return
        try:
            validate_graph(reading, task, task.crm_item, org.pk)
        except NeedConflict:
            return
    actor = _recipient(org, user_id)
    if not actor or user_id == reading.added_by_id:
        return
    # Current parent metadata after recipient lock waits, including changed
    # org/client bindings or deletion. This is a decision snapshot, not a lock
    # guarantee against legacy reparent during the external network send.
    current_parents = _parents(reading)
    if not current_parents or _parent_identity(current_parents) != _parent_identity(parents):
        return
    pool, client, org = current_parents
    reading.pool = pool
    note = Notification.objects.filter(pk=notification_id, user_id=user_id, organization=org).first() if notification_id else None
    if task:
        try:
            validate_graph(reading, task, task.crm_item, org.pk)
        except NeedConflict:
            return
    if kind == 'task':
        if not editable(task) or not readable_tasks(ServiceTask.objects.filter(pk=task.pk), actor):
            return
        task.water_reading, task.pool, task.client = reading, pool, client
        title, message, action_url = notifications.task_assignment_notification_content(task)
    else:
        if not OrganizationAccess.objects.filter(organization=org, user_id=user_id, role__in=notifications.SERVICE_NOTIFICATION_ROLES).exists():
            return
        staff_source = OrganizationAccess.objects.filter(organization=org, user_id=reading.added_by_id).exists()
        client_source = (client.user_id == reading.added_by_id or ClientAccess.objects.filter(client=client, user_id=reading.added_by_id).exists())
        enabled = org.notify_limits_pool_staff_push if not staff_source and client_source else org.notify_limits_service_staff_push
        limits = notifications._limits_for_org(org)
        if not enabled or pool.service_suspended or not notifications._reading_violations(reading, limits):
            return
        violations = notifications._reading_violation_messages(reading, limits)
        title = 'Показатели вне нормы'
        message = f'{notifications._object_location_phrase(client.name or pool.address)} ' + '; '.join(violations)
        action_url = reverse('pool_detail', kwargs={'pool_uuid':pool.uuid})
    notifications.send_push_to_users([actor], title=title, message=message, action_url=action_url, notification=note)
