import django.db.models.deletion
import pool_service.avito_media_storage
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('pool_service', '0144_avito_services_feed_versions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AvitoServicesImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account_id', models.CharField(max_length=255)),
                ('organization_id_at_upload', models.PositiveBigIntegerField()),
                ('public_key', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('public_url', models.CharField(max_length=2000)),
                ('file', models.FileField(max_length=255, storage=pool_service.avito_media_storage.AvitoImageStorage(), upload_to='')),
                ('sha256', models.CharField(max_length=64)),
                ('byte_size', models.PositiveIntegerField()),
                ('width', models.PositiveIntegerField()),
                ('height', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('published_in_feed_at', models.DateTimeField(blank=True, null=True)),
                ('connection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='pool_service.channelconnection')),
                ('uploaded_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [models.UniqueConstraint(
                fields=('connection', 'account_id', 'organization_id_at_upload', 'sha256'),
                name='avito_image_scope_digest_uniq')]},
        ),
    ]
