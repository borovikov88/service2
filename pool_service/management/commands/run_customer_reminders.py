"""Deliver due staff reminders from the hosting scheduler."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from pool_service.crm_followup_models import CustomerReminderPolicy
from pool_service.services.customer_reminder_calendar import working_start
from pool_service.services.customer_reminders import notify_due
from pool_service.services.permissions import company_has_access


class Command(BaseCommand):
    help = "Deliver due customer follow-up reminders to authorized staff."

    def add_arguments(self, parser):
        parser.add_argument("--organization", type=int, required=True)

    def handle(self, *args, **options):
        now = timezone.now()
        if working_start(now) != now:
            self.stdout.write(json.dumps({"reminders": "outside_working_hours", "delivered": 0}))
            return
        policy = CustomerReminderPolicy.objects.select_related("organization").filter(
            organization_id=options["organization"],
        ).first()
        if not policy or not company_has_access(policy.organization, now):
            self.stdout.write(json.dumps({"reminders": "disabled", "delivered": 0}))
            return
        try:
            delivered = notify_due(policy, now) if policy.started_at and policy.started_at <= now else 0
        except Exception:
            raise CommandError("customer_reminder_tick_failed") from None
        CustomerReminderPolicy.objects.filter(pk=policy.pk).update(last_tick_at=now)
        self.stdout.write(json.dumps({"reminders": "complete", "delivered": delivered}))
