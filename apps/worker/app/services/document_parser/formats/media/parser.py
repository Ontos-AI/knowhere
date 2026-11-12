from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.connect_builder.summary_builder import PARSER_SECTION_SUMMARIES_REL
from app.services.document_parser.formats.media.extract import extract_source_audio
from app.services.document_parser.formats.media.outline import (
    build_media_outline,
    flat_outline,
)
from app.services.document_parser.formats.media.probe import (
    admit_media_source,
    measure_media_duration_ms,
)
from app.services.document_parser.formats.media.segments import segment_transcript
from app.services.document_parser.formats.media.transcriber import (
    FunAsrSpeechTranscriber,
    MediaAudio,
    SpeechTranscriber,
    TranscriptSentence,
)
from app.services.document_parser.orchestration.format_router import (
    VIDEO_SOURCE_EXTENSIONS,
)
from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import ParsedRow, ParsedRowsBuilder

from shared.core.exceptions.domain_exceptions import ValidationException

_SOURCE_KIND_ASR = "asr"
ASR_USAGE_TASK = "parser.media.asr"
# Working copy of the transcript, so a progressive refine never re-runs ASR.
MEDIA_TRANSCRIPT_REL = "media_transcript.json"


def parse_media(
    file_path: str,
    *,
    filename: str,
    output_dir: str,
    relative_root: str,
    chapterize: bool,
    job_id: str | None = None,
    transcriber: SpeechTranscriber | None = None,
) -> ParseOutput:
    """Probe, extract mono audio, transcribe, and emit timestamped text rows.

    ``chapterize`` builds the chaptered outline (full mode). Without it this is
    the progressive baseline: segments stay at the file root and the transcript
    is saved as the refine input, so the refine never re-runs ASR.
    """
    info = admit_media_source(file_path)
    audio_path = extract_source_audio(file_path, output_dir)
    resolved_transcriber = transcriber or FunAsrSpeechTranscriber()
    transcript = resolved_transcriber.transcribe(
        MediaAudio(path=audio_path, job_id=job_id),
        diarize=True,
        language_hint=None,
        usage_task=ASR_USAGE_TASK,
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
    media = {
        "kind": _media_kind_from_filename(filename),
        "duration_ms": measure_media_duration_ms(file_path, info),
    }
    if not chapterize:
        _write_transcript(output_dir, media=media, sentences=transcript.sentences)
    return ParseOutput(
        output_dir=output_dir,
        parsed_df=_build_rows(
            transcript.sentences,
            media=media,
            filename=filename,
            output_dir=output_dir,
            relative_root=relative_root,
            chapterize=chapterize,
        ),
    )


def parse_media_transcript(
    transcript_path: str,
    *,
    filename: str,
    output_dir: str,
    relative_root: str,
) -> ParseOutput:
    """Rebuild chaptered rows from a persisted transcript without ASR."""
    media, sentences = _read_transcript(transcript_path)
    return ParseOutput(
        output_dir=output_dir,
        parsed_df=_build_rows(
            sentences,
            media=media,
            filename=filename,
            output_dir=output_dir,
            relative_root=relative_root,
            chapterize=True,
        ),
    )


def _build_rows(
    sentences: Sequence[TranscriptSentence],
    *,
    media: dict[str, Any],
    filename: str,
    output_dir: str,
    relative_root: str,
    chapterize: bool,
) -> pd.DataFrame:
    segments = segment_transcript(list(sentences))
    document_root = str(relative_root or filename).strip() or filename
    outline = (
        build_media_outline(document_root, segments)
        if chapterize
        else flat_outline(document_root, segments)
    )
    if outline.section_summaries:
        with open(
            os.path.join(output_dir, PARSER_SECTION_SUMMARIES_REL), "w", encoding="utf-8"
        ) as handle:
            json.dump(outline.section_summaries, handle, ensure_ascii=False)
    timestamp = get_str_time()
    rows_builder = ParsedRowsBuilder()
    for segment, path in zip(segments, outline.paths, strict=True):
        extra_metadata = {
            "time_ranges": [
                {"start_ms": segment.start_ms, "end_ms": segment.end_ms}
            ],
            "speakers": list(segment.speakers),
            "media": media,
            "source_kind": _SOURCE_KIND_ASR,
        }
        rows_builder.append(
            ParsedRow(
                content=segment.text,
                path=path,
                type="PTXT",
                know_id=gen_str_codes(
                    f"{segment.start_ms}:{segment.end_ms}:{segment.text}"
                ),
                addtime=timestamp,
                extra_metadata=json.dumps(extra_metadata, ensure_ascii=False),
            )
        )
    return rows_builder.to_dataframe()


def _write_transcript(
    output_dir: str,
    *,
    media: dict[str, Any],
    sentences: Sequence[TranscriptSentence],
) -> None:
    payload = {
        "media": media,
        "sentences": [
            {
                "start_ms": sentence.start_ms,
                "end_ms": sentence.end_ms,
                "text": sentence.text,
                "speaker_id": sentence.speaker_id,
            }
            for sentence in sentences
        ],
    }
    with open(
        os.path.join(output_dir, MEDIA_TRANSCRIPT_REL), "w", encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)


def _read_transcript(
    transcript_path: str,
) -> tuple[dict[str, Any], list[TranscriptSentence]]:
    with open(transcript_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    media = payload["media"]
    sentences = [
        TranscriptSentence(
            start_ms=int(item["start_ms"]),
            end_ms=int(item["end_ms"]),
            text=str(item["text"]),
            speaker_id=item["speaker_id"],
        )
        for item in payload["sentences"]
    ]
    if not isinstance(media, dict) or not sentences:
        raise ValueError(f"Media transcript is empty or malformed: {transcript_path}")
    return media, sentences


def _media_kind_from_filename(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension in VIDEO_SOURCE_EXTENSIONS:
        return "video"
    return "audio"
