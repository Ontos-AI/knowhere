from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.services.document_parser.orchestration.parse_input import ParseInput
from app.services.document_parser.orchestration.parse_session import build_parse_session
from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess
from app.services.document_parser.orchestration.route_parse import route_document_parse


@dataclass(frozen=True)
class ParsePipelineResult:
    output_dir: str
    parsed_df: pd.DataFrame | None

    def as_legacy_tuple(self) -> tuple[str, pd.DataFrame | None]:
        return self.output_dir, self.parsed_df


def run_parse_pipeline(parse_input: ParseInput) -> ParsePipelineResult:
    """Run parser session building, format routing, and output postprocessing."""
    session = build_parse_session(parse_input)
    output_dir, parsed_df = route_document_parse(session)
    processed_df = apply_parse_postprocess(output_dir, parsed_df)
    return ParsePipelineResult(output_dir=output_dir, parsed_df=processed_df)
