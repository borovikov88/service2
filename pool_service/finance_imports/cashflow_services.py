from pool_service.models import OneCImportBatch
from .cashflow_parser import PARSER_VERSION, parse_cashflow
from .foundation_services import create_foundation_preview, confirm_foundation_import


def create_cashflow_preview(uploaded_file, organization, user):
    return create_foundation_preview(
        uploaded_file, organization, user,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        parser=parse_cashflow,
        parser_version=PARSER_VERSION,
    )


def confirm_cashflow(batch_id, organization, user):
    return confirm_foundation_import(
        batch_id, organization, user,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        parser=parse_cashflow,
        parser_version=PARSER_VERSION,
    )
