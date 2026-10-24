import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('pool_service', '0143_estimate_document_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='avitoservicesfeed', name='published_photo_limit',
            field=models.PositiveSmallIntegerField(default=30),
        ),
        migrations.AddField(
            model_name='avitoservicesfeed', name='latest_observations',
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name='avitoservicesfeed', name='last_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='AvitoServicesFeedVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision', models.PositiveIntegerField()),
                ('published_revision', models.PositiveIntegerField(default=0)),
                ('action', models.CharField(choices=[
                    ('baseline', 'Снимок прежнего черновика'), ('legacy_published', 'Снимок прежнего файла'),
                    ('import', 'Импорт черновика'), ('edit', 'Изменение черновика'),
                    ('restore', 'Восстановление в новый черновик'), ('publish', 'Публикация файла'),
                    ('verify', 'Проверка статусов Авито'), ('disable', 'Отключение файла'),
                ], max_length=24)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('reason', models.CharField(blank=True, max_length=500)),
                ('snapshot', models.JSONField(default=list)),
                ('changes', models.JSONField(default=list)),
                ('observations', models.JSONField(default=list)),
                ('file_digest', models.CharField(blank=True, max_length=64)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('feed', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='pool_service.avitoservicesfeed')),
                ('source_version', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='pool_service.avitoservicesfeedversion')),
            ],
            options={'ordering': ['-pk'], 'indexes': [models.Index(fields=['feed', 'created_at'], name='avito_feed_history_idx')]},
        ),
    ]
