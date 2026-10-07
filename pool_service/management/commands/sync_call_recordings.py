from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from pool_service.communication_models import PhoneCall, TelephonyConnection
from pool_service.communication_recordings import download_call_recording
from pool_service.services.call_commitment_control import process_call_commitment_controls
from pool_service.services.megafon_internal_calls import (
    MegafonInternalCallSyncError,
    sync_megafon_internal_calls,
)


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

        internal_checked = 0
        internal_created = 0
        internal_updated = 0
        internal_errors = []
        if call_id is None:
            for telephony in TelephonyConnection.objects.filter(
                is_active=True,
            ).select_related("organization"):
                try:
                    result = sync_megafon_internal_calls(telephony)
                except MegafonInternalCallSyncError as exc:
                    internal_errors.append(
                        f"{telephony.pk}:{str(exc)[:160]}"
                    )
                else:
                    internal_checked += result["checked"]
                    internal_created += result["created"]
                    internal_updated += result["updated"]

        queryset = PhoneCall.objects.filter(
            recording_ref__isnull=False,
        ).exclude(recording_ref="")

        if call_id:
            queryset = queryset.filter(pk=call_id)
        elif not force:
            max_attempts = int(
                getattr(settings, "COMMUNICATION_RECORDING_MAX_ATTEMPTS", 20)
            )
            retryable_failures = (
                Q(recording_error="provider_unavailable")
                | Q(recording_error="recording_storage_error")
                | Q(recording_error__in=[
                    "provider_http_404",
                    "provider_http_408",
                    "provider_http_409",
                    "provider_http_425",
                    "provider_http_429",
                ])
                | Q(recording_error__startswith="provider_http_5")
            )
            queryset = queryset.filter(
                recording_file="",
                recording_attempts__lt=max_attempts,
            ).filter(
                Q(
                    recording_status__in=[
                        PhoneCall.RECORDING_NONE,
                        PhoneCall.RECORDING_PENDING,
                    ]
                )
                | Q(
                    recording_status=PhoneCall.RECORDING_FAILED,
                ) & retryable_failures
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

        control = process_call_commitment_controls()

        self.stdout.write(
            self.style.SUCCESS(
                "MegaFon internal call sync: "
                f"checked={internal_checked} created={internal_created} "
                f"updated={internal_updated} errors={len(internal_errors)}"
            )
        )
        for error in internal_errors:
            self.stderr.write(
                self.style.WARNING(
                    f"MegaFon internal call sync skipped line: {error}"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Call recording sync: checked={len(ids)} saved={saved} "
                f"failed={failed} skipped={skipped}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Call commitment control: "
                f"checked={control['checked']} "
                f"due_reminders={control['due_reminders']} "
                f"escalations={control['escalations']} "
                f"without_deadline={control['without_deadline']}"
            )
        )
