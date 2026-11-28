from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0213_mail_attachment_indicator")]

    operations = [
        migrations.AddField(model_name="mailletter", name="body_html", field=models.TextField(blank=True, null=True)),
        migrations.AddField(model_name="mailsignature", name="html", field=models.TextField(blank=True, null=True)),
    ]
