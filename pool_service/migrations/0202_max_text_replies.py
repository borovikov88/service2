from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0201_max_communication_channel")]

    operations = [
        migrations.AddField(
            model_name="channelconnection",
            name="max_bot_token_encrypted",
            field=models.TextField(blank=True, default="", editable=False),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="max_reply_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="conversationmessage",
            constraint=models.UniqueConstraint(
                fields=("conversation", "max_reply_id"),
                name="comm_message_max_reply_uniq",
            ),
        ),
    ]
