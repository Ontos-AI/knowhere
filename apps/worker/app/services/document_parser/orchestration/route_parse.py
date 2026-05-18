from app.services.document_parser.orchestration.format_router import (
    get_document_parse_adapter,
    resolve_document_format,
)
from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.orchestration.parse_session import ParseSession


def route_document_parse(session: ParseSession) -> ParseOutput:
    """Route a parser session to the correct adapter and return its output."""
    document_format = resolve_document_format(session.file_full_path)
    adapter = get_document_parse_adapter(document_format)
    return adapter.parse(session)
