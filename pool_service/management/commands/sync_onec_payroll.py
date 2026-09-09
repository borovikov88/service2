"""One-month payroll accrual sync; suitable for explicitly configured scheduling."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from pool_service.models import Organization
from pool_service.finance_imports.odata_payroll_drafts import create_odata_payroll_draft, confirm_odata_payroll
from pool_service.finance_imports.services import DuplicateImportError


class Command(BaseCommand):
    help = "Получить начисления ФОТ за месяц; по умолчанию только предпросмотр."

    def add_arguments(self, parser):
        parser.add_argument("--organization", type=int, required=True)
        parser.add_argument("--user", type=int, required=True)
        parser.add_argument("--month", required=True)
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--accept-coverage", action="store_true")

    def handle(self, *args, **options):
        if options["confirm"] and not options["accept_coverage"]:
            raise CommandError("Для применения требуется --accept-coverage: подтвердите охват организаций источника.")
        try:
            org = Organization.objects.get(pk=options["organization"])
            user = get_user_model().objects.get(pk=options["user"], is_active=True)
            try:
                batch = create_odata_payroll_draft(options["month"], org, user)
            except DuplicateImportError as exc:
                batch = exc.batch
            if options["confirm"]:
                batch = confirm_odata_payroll(batch.pk, org, user, confirm_coverage=True)
        except (Organization.DoesNotExist, get_user_model().DoesNotExist):
            raise CommandError("Организация или действующий пользователь не найдены.") from None
        except (ValidationError, PermissionDenied) as exc:
            message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Недостаточно прав для импорта ФОТ."
            raise CommandError(message) from None
        self.stdout.write(f"batch={batch.pk} status={batch.status} month={options['month']}")
