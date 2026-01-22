import calendar
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.urls import reverse

from pool_service.models import OrganizationAccess, Pool, WaterReading
from pool_service.services.notifications import notify_client_users, notify_org_users


class Command(BaseCommand):
    help = "Generate scheduled notifications (missed visits, daily readings)."

    def handle(self, *args, **options):
        now = timezone.localtime()
        today = now.date()

        self._generate_missed_visits(now, today)
        self._generate_daily_missing(today)

    def _is_friday_noon(self, now):
        return now.weekday() == 4 and (now.hour, now.minute) >= (12, 0)

    def _map_interval_to_frequency(self, interval):
        if not interval:
            return None
        try:
            value = int(interval)
        except (TypeError, ValueError):
            return None
        if value <= 7:
            return Pool.SERVICE_FREQ_WEEKLY
        if value <= 15:
            return Pool.SERVICE_FREQ_TWICE_MONTHLY
        if value <= 31:
            return Pool.SERVICE_FREQ_MONTHLY
        if value <= 62:
            return Pool.SERVICE_FREQ_BIMONTHLY
        if value <= 93:
            return Pool.SERVICE_FREQ_QUARTERLY
        if value <= 186:
            return Pool.SERVICE_FREQ_TWICE_YEARLY
        return Pool.SERVICE_FREQ_YEARLY

    def _period_bounds(self, today, frequency):
        if frequency == Pool.SERVICE_FREQ_WEEKLY:
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            iso = start.isocalendar()
            return start, end, f"{iso.year}-W{iso.week:02d}"
        if frequency == Pool.SERVICE_FREQ_TWICE_MONTHLY:
            if today.day <= 15:
                start = today.replace(day=1)
                end = today.replace(day=15)
                return start, end, f"{today.year}-{today.month:02d}-H1"
            start = today.replace(day=16)
            last_day = calendar.monthrange(today.year, today.month)[1]
            end = today.replace(day=last_day)
            return start, end, f"{today.year}-{today.month:02d}-H2"
        if frequency == Pool.SERVICE_FREQ_MONTHLY:
            start = today.replace(day=1)
            last_day = calendar.monthrange(today.year, today.month)[1]
            end = today.replace(day=last_day)
            return start, end, f"{today.year}-{today.month:02d}"
        if frequency == Pool.SERVICE_FREQ_BIMONTHLY:
            start_month = ((today.month - 1) // 2) * 2 + 1
            end_month = start_month + 1
            start = date(today.year, start_month, 1)
            last_day = calendar.monthrange(today.year, end_month)[1]
            end = date(today.year, end_month, last_day)
            return start, end, f"{today.year}-B{start_month:02d}"
        if frequency == Pool.SERVICE_FREQ_QUARTERLY:
            quarter = ((today.month - 1) // 3) + 1
            start_month = 1 + (quarter - 1) * 3
            end_month = start_month + 2
            start = date(today.year, start_month, 1)
            last_day = calendar.monthrange(today.year, end_month)[1]
            end = date(today.year, end_month, last_day)
            return start, end, f"{today.year}-Q{quarter}"
        if frequency == Pool.SERVICE_FREQ_TWICE_YEARLY:
            if today.month <= 6:
                start = date(today.year, 1, 1)
                end = date(today.year, 6, 30)
                return start, end, f"{today.year}-H1"
            start = date(today.year, 7, 1)
            end = date(today.year, 12, 31)
            return start, end, f"{today.year}-H2"
        if frequency == Pool.SERVICE_FREQ_YEARLY:
            start = date(today.year, 1, 1)
            end = date(today.year, 12, 31)
            return start, end, f"{today.year}"
        return None, None, None

    def _period_label(self, frequency):
        labels = {
            Pool.SERVICE_FREQ_WEEKLY: "\u0437\u0430 \u044d\u0442\u0443 \u043d\u0435\u0434\u0435\u043b\u044e",
            Pool.SERVICE_FREQ_TWICE_MONTHLY: "\u0437\u0430 \u0442\u0435\u043a\u0443\u0449\u0443\u044e \u043f\u043e\u043b\u043e\u0432\u0438\u043d\u0443 \u043c\u0435\u0441\u044f\u0446\u0430",
            Pool.SERVICE_FREQ_MONTHLY: "\u0437\u0430 \u044d\u0442\u043e\u0442 \u043c\u0435\u0441\u044f\u0446",
            Pool.SERVICE_FREQ_BIMONTHLY: "\u0437\u0430 \u0442\u0435\u043a\u0443\u0449\u0438\u0435 2 \u043c\u0435\u0441\u044f\u0446\u0430",
            Pool.SERVICE_FREQ_QUARTERLY: "\u0437\u0430 \u0442\u0435\u043a\u0443\u0449\u0438\u0439 \u043a\u0432\u0430\u0440\u0442\u0430\u043b",
            Pool.SERVICE_FREQ_TWICE_YEARLY: "\u0437\u0430 \u0442\u0435\u043a\u0443\u0449\u0435\u0435 \u043f\u043e\u043b\u0443\u0433\u043e\u0434\u0438\u0435",
            Pool.SERVICE_FREQ_YEARLY: "\u0437\u0430 \u044d\u0442\u043e\u0442 \u0433\u043e\u0434",
        }
        return labels.get(frequency, "")

    def _generate_missed_visits(self, now, today):
        if not self._is_friday_noon(now):
            return
        pools = Pool.objects.filter(
            organization__isnull=False,
        ).select_related("organization")

        for pool in pools:
            if pool.service_suspended:
                continue
            if not pool.organization.notify_missed_visits:
                continue
            frequency = pool.service_frequency or self._map_interval_to_frequency(pool.service_interval_days)
            if not frequency:
                continue
            org_user_ids = list(
                OrganizationAccess.objects.filter(organization=pool.organization).values_list("user_id", flat=True)
            )
            if not org_user_ids:
                continue
            start_date, end_date, period_key = self._period_bounds(today, frequency)
            if not period_key:
                continue
            has_service = WaterReading.objects.filter(
                pool=pool,
                added_by_id__in=org_user_ids,
                date__date__gte=start_date,
                date__date__lte=end_date,
            ).exists()
            if has_service:
                continue

            label = self._period_label(frequency)
            title = "\u041d\u0435\u0442 \u0441\u0435\u0440\u0432\u0438\u0441\u043d\u043e\u0433\u043e \u043f\u043e\u0441\u0435\u0449\u0435\u043d\u0438\u044f"
            message = f"{pool.address}: \u043d\u0435\u0442 \u043f\u043e\u0441\u0435\u0449\u0435\u043d\u0438\u044f {label}".strip()
            action_url = reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})
            dedupe_key = f"missed_visit:{pool.id}:{period_key}"

            notify_org_users(
                pool.organization,
                title=title,
                message=message,
                kind="missed_visit",
                level="warning",
                action_url=action_url,
                pool=pool,
                dedupe_key=dedupe_key,
            )

    def _generate_daily_missing(self, today):
        pools = Pool.objects.filter(daily_readings_required=True, service_suspended=False).select_related(
            "client",
            "organization",
        )
        for pool in pools:
            if pool.organization and not pool.organization.notify_pool_staff_daily:
                continue
            has_reading = WaterReading.objects.filter(pool=pool, date__date=today).exists()
            if has_reading:
                continue
            title = "\u041d\u0435\u0442 \u0435\u0436\u0435\u0434\u043d\u0435\u0432\u043d\u044b\u0445 \u043f\u043e\u043a\u0430\u0437\u0430\u043d\u0438\u0439"
            message = f"{pool.address}: \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u043d\u0435\u0442 \u043f\u043e\u043a\u0430\u0437\u0430\u043d\u0438\u0439"
            action_url = reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})
            dedupe_key = f"daily_missing:{pool.id}:{today.isoformat()}"

            notify_client_users(
                pool.client,
                title=title,
                message=message,
                kind="daily_missing",
                level="warning",
                action_url=action_url,
                pool=pool,
                dedupe_key=dedupe_key,
            )
