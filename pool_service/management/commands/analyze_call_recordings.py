from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from pool_service.communication_models import CallAnalysis, PhoneCall
from pool_service.services.call_ai import process_call_analysis


class Command(BaseCommand):
    help = "Transcribe stored phone-call recordings and extract structured CRM facts."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--call-id", type=int)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
            self.stdout.write(self.style.WARNING("Call analysis skipped: OPENAI_API_KEY is not configured."))
            return

        limit = max(1, min(int(options["limit"]), 100))
        call_id = options.get("call_id")
        force = bool(options.get("force"))
        stale_before = timezone.now() - timedelta(minutes=30)

        queryset = PhoneCall.objects.filter(
            recording_status=PhoneCall.RECORDING_STORED,
        ).exclude(recording_file="")

        if call_id:
            queryset = queryset.filter(pk=call_id)
        elif not force:
            max_attempts = int(getattr(settings, "OPENAI_CALL_MAX_ATTEMPTS", 5))
            queryset = queryset.filter(
                Q(analysis__isnull=True)
                | Q(
                    analysis__attempts__lt=max_attempts,
                    analysis__status__in=[
                        CallAnalysis.STATUS_PENDING,
                        CallAnalysis.STATUS_FAILED,
                    ],
                )
                | Q(
                    analysis__attempts__lt=max_attempts,
                    analysis__status=CallAnalysis.STATUS_PROCESSING,
                    analysis__processing_started_at__lt=stale_before,
                )
            ).exclude(analysis__status=CallAnalysis.STATUS_READY)

        ids = list(queryset.order_by("-started_at", "-pk").values_list("pk", flat=True)[:limit])
        ready = 0
        failed = 0
        skipped = 0
        for pk in ids:
            if process_call_analysis(pk, force=force):
                ready += 1
            else:
                analysis = CallAnalysis.objects.filter(call_id=pk).only("status").first()
                if analysis and analysis.status == CallAnalysis.STATUS_FAILED:
                    failed += 1
                else:
                    skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Call AI: checked={len(ids)} ready={ready} failed={failed} skipped={skipped}"
            )
        )
