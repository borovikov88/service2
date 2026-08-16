from pool_service.models import OneCImportBatch
from .foundation_services import (
    confirm_foundation_import,
    create_foundation_preview,
    foundation_confirmation_state,
)
from .payroll_parser import PARSER_VERSION, parse_payroll


def create_payroll_preview(uploaded_file, organization, user):
    return create_foundation_preview(
        uploaded_file, organization, user,
        import_type=OneCImportBatch.TYPE_PAYROLL,
        parser=parse_payroll,
        parser_version=PARSER_VERSION,
    )


def payroll_confirmation_state(batch):
    return foundation_confirmation_state(batch, parser_version=PARSER_VERSION)


def confirm_payroll(batch_id, organization, user, *, audit_context=None):
    return confirm_foundation_import(
        batch_id, organization, user,
        import_type=OneCImportBatch.TYPE_PAYROLL,
        parser=parse_payroll,
        parser_version=PARSER_VERSION,
        audit_context=audit_context,
    )
