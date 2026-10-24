from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('pool_service', '0146_avito_services_publication')]
    operations = [migrations.AddField(model_name='avitoschedulerheartbeat', name='checks', field=models.JSONField(default=list))]
