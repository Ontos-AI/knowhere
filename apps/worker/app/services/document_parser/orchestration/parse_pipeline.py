from __future__ import annotations

from app.services.document_parser.orchestration.parse_input import ParseInput
from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.orchestration.parse_session import build_parse_session
from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess
from app.services.document_parser.orchestration.route_parse import route_document_parse


ParsePipelineResult = ParseOutput


def run_parse_pipeline(parse_input: ParseInput) -> ParsePipelineResult:
    """Run parser session building, format routing, and output postprocessing."""
    session = build_parse_session(parse_input)
    parsed_output = route_document_parse(session)
    processed_df = apply_parse_postprocess(
        parsed_output.output_dir,
        parsed_output.parsed_df,
    )
    return parsed_output.with_dataframe(processed_df)
