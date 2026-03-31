from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from pool_service.models import (
    ClientAccess,
    CrmItem,
    OrganizationAccess,
    ServiceTask,
    ServiceTaskChange,
)
from pool_service.services.task_archive import archive_task, restore_task
from pool_service.services.notifications import notify_task_assignment, notify_users


def _reading_source_type(reading):
    pool = reading.pool
    organization = getattr(pool, "organization", None)
    if reading.added_by_id and organization and OrganizationAccess.objects.filter(
        user_id=reading.added_by_id,
        organization=organization,
    ).exists():
        return ServiceTask.SOURCE_SERVICE_STAFF
    if reading.added_by_id and pool.client_id and (
        getattr(pool.client, "user_id", None) == reading.added_by_id
        or ClientAccess.objects.filter(user_id=reading.added_by_id, client_id=pool.client_id).exists()
    ):
        return ServiceTask.SOURCE_POOL_STAFF
    return ServiceTask.SOURCE_SYSTEM


def resolve_default_manager(pool):
    organization = getattr(pool, "organization", None)
    if not organization:
        return None
    manager = (
        User.objects.filter(
            is_active=True,
            organizationaccess__organization=organization,
            organizationaccess__role="manager",
        )
        .order_by("last_name", "first_name", "id")
        .distinct()
        .first()
    )
    if manager:
        return manager
    return (
        User.objects.filter(
            is_active=True,
            organizationaccess__organization=organization,
            organizationaccess__role__in=["owner", "admin"],
        )
        .order_by("organizationaccess__role", "last_name", "first_name", "id")
        .distinct()
        .first()
    )


def resolve_manager_team(pool):
    organization = getattr(pool, "organization", None)
    if not organization:
        return []
    managers = list(
        User.objects.filter(
            is_active=True,
            organizationaccess__organization=organization,
            organizationaccess__role="manager",
        )
        .order_by("last_name", "first_name", "id")
        .distinct()
    )
    if managers:
        return managers
    fallback = resolve_default_manager(pool)
    return [fallback] if fallback else []


def resolve_admin_team(pool):
    organization = getattr(pool, "organization", None)
    if not organization:
        return []
    return list(
        User.objects.filter(
            is_active=True,
            organizationaccess__organization=organization,
            organizationaccess__role__in=["owner", "admin"],
        )
        .order_by("organizationaccess__role", "last_name", "first_name", "id")
        .distinct()
    )


def _supply_title(reading):
    pool = reading.pool
    object_name = pool.client.name if pool.client_id and pool.client else pool.address
    return f"Поставка материалов: {object_name}"


def _crm_supply_title(reading):
    materials = (reading.required_materials or "").strip()
    return f'Заявка: "{materials}"' if materials else _supply_title(reading)


def _supply_description(reading):
    author = ""
    if reading.added_by_id and reading.added_by:
        author = reading.added_by.get_full_name() or reading.added_by.username
    lines = [
        f"По записи от {reading.date:%d.%m.%Y %H:%M} требуется поставка материалов.",
        "",
        f"Материалы: {reading.required_materials.strip()}",
    ]
    if author:
        lines.append(f"Автор записи: {author}")
    if reading.pool_id:
        object_name = reading.pool.client.name if reading.pool.client_id and reading.pool.client else reading.pool.address
        lines.append(f"Объект: {object_name}")
    return "\n".join(lines)


def ensure_crm_for_supply_task(task):
    if task.crm_item_id:
        return task.crm_item
    reading = task.water_reading
    if not reading:
        return None
    item = CrmItem.objects.create(
        organization=task.organization,
        direction=CrmItem.DIRECTION_SERVICE,
        title=_crm_supply_title(reading),
        client=task.client,
        pool=task.pool,
        stage=CrmItem.STAGE_SERVICE_NEW,
        urgency=CrmItem.URGENCY_REQUIRED,
        description=task.description,
        equipment_replacement=(reading.required_materials or "").strip(),
        responsible=task.primary_responsible,
        created_by=task.created_by,
    )
    task.crm_item = item
    task.save(update_fields=["crm_item", "updated_at"])
    return item


def sync_crm_item_for_task(task):
    if not task.crm_item_id:
        return None
    crm_item = task.crm_item
    update_fields = []

    if task.task_type == ServiceTask.TYPE_SUPPLY_REQUEST and task.water_reading_id:
        target_title = _crm_supply_title(task.water_reading)
    else:
        target_title = task.title
    if target_title and crm_item.title != target_title:
        crm_item.title = target_title
        update_fields.append("title")

    if task.primary_responsible_id and crm_item.responsible_id != task.primary_responsible_id:
        crm_item.responsible = task.primary_responsible
        update_fields.append("responsible")

    if task.description and crm_item.description != task.description:
        crm_item.description = task.description
        update_fields.append("description")

    if crm_item.direction == CrmItem.DIRECTION_SERVICE:
        target_stage = CrmItem.STAGE_SERVICE_DONE if task.completed_at else CrmItem.STAGE_SERVICE_IN_PROGRESS
        if crm_item.stage != target_stage:
            crm_item.stage = target_stage
            update_fields.append("stage")

    if update_fields:
        crm_item.save(update_fields=[*update_fields, "updated_at"])
    return crm_item


