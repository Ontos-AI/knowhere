from __future__ import annotations

from dataclasses import dataclass

from app.services.document_parser.formats.media.transcriber import TranscriptSentence
from app.services.document_parser.formats.text.parser import divide_long_contents

from shared.core.constants import ProcessingConstants

# faster-whisper `vad_filter` default `min_silence_duration_ms`
# (SYSTRAN/faster-whisper). Fun-ASR already emitted sentences with its own
# fsmn-vad (~800ms); this gap groups those sentences into longer segments.
FASTER_WHISPER_MIN_SILENCE_DURATION_MS = 2000


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    speakers: tuple[str, ...]


def segment_transcript(
    sentences: tuple[TranscriptSentence, ...] | list[TranscriptSentence],
) -> list[TranscriptSegment]:
    """Split on a speaker change or silence gap, then bound each group by length."""
    ordered = list(sentences)
    if not ordered:
        return []
    return [
        _segment_from_sentences(group)
        for turn in _split_on_turn_boundaries(ordered)
        for group in _split_on_length(turn)
    ]


def speaker_label(speaker_id: str | None) -> str | None:
    """Map Fun-ASR 0-based `speaker_id` to searchable `S1`/`S2` labels."""
    if speaker_id is None:
        return None
    text = str(speaker_id).strip()
    if not text:
        return None
    if text.isdigit():
        return f"S{int(text) + 1}"
    return f"S{text}"


def _split_on_turn_boundaries(
    sentences: list[TranscriptSentence],
) -> list[list[TranscriptSentence]]:
    groups: list[list[TranscriptSentence]] = [[sentences[0]]]
    for sentence in sentences[1:]:
        previous = groups[-1][-1]
        speaker_changed = speaker_label(sentence.speaker_id) != speaker_label(
            previous.speaker_id
        )
        gap_ms = sentence.start_ms - previous.end_ms
        if speaker_changed or gap_ms >= FASTER_WHISPER_MIN_SILENCE_DURATION_MS:
            groups.append([sentence])
        else:
            groups[-1].append(sentence)
    return groups


def _split_on_length(
    sentences: list[TranscriptSentence],
) -> list[list[TranscriptSentence]]:
    lines = [_sentence_line(sentence) for sentence in sentences]
    sublists, _part_count = divide_long_contents(
        lines,
        max_threshold=int(3 * ProcessingConstants.POSTPROCESS_SUMMARY_LEN),
    )
    groups: list[list[TranscriptSentence]] = []
    offset = 0
    for part in sublists:
        if not part:
            continue
        count = len(part)
        groups.append(sentences[offset : offset + count])
        offset += count
    return groups


def _segment_from_sentences(sentences: list[TranscriptSentence]) -> TranscriptSegment:
    speakers: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        label = speaker_label(sentence.speaker_id)
        if label and label not in seen:
            seen.add(label)
            speakers.append(label)
    return TranscriptSegment(
        start_ms=sentences[0].start_ms,
        end_ms=sentences[-1].end_ms,
        text="\n".join(_sentence_line(sentence) for sentence in sentences),
        speakers=tuple(speakers),
    )


def _sentence_line(sentence: TranscriptSentence) -> str:
    label = speaker_label(sentence.speaker_id)
    if label:
        return f"{label}: {sentence.text}"
    return sentence.text
