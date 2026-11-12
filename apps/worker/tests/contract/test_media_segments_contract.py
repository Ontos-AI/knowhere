from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.formats.media.segments import (
    FASTER_WHISPER_MIN_SILENCE_DURATION_MS,
    segment_transcript,
    speaker_label,
)
from app.services.document_parser.formats.media.transcriber import TranscriptSentence

from shared.core.constants import ProcessingConstants


def _sentence(
    start_ms: int,
    end_ms: int,
    text: str,
    speaker_id: str | None,
) -> TranscriptSentence:
    return TranscriptSentence(
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        speaker_id=speaker_id,
    )


def test_speaker_label_maps_fun_asr_zero_based_ids() -> None:
    assert speaker_label("0") == "S1"
    assert speaker_label("1") == "S2"
    assert speaker_label(None) is None


def test_multi_speaker_splits_on_speaker_change_and_silence_gap() -> None:
    gap = FASTER_WHISPER_MIN_SILENCE_DURATION_MS
    segments = segment_transcript(
        [
            _sentence(0, 100, "hello from one", "0"),
            _sentence(150, 300, "hi from two", "1"),
            _sentence(300 + gap, 2600, "two after a pause", "1"),
            _sentence(2650, 2800, "one again", "0"),
        ]
    )
    assert [segment.speakers for segment in segments] == [
        ("S1",),
        ("S2",),
        ("S2",),
        ("S1",),
    ]
    assert [segment.text for segment in segments] == [
        "S1: hello from one",
        "S2: hi from two",
        "S2: two after a pause",
        "S1: one again",
    ]


def test_multi_speaker_long_turn_is_bounded_by_long_leaf_length() -> None:
    max_threshold = int(3 * ProcessingConstants.POSTPROCESS_SUMMARY_LEN)
    first = "A" * (max_threshold - 10)
    second = "B" * ProcessingConstants.MIN_THRESHOLD
    segments = segment_transcript(
        [
            _sentence(0, 100, "intro from one", "0"),
            _sentence(150, 200, first, "1"),
            _sentence(250, 300, second, "1"),
        ]
    )
    assert [segment.text for segment in segments] == [
        "S1: intro from one",
        f"S2: {first}",
        f"S2: {second}",
    ]
    assert [(segment.start_ms, segment.end_ms) for segment in segments] == [
        (0, 100),
        (150, 200),
        (250, 300),
    ]


def test_single_speaker_splits_on_silence_gap() -> None:
    gap = FASTER_WHISPER_MIN_SILENCE_DURATION_MS
    segments = segment_transcript(
        [
            _sentence(0, 100, "first", "0"),
            _sentence(100 + gap, 300, "second", "0"),
        ]
    )
    assert len(segments) == 2
    assert segments[0].text == "S1: first"
    assert segments[1].text == "S1: second"
    assert segments[0].start_ms == 0
    assert segments[1].start_ms == 100 + gap


def test_single_speaker_below_silence_gap_stays_one_segment() -> None:
    segments = segment_transcript(
        [
            _sentence(0, 100, "first", "0"),
            _sentence(100 + FASTER_WHISPER_MIN_SILENCE_DURATION_MS - 1, 300, "second", "0"),
        ]
    )
    assert len(segments) == 1
    assert segments[0].text == "S1: first\nS1: second"


def test_single_speaker_without_silence_uses_markdown_long_leaf_length() -> None:
    max_threshold = int(3 * ProcessingConstants.POSTPROCESS_SUMMARY_LEN)
    first = "A" * (max_threshold - 10)
    second = "B" * ProcessingConstants.MIN_THRESHOLD
    segments = segment_transcript(
        [
            _sentence(0, 100, first, "0"),
            _sentence(150, 200, second, "0"),
        ]
    )
    assert len(segments) == 2
    assert segments[0].text == f"S1: {first}"
    assert segments[1].text == f"S1: {second}"


def test_unlabeled_sentences_split_on_silence_gap() -> None:
    gap = FASTER_WHISPER_MIN_SILENCE_DURATION_MS
    segments = segment_transcript(
        [
            _sentence(0, 80, "cold open", None),
            _sentence(80 + gap, 200, "after pause", None),
        ]
    )
    assert len(segments) == 2
    assert segments[0].text == "cold open"
    assert segments[1].text == "after pause"
    assert segments[0].speakers == ()
