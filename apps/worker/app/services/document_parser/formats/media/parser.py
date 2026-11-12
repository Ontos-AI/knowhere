from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.document_parser.formats.media.chapters import chapter_titles
from app.services.document_parser.formats.media.extract import extract_source_audio
from app.services.document_parser.formats.media.probe import admit_media_source
from app.services.document_parser.formats.media.segments import (
    TranscriptSegment,
    segment_transcript,
)
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
from shared.services.chunks.path_segments import append_document_path

_SOURCE_KIND_ASR = "asr"
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

    ``chapterize`` groups segments under LLM chapter titles (full mode and
    refine); the progressive baseline keeps every segment at the file root.
    """
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
    media = {"kind": _media_kind_from_filename(filename), "duration_ms": info.duration_ms}
    _write_transcript(output_dir, media=media, sentences=transcript.sentences)
    return ParseOutput(
        output_dir=output_dir,
        parsed_df=_build_rows(
            transcript.sentences,
            media=media,
            filename=filename,
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
            relative_root=relative_root,
            chapterize=True,
        ),
    )


def _build_rows(
    sentences: Sequence[TranscriptSentence],
    *,
    media: dict[str, Any],
    filename: str,
    relative_root: str,
    chapterize: bool,
) -> pd.DataFrame:
    segments = segment_transcript(list(sentences))
    document_root = str(relative_root or filename).strip() or filename
    paths = _segment_paths(document_root, segments, chapterize=chapterize)
    timestamp = get_str_time()
    rows_builder = ParsedRowsBuilder()
    for segment, path in zip(segments, paths, strict=True):
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


def _segment_paths(
    document_root: str,
    segments: Sequence[TranscriptSegment],
    *,
    chapterize: bool,
) -> list[str]:
    titles = chapter_titles(segments) if chapterize else None
    if titles is None:
        return [document_root] * len(segments)
    # Consecutive equal titles are one chapter (also across window edges);
    # a title reused by a later chapter gets the markdown `_N` suffix.
    paths: list[str] = []
    title_counts: dict[str, int] = {}
    previous_title: str | None = None
    current_path = document_root
    for title in titles:
        if title != previous_title:
            title_counts[title] = title_counts.get(title, 0) + 1
            chapter_title = (
                title if title_counts[title] == 1 else f"{title}_{title_counts[title]}"
            )
            current_path = append_document_path(document_root, chapter_title)
            previous_title = title
        paths.append(current_path)
    return paths


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
