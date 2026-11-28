from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0212_mail_reader_workspace")]

    operations = [
        migrations.AddField(
            model_name="mailletter",
            name="has_attachments",
            field=models.BooleanField(null=True),
        ),
    ]
