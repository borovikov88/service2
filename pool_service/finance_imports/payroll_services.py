from pool_service.models import OneCImportBatch
from .foundation_services import create_foundation_preview, confirm_foundation_import
from .payroll_parser import PARSER_VERSION, parse_payroll


def create_payroll_preview(uploaded_file, organization, user):
    return create_foundation_preview(
        uploaded_file, organization, user,
        import_type=OneCImportBatch.TYPE_PAYROLL,
        parser=parse_payroll,
        parser_version=PARSER_VERSION,
    )


def confirm_payroll(batch_id, organization, user):
    return confirm_foundation_import(
        batch_id, organization, user,
        import_type=OneCImportBatch.TYPE_PAYROLL,
        parser=parse_payroll,
        parser_version=PARSER_VERSION,
    )
