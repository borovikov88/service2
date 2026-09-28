# Generated for Service2 communications access backfill on 2026-09-28

from django.db import migrations


_ACCESS_FIELDS = (
    "can_view_conversations",
    "can_reply_conversations",
    "can_take_conversation",
    "can_assign_conversation",
    "can_view_all_calls",
    "can_view_own_calls",
    "can_listen_calls",
    "can_manage_channels",
)


def _defaults_for_roles(roles):
    defaults = {name: False for name in _ACCESS_FIELDS}
    roles = set(roles)
    if roles & {"owner", "admin"}:
        return {name: True for name in _ACCESS_FIELDS}
    if "manager" in roles:
        defaults.update(
            can_view_conversations=True,
            can_reply_conversations=True,
            can_take_conversation=True,
            can_view_own_calls=True,
            can_listen_calls=True,
        )
    return defaults


def backfill_missing_communication_access(apps, schema_editor):
    OrganizationAccess = apps.get_model("pool_service", "OrganizationAccess")
    CommunicationAccess = apps.get_model("pool_service", "CommunicationAccess")

    role_rows = OrganizationAccess.objects.values_list(
        "organization_id", "user_id", "role"
    ).order_by("organization_id", "user_id", "id")

    grouped = {}
    for organization_id, user_id, role in role_rows.iterator():
        grouped.setdefault((organization_id, user_id), set()).add(role)

    existing = set(
        CommunicationAccess.objects.values_list("organization_id", "user_id")
    )
    to_create = []
    for (organization_id, user_id), roles in grouped.items():
        if (organization_id, user_id) in existing:
            continue
        to_create.append(
            CommunicationAccess(
                organization_id=organization_id,
                user_id=user_id,
                **_defaults_for_roles(roles),
            )
        )

    if to_create:
        CommunicationAccess.objects.bulk_create(to_create, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0115_communication_review_hardening"),
    ]

    operations = [
        migrations.RunPython(
            backfill_missing_communication_access,
            migrations.RunPython.noop,
        ),
    ]
