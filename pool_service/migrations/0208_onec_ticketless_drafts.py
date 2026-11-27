from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0207_onec_write_approvals")]

    operations = [
        migrations.AddField(
            model_name="onecwritefence",
            name="is_existing",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="onecwritefence",
            name="author_was_empty",
            field=models.BooleanField(null=True, editable=False),
        ),
        migrations.AddField(
            model_name="onecdraftrequest",
            name="document_snapshot",
            field=models.JSONField(editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="onecdraftrequest",
            name="token_hash",
            field=models.CharField(editable=False, max_length=64, null=True, unique=True),
        ),
    ]
