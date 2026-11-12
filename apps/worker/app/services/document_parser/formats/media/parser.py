from __future__ import annotations

import json
from pathlib import Path

from app.services.document_parser.formats.media.extract import extract_source_audio
from app.services.document_parser.formats.media.probe import admit_media_source
from app.services.document_parser.formats.media.segments import segment_transcript
from app.services.document_parser.formats.media.transcriber import (
    FunAsrSpeechTranscriber,
    MediaAudio,
    SpeechTranscriber,
)
from app.services.document_parser.orchestration.format_router import (
    VIDEO_SOURCE_EXTENSIONS,
)
from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import ParsedRow, ParsedRowsBuilder
from app.services.document_parser.support.path_helpers import find_matches_parsing
from app.services.document_parser.tables.dataframe_helpers import process_dup_paths_df

from shared.core.exceptions.domain_exceptions import ValidationException
from shared.services.chunks.path_segments import (
    append_document_path,
    split_escaped_document_path,
)
from shared.utils.text_utils import tokenize2stw_remove

_SOURCE_KIND_ASR = "asr"


def parse_media(
    file_path: str,
    *,
    filename: str,
    output_dir: str,
    relative_root: str,
    job_id: str | None = None,
    transcriber: SpeechTranscriber | None = None,
) -> ParseOutput:
    """Probe, extract mono audio, transcribe, and emit timestamped text rows."""
    info = admit_media_source(file_path)
    audio_path = extract_source_audio(file_path, output_dir)
    resolved_transcriber = transcriber or FunAsrSpeechTranscriber()
    transcript = resolved_transcriber.transcribe(
        MediaAudio(path=audio_path, job_id=job_id),
        diarize=True,
        language_hint=None,
    )
    if not transcript.sentences:
        raise ValidationException(
            user_message="This media file has no recognizable speech.",
            violations=[
                {
                    "field": "file",
                    "description": "speech transcription returned no sentences",
                }
            ],
            internal_message=(
                "media parse produced an empty transcript: "
                f"filename={filename}, audio_path={audio_path}"
            ),
        )
    segments = segment_transcript(transcript.sentences)
    timestamp = get_str_time()
    media_kind = _media_kind_from_filename(filename)
    document_root = str(relative_root or filename).strip() or filename
    rows_builder = ParsedRowsBuilder()
    for index, segment in enumerate(segments):
        path = _segment_path(document_root, index=index, total=len(segments))
        extra_metadata = {
            "time_ranges": [
                {"start_ms": segment.start_ms, "end_ms": segment.end_ms}
            ],
            "speakers": list(segment.speakers),
            "media": {
                "kind": media_kind,
                "duration_ms": info.duration_ms,
            },
            "source_kind": _SOURCE_KIND_ASR,
        }
        tokens = tokenize2stw_remove([segment.text], None)
        rows_builder.append(
            ParsedRow(
                content=segment.text,
                path=path,
                type=find_matches_parsing(segment.text, path),
                keywords="",
                summary="",
                know_id=gen_str_codes(segment.text),
                tokens=tokens[0] if tokens else "",
                addtime=timestamp,
                extra_metadata=json.dumps(extra_metadata, ensure_ascii=False),
            )
        )
    parsed_df = process_dup_paths_df(rows_builder.to_dataframe())
    return ParseOutput(output_dir=output_dir, parsed_df=parsed_df)


def _media_kind_from_filename(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension in VIDEO_SOURCE_EXTENSIONS:
        return "video"
    return "audio"


def _segment_path(document_root: str, *, index: int, total: int) -> str:
    if total <= 1:
        return document_root
    path_parts = split_escaped_document_path(document_root)
    leaf_title = path_parts[-1] if path_parts else document_root
    return append_document_path(document_root, f"{leaf_title} part {index + 1}")
