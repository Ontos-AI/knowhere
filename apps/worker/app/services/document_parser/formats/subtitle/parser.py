from __future__ import annotations

from app.services.document_parser.orchestration.parse_output import ParseOutput

from shared.core.exceptions.domain_exceptions import ValidationException


def parse_subtitle(
    file_path: str,
    *,
    filename: str,
    output_dir: str,
    relative_root: str,
) -> ParseOutput:
    """Subtitle cue parsing is added on the P5 parser path."""
    raise ValidationException(
        user_message="Subtitle files cannot be converted to transcript chunks yet.",
        violations=[
            {
                "field": "file_type",
                "description": "subtitle parser is not enabled",
            }
        ],
        internal_message=(
            "subtitle parse track is routed but cue parsing is not enabled: "
            f"file_path={file_path}, filename={filename}, output_dir={output_dir}, "
            f"relative_root={relative_root}"
        ),
    )
