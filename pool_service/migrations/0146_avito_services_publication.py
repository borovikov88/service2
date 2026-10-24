import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('pool_service', '0145_avito_services_images'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AlterField(model_name='avitoservicesfeedversion', name='action',
            field=models.CharField(max_length=24, choices=[
                ('baseline', 'Снимок прежнего черновика'), ('legacy_published', 'Снимок прежнего файла'),
                ('import', 'Импорт черновика'), ('edit', 'Изменение черновика'),
                ('restore', 'Восстановление в новый черновик'), ('publish', 'Публикация файла'),
                ('verify', 'Проверка статусов Авито'), ('disable', 'Отключение файла'),
                ('policy', 'Политика возврата объявления'),
            ])),
        migrations.CreateModel(
            name='AvitoServicesPublication',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account_id', models.CharField(max_length=255)),
                ('organization_id', models.PositiveBigIntegerField()),
                ('immutable_source_sha256', models.CharField(max_length=64)),
                ('stable_source_sha256', models.CharField(max_length=64)),
                ('file_sha256', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('version', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                    related_name='publication', to='pool_service.avitoservicesfeedversion')),
            ],
        ),
        migrations.CreateModel(
            name='AvitoServicesPublicationAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('upload_id', models.CharField(max_length=32)),
                ('report_sha256', models.CharField(max_length=64)),
                ('report_started_at', models.DateTimeField(null=True)),
                ('checked_at', models.DateTimeField(auto_now_add=True)),
                ('state', models.CharField(max_length=40)),
                ('upload_status', models.CharField(blank=True, max_length=40)),
                ('source_confirmed', models.BooleanField(default=False)),
                ('content_verified', models.BooleanField(default=False)),
                ('rows', models.JSONField(default=list)),
                ('publication', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='attempts', to='pool_service.avitoservicespublication')),
            ],
            options={'ordering': ['-report_started_at', '-pk'], 'constraints': [
                models.UniqueConstraint(fields=['publication', 'upload_id', 'report_sha256'], name='avito_pub_report_unique')]},
        ),
        migrations.CreateModel(
            name='AvitoServicesListingIntent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('avito_id', models.CharField(max_length=32)),
                ('account_id', models.CharField(max_length=255)),
                ('organization_id', models.PositiveBigIntegerField()),
                ('owner_withdrawn', models.BooleanField(default=False)),
                ('marked_at', models.DateTimeField(auto_now=True)),
                ('feed', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='listing_intents', to='pool_service.avitoservicesfeed')),
                ('marked_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [
                models.UniqueConstraint(fields=['feed', 'avito_id'], name='avito_feed_intent_unique')]},
        ),
    ]
