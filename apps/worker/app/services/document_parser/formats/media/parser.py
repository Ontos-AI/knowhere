from __future__ import annotations

from app.services.document_parser.formats.media.probe import admit_media_source
from app.services.document_parser.orchestration.parse_output import ParseOutput

from shared.core.exceptions.domain_exceptions import ValidationException


def parse_media(
    file_path: str,
    *,
    filename: str,
    output_dir: str,
    relative_root: str,
) -> ParseOutput:
    """Admit a media source. Transcription is added on the P2 parser path."""
    admit_media_source(file_path)
    raise ValidationException(
        user_message="Speech transcription is not enabled for this file type yet.",
        violations=[
            {
                "field": "parse_track",
                "description": "media transcription is not enabled",
            }
        ],
        internal_message=(
            "media parse track admitted an audio source before the transcriber "
            f"exists: filename={filename}, output_dir={output_dir}, "
            f"relative_root={relative_root}"
        ),
    )
