"""Existing source pushes after commit, with current scoped recipient checks.

No outbox/retry or new channel. Only typed source/recipient/notification IDs are
captured by the scheduling callback; source text is read again under locks.
"""
from django.db import transaction
from django.urls import reverse

from pool_service.models import Client, ClientAccess, Employee, Notification, Organization, OrganizationAccess, Pool, ServiceTask, WaterReading
from pool_service.services.crm_locking import locked_task_with_crm_graph
from pool_service.services.task_read_permissions import readable_tasks


def _recipient(organization, user_id):
    from pool_service.operations_mcp_views import _locked_staff_user
    actor = _locked_staff_user(organization, user_id)
    if not actor:
        return None
    roles = list(OrganizationAccess.objects.select_for_update().filter(organization=organization, user_id=user_id).order_by('pk'))
    employees = list(Employee.objects.select_for_update().filter(organization=organization, user_id=user_id).order_by('pk'))
    if not roles or any(not row.is_active or row.employment_status == Employee.STATUS_DISMISSED for row in employees):
        return None
    return actor


@transaction.atomic
def deliver_source_push(kind, source_id, user_id, notification_id=None):
    from pool_service.services import notifications
    from pool_service.services.crm_object_needs import NeedConflict, editable, validate_graph
    if kind == 'task':
        org_id = ServiceTask.objects.filter(pk=source_id).values_list('organization_id', flat=True).first()
    elif kind == 'reading':
        org_id = WaterReading.objects.filter(pk=source_id).values_list('pool__organization_id', flat=True).first()
    else:
        return
    if not org_id:
        return
    org = Organization.objects.select_for_update().get(pk=org_id)
    task = locked_task_with_crm_graph(organization=org, task_id=source_id) if kind == 'task' else None
    actor = _recipient(org, user_id)
    if not actor or (kind == 'task' and not task):
        return
    reading_id = task.water_reading_id if task else source_id
    seed = WaterReading.objects.filter(pk=reading_id).values('pool_id').first()
    if not seed:
        return
    pool = Pool.objects.select_for_update().get(pk=seed['pool_id'])
    client = Client.objects.select_for_update().get(pk=pool.client_id)
    reading = WaterReading.objects.select_for_update().filter(pk=reading_id).first()
    if (not reading or reading.pool_id != pool.pk or reading.is_deleted or pool.is_deleted
            or pool.organization_id != org_id or client.organization_id != org_id or user_id == reading.added_by_id):
        return
    pool.client = client
    reading.pool = pool
    note = Notification.objects.filter(pk=notification_id, user_id=user_id, organization=org).first() if notification_id else None
    if task:
        payload = task.payload_json if isinstance(task.payload_json, dict) else {}
        if ('source_call_id' in payload or payload.get('source') == 'call_analysis'
                or not editable(task) or not readable_tasks(ServiceTask.objects.filter(pk=task.pk), actor)):
            return
        try:
            validate_graph(reading, task, task.crm_item, org_id)
        except NeedConflict:
            return
        task.water_reading = reading
        task.pool = pool
        task.client = client
        title, message, action_url = notifications.task_assignment_notification_content(task)
    else:
        if not OrganizationAccess.objects.filter(organization=org, user_id=user_id, role__in=notifications.SERVICE_NOTIFICATION_ROLES).exists():
            return
        staff_source = OrganizationAccess.objects.filter(organization=org, user_id=reading.added_by_id).exists()
        client_source = (client.user_id == reading.added_by_id or ClientAccess.objects.filter(client=client, user_id=reading.added_by_id).exists())
        enabled = org.notify_limits_pool_staff_push if not staff_source and client_source else org.notify_limits_service_staff_push
        if not enabled or pool.service_suspended or not notifications._reading_violations(reading, notifications._limits_for_org(org)):
            return
        violations = notifications._reading_violation_messages(reading, notifications._limits_for_org(org))
        title = 'Показатели вне нормы'
        message = f'{notifications._object_location_phrase(client.name or pool.address)} ' + '; '.join(violations)
        action_url = reverse('pool_detail', kwargs={'pool_uuid':pool.uuid})
    notifications.send_push_to_users([actor], title=title, message=message, action_url=action_url, notification=note)
