from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0202_max_text_replies")]

    operations = [
        migrations.AddField(
            model_name="conversationmessage",
            name="max_details",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="messageattachment",
            name="max_media_position",
            field=models.PositiveSmallIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="messageattachment",
            name="max_media_kind",
            field=models.CharField(blank=True, default="", editable=False, max_length=8),
        ),
        migrations.AddConstraint(
            model_name="messageattachment",
            constraint=models.UniqueConstraint(
                fields=("message", "max_media_position"),
                name="comm_attachment_max_pos_uniq",
            ),
        ),
    ]
