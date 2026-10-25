from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('pool_service', '0148_conversation_clients_and_context')]
    operations = [
        migrations.CreateModel(name='AvitoStatisticsRun', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('account_id', models.CharField(max_length=64)),
            ('organization_id_at_collection', models.PositiveBigIntegerField()),
            ('day', models.DateField()),
            ('source', models.CharField(default='avito_stats_v2_items', max_length=64)),
            ('requested_calendar', models.CharField(default='Asia/Barnaul', max_length=64)),
            ('provider_timezone', models.CharField(default='unknown', max_length=64)),
            ('calendar_status', models.CharField(default='unknown', max_length=24)),
            ('binding_digest', models.CharField(max_length=64)),
            ('state', models.CharField(default='collecting', max_length=24)),
            ('pagination_complete', models.BooleanField(default=False)),
            ('expected_total', models.PositiveIntegerField(null=True)),
            ('received_rows', models.PositiveIntegerField(default=0)),
            ('next_offset', models.PositiveIntegerField(default=0)),
            ('page_provenance', models.JSONField(default=list)),
            ('staged_rows', models.JSONField(default=list)),
            ('dimensions', models.JSONField(default=dict)),
            ('error_code', models.CharField(blank=True, max_length=80)),
            ('started_at', models.DateTimeField(auto_now_add=True)),
            ('finished_at', models.DateTimeField(null=True)),
            ('connection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='pool_service.channelconnection')),
        ], options={'indexes': [models.Index(fields=['connection', 'account_id', 'day'], name='avito_stats_run_scope')]}),
        migrations.CreateModel(name='AvitoDailyStatistic', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('account_id', models.CharField(max_length=64)),
            ('organization_id_at_collection', models.PositiveBigIntegerField()),
            ('day', models.DateField()),
            ('avito_id', models.CharField(max_length=64)),
            ('metrics', models.JSONField(default=dict)),
            ('provider_row_present', models.BooleanField(default=True)),
            ('title', models.CharField(blank=True, max_length=300)),
            ('service_label', models.CharField(blank=True, max_length=300)),
            ('service_source', models.CharField(blank=True, max_length=32)),
            ('city_label', models.CharField(blank=True, max_length=200)),
            ('dimension_source', models.CharField(default='published_snapshot_at_collection', max_length=64)),
            ('fetched_at', models.DateTimeField()),
            ('connection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='pool_service.channelconnection')),
            ('source_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='daily_rows', to='pool_service.avitostatisticsrun')),
        ], options={
            'indexes': [models.Index(fields=['connection', 'account_id', 'day'], name='avito_daily_scope_day')],
            'constraints': [models.UniqueConstraint(fields=('connection', 'account_id', 'organization_id_at_collection', 'day', 'avito_id'), name='avito_daily_scope_item')],
        }),
    ]