def sync_task_with_crm_item(task):
    if not task or not task.crm_item_id:
        return task
    crm_item = task.crm_item
    if crm_item.direction == CrmItem.DIRECTION_SERVICE:
        if crm_item.stage == CrmItem.STAGE_SERVICE_DONE:
            archive_task(task, ServiceTask.ARCHIVE_REASON_COMPLETED, crm_item.responsible or task.completed_by)
        else:
            if task.completed_at:
                task.completed_at = None
                task.completed_by = None
                task.save(update_fields=["completed_at", "completed_by", "updated_at"])
            if task.is_archived and task.archived_reason == ServiceTask.ARCHIVE_REASON_COMPLETED:
                restore_task(task)
    return task


@transaction.atomic
def create_supply_task_from_reading(reading):
    materials = (reading.required_materials or "").strip()
    if not materials:
        return None
    if not reading.pool_id or not getattr(reading.pool, "organization_id", None):
        return None

    existing = (
        ServiceTask.objects.filter(
            water_reading=reading,
            task_type=ServiceTask.TYPE_SUPPLY_REQUEST,
            is_archived=False,
            status__in=[
                ServiceTask.STATUS_NEW,
                ServiceTask.STATUS_IN_PROGRESS,
                ServiceTask.STATUS_WAITING,
            ],
        )
        .prefetch_related("responsibles")
        .first()
    )
    if existing:
        if not existing.crm_item_id:
            ensure_crm_for_supply_task(existing)
        else:
            sync_crm_item_for_task(existing)
        return existing

    pool = reading.pool
    manager = resolve_default_manager(pool)
    manager_team = resolve_manager_team(pool)
    admin_team = resolve_admin_team(pool)
    due_at = reading.date + timedelta(days=7)
    due_local = timezone.localtime(due_at) if timezone.is_aware(due_at) else due_at
    start_local = timezone.localtime(reading.date) if timezone.is_aware(reading.date) else reading.date

    task = ServiceTask.objects.create(
        organization=pool.organization,
        title=_supply_title(reading),
        description=_supply_description(reading),
        start_date=start_local.date(),
        end_date=due_local.date(),
        task_type=ServiceTask.TYPE_SUPPLY_REQUEST,
        source_type=_reading_source_type(reading),
        status=ServiceTask.STATUS_NEW,
        visibility=ServiceTask.VISIBILITY_PRIVATE,
        priority=ServiceTask.PRIORITY_NORMAL,
        pool=pool,
        client=pool.client,
        water_reading=reading,
        created_by=reading.added_by,
        primary_responsible=manager,
        auto_created=True,
        is_editable=True,
        due_at=due_at,
        payload_json={
            "required_materials": materials,
            "reading_uuid": str(reading.uuid),
        },
    )

    responsibles = []
    if reading.added_by_id and reading.added_by and reading.added_by.is_active:
        responsibles.append(reading.added_by)
    manager_ids = {user.id for user in responsibles}
    if task.source_type == ServiceTask.SOURCE_POOL_STAFF:
        for team_member in manager_team:
            if team_member and team_member.is_active and team_member.id not in manager_ids:
                responsibles.append(team_member)
                manager_ids.add(team_member.id)
    elif manager and manager.is_active and manager.id not in manager_ids:
        responsibles.append(manager)
        manager_ids.add(manager.id)
    if responsibles:
        task.responsibles.set(responsibles)
        notify_task_assignment(task, responsibles, added_by=reading.added_by)
    admin_recipients = [
        user
        for user in admin_team
        if user
        and user.is_active
        and user.id != reading.added_by_id
        and user.id not in {member.id for member in responsibles}
    ]
    if admin_recipients:
        notify_users(
            admin_recipients,
            title="Новая заявка",
            message=f'Заявка {(task.client.name if task.client_id and task.client else task.pool.address)}: {materials}',
            kind="task_assignment",
            level="info",
            action_url=f"/tasks/{task.id}/",
            organization=task.organization,
            send_push=True,
            send_in_app=True,
        )

    ServiceTaskChange.objects.create(
        task=task,
        changed_by=reading.added_by,
        action=ServiceTaskChange.ACTION_CREATED,
        new_value=task.title,
    )

    ensure_crm_for_supply_task(task)
    return task
