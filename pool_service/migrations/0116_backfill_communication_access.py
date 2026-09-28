# Generated for existing Service2 users after communications rollout.

from django.db import migrations


CAPABILITY_FIELDS = (
    "can_view_conversations",
    "can_reply_conversations",
    "can_take_conversation",
    "can_assign_conversation",
    "can_view_all_calls",
    "can_view_own_calls",
    "can_listen_calls",
    "can_manage_channels",
)


def defaults_for_roles(roles):
    defaults = {name: False for name in CAPABILITY_FIELDS}
    if roles & {"owner", "admin"}:
        return {name: True for name in CAPABILITY_FIELDS}
    if "manager" in roles:
        defaults.update(
            can_view_conversations=True,
            can_reply_conversations=True,
            can_take_conversation=True,
            can_view_own_calls=True,
            can_listen_calls=True,
        )
    return defaults


def backfill_communication_access(apps, schema_editor):
    OrganizationAccess = apps.get_model("pool_service", "OrganizationAccess")
    CommunicationAccess = apps.get_model("pool_service", "CommunicationAccess")

    roles_by_user_org = {}
    for organization_id, user_id, role in OrganizationAccess.objects.values_list(
        "organization_id", "user_id", "role"
    ).iterator():
        roles_by_user_org.setdefault((organization_id, user_id), set()).add(role)

    for (organization_id, user_id), roles in roles_by_user_org.items():
        # Do not overwrite an explicit access row. This migration only fills the
        # gap for users whose organization roles existed before communications.
        CommunicationAccess.objects.get_or_create(
            organization_id=organization_id,
            user_id=user_id,
            defaults=defaults_for_roles(roles),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0115_communication_review_hardening"),
    ]

    operations = [
        migrations.RunPython(
            backfill_communication_access,
            migrations.RunPython.noop,
        ),
    ]
