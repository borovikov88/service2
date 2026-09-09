"""Bounded cron tick for daily and manually queued 1C finance jobs."""
import json
import signal

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from pool_service.finance_imports.odata_daily_sync import check_configuration, worker_tick
from pool_service.finance_imports.odata_profit import ODataPreviewError


class WorkerDeadline(BaseException):
    pass


class Command(BaseCommand):
    help = "Обновление ФОТ, валовой прибыли и ДДС: tick для cron; --check только проверяет настройки."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--max-steps", type=int, default=4)
        parser.add_argument("--max-seconds", type=int, default=240)

    def handle(self, *args, **options):
        previous = None
        armed = False
        try:
            if options["check"]:
                config = check_configuration()
                self.stdout.write("CONFIG_OK (read-only); daily_enabled=%s; time=%s; zone=%s; months=%s" % (
                    config.enabled, config.at.strftime("%H:%M"), config.zone.key, config.months))
                return
            if not 1 <= options["max_seconds"] <= 240 or not 1 <= options["max_steps"] <= 100:
                raise CommandError("Лимиты: max-seconds 1–240, max-steps 1–100.")
            if not hasattr(signal, "SIGALRM"):
                raise CommandError("Worker требует Linux с ограничением времени SIGALRM.")

            def deadline(_signum, _frame):
                raise WorkerDeadline

            previous = signal.signal(signal.SIGALRM, deadline)
            signal.alarm(options["max_seconds"])
            armed = True
            result = worker_tick(max_steps=options["max_steps"], max_seconds=options["max_seconds"])
            self.stdout.write(json.dumps(result))
            if result["state"] in {"failed", "partial_failed"}:
                raise CommandError("Обновление завершилось с ошибкой; проверьте историю в приложении.")
        except WorkerDeadline:
            # A claimed lease remains until expiry; next cron tick safely resumes it.
            raise CommandError("WORKER_TIME_LIMIT: продолжение после освобождения lease; успех не подтверждён.") from None
        except (ValidationError, ObjectDoesNotExist, PermissionDenied, ODataPreviewError):
            raise CommandError("Проверьте настройки источника, охват ФОТ и действующие права пользователя.") from None
        finally:
            if armed:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
