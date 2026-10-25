"""Account-bound provider-calendar history; never a separate CRM or finance ledger."""
from django.db import models


class AvitoStatisticsRun(models.Model):
    connection = models.ForeignKey('pool_service.ChannelConnection', on_delete=models.CASCADE)
    account_id = models.CharField(max_length=64)
    organization_id_at_collection = models.PositiveBigIntegerField()
    day = models.DateField()
    source = models.CharField(max_length=64, default='avito_stats_v2_items')
    requested_calendar = models.CharField(max_length=64, default='Asia/Barnaul')
    provider_timezone = models.CharField(max_length=64, default='unknown')
    calendar_status = models.CharField(max_length=24, default='unknown')
    binding_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=24, default='collecting')
    pagination_complete = models.BooleanField(default=False)
    expected_total = models.PositiveIntegerField(null=True)
    received_rows = models.PositiveIntegerField(default=0)
    next_offset = models.PositiveIntegerField(default=0)
    page_provenance = models.JSONField(default=list)
    staged_rows = models.JSONField(default=list)
    dimensions = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=['connection', 'account_id', 'day'], name='avito_stats_run_scope')]


class AvitoDailyStatistic(models.Model):
    connection = models.ForeignKey('pool_service.ChannelConnection', on_delete=models.CASCADE)
    account_id = models.CharField(max_length=64)
    organization_id_at_collection = models.PositiveBigIntegerField()
    day = models.DateField()
    avito_id = models.CharField(max_length=64)
    source_run = models.ForeignKey(AvitoStatisticsRun, on_delete=models.CASCADE, related_name='daily_rows')
    metrics = models.JSONField(default=dict)
    provider_row_present = models.BooleanField(default=True)
    title = models.CharField(max_length=300, blank=True)
    service_label = models.CharField(max_length=300, blank=True)
    service_source = models.CharField(max_length=32, blank=True)
    city_label = models.CharField(max_length=200, blank=True)
    dimension_source = models.CharField(max_length=64, default='published_snapshot_at_collection')
    fetched_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=[
            'connection', 'account_id', 'organization_id_at_collection', 'day', 'avito_id'],
            name='avito_daily_scope_item')]
        indexes = [models.Index(fields=['connection', 'account_id', 'day'], name='avito_daily_scope_day')]
