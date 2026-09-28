import uuid

from django.db import migrations, models
from django.db.models import Q


def populate_connection_public_ids(apps, schema_editor):
    connection_model = apps.get_model("pool_service", "ChannelConnection")
    for connection in connection_model.objects.filter(public_id__isnull=True).iterator():
        connection.public_id = uuid.uuid4()
        connection.save(update_fields=["public_id"])


def mark_existing_outgoing_pending(apps, schema_editor):
    message_model = apps.get_model("pool_service", "ConversationMessage")
    message_model.objects.filter(direction="out").update(delivery_status="pending")


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0110_alter_notification_kind_communicationchannel_and_more")]

    operations = [
        migrations.AddField(
            model_name="channelconnection",
            name="api_token_hash",
            field=models.CharField(blank=True, editable=False, max_length=128),
        ),
        migrations.AddField(
            model_name="channelconnection",
            name="public_id",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(populate_connection_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="channelconnection",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="delivery_error",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="delivery_status",
            field=models.CharField(
                choices=[("received", "Получено"), ("pending", "Ожидает отправки"), ("delivered", "Доставлено"), ("failed", "Ошибка")],
                default="received",
                max_length=16,
            ),
        ),
        migrations.RunPython(mark_existing_outgoing_pending, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="conversationmessage",
            constraint=models.UniqueConstraint(
                condition=~Q(external_id=""),
                fields=("conversation", "external_id"),
                name="comm_message_external_uniq",
            ),
        ),
    ]
