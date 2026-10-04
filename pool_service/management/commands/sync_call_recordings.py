from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from pool_service.communication_models import PhoneCall
from pool_service.communication_recordings import download_call_recording


class Command(BaseCommand):
    help = "Download pending MegaFon call recordings into Service2 private storage."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--call-id", type=int)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 500))
        call_id = options.get("call_id")
        force = bool(options.get("force"))
        stale_before = timezone.now() - timedelta(minutes=30)

        queryset = PhoneCall.objects.filter(
            recording_ref__isnull=False,
        ).exclude(recording_ref="")

        if call_id:
            queryset = queryset.filter(pk=call_id)
        elif not force:
            queryset = queryset.filter(recording_file="").filter(
                Q(
                    recording_status__in=[
                        PhoneCall.RECORDING_NONE,
                        PhoneCall.RECORDING_PENDING,
                        PhoneCall.RECORDING_FAILED,
                    ]
                )
                | Q(
                    recording_status=PhoneCall.RECORDING_DOWNLOADING,
                    recording_last_attempt_at__lt=stale_before,
                )
                | Q(
                    recording_status=PhoneCall.RECORDING_DOWNLOADING,
                    recording_last_attempt_at__isnull=True,
                )
            )

        ids = list(
            queryset.order_by("recording_last_attempt_at", "started_at", "pk")
            .values_list("pk", flat=True)[:limit]
        )

        saved = 0
        failed = 0
        skipped = 0
        for pk in ids:
            if download_call_recording(pk, force=force):
                saved += 1
            else:
                call = PhoneCall.objects.filter(pk=pk).only(
                    "recording_status", "recording_file"
                ).first()
                if call and (
                    call.recording_file
                    or call.recording_status == PhoneCall.RECORDING_STORED
                ):
                    skipped += 1
                else:
                    failed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Call recording sync: checked={len(ids)} saved={saved} "
                f"failed={failed} skipped={skipped}"
            )
        )
