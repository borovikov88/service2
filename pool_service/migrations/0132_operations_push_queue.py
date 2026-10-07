from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


_TERMINAL = frozenset({
    "sent", "skipped_self", "blocked_not_authorized",
    "blocked_push_disabled", "blocked_task_closed",
})


def _pending(delivery):
    return bool(
        isinstance(delivery, dict) and delivery
        and not delivery.get("push_delivered_at")
        and delivery.get("push_delivery_result") not in _TERMINAL
    )


def seed_existing_pending(apps, schema_editor):
    """One-time, primary-key-paged import; no runtime historical JSON sweep."""
    Task = apps.get_model("pool_service", "ServiceTask")
    Queue = apps.get_model("pool_service", "OperationsPushQueue")
    alias = schema_editor.connection.alias
    cursor = 0
    while True:
        rows = list(
            Task.objects.using(alias).filter(pk__gt=cursor)
            .order_by("pk").values_list("pk", "task_type", "payload_json")[:500]
        )
        if not rows:
            break
        entries = []
        for task_id, task_type, payload in rows:
            if task_type != "crm_followup" or not isinstance(payload, dict):
                continue
            assignment_pending = _pending(payload.get("operations_assignment_delivery"))
            deliveries = payload.get("operations_employee_notification_deliveries")
            notification_pending = isinstance(deliveries, dict) and any(
                _pending(value) for value in deliveries.values()
            )
            if assignment_pending or notification_pending:
                entries.append(Queue(task_id=task_id))
        Queue.objects.using(alias).bulk_create(entries, batch_size=500, ignore_conflicts=True)
        cursor = rows[-1][0]


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0131_phonecall_internal_participants")]

    operations = [
        migrations.CreateModel(
            name="OperationsPushQueue",
            fields=[
                ("task", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    primary_key=True, serialize=False,
                    related_name="operations_push_queue_entry",
                    to="pool_service.servicetask",
                )),
                ("next_attempt_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_marker", models.CharField(blank=True, default="", max_length=160)),
            ],
            options={"indexes": [models.Index(fields=["next_attempt_at", "task"], name="ops_push_due_task_idx")]},
        ),
        migrations.RunPython(seed_existing_pending, migrations.RunPython.noop),
    ]
