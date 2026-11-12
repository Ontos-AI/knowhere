from __future__ import annotations

from dataclasses import dataclass

from app.services.document_parser.formats.media.transcriber import TranscriptSentence
from app.services.document_parser.formats.text.parser import divide_long_contents

from shared.core.constants import ProcessingConstants

# faster-whisper `vad_filter` default `min_silence_duration_ms`
# (SYSTRAN/faster-whisper). Fun-ASR already emitted sentences with its own
# fsmn-vad (~800ms); this gap only groups those sentences when diarization
# found a single speaker (or none).
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
    """Split sentences into chunks: speaker, else silence, else long-leaf length."""
    ordered = list(sentences)
    if not ordered:
        return []
    groups = _split_sentence_groups(ordered)
    return [_segment_from_sentences(group) for group in groups if group]


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


def _split_sentence_groups(
    sentences: list[TranscriptSentence],
) -> list[list[TranscriptSentence]]:
    labeled_speakers = {
        speaker_label(sentence.speaker_id)
        for sentence in sentences
        if speaker_label(sentence.speaker_id)
    }
    if len(labeled_speakers) >= 2:
        return _split_on_speaker_change(sentences)
    silence_groups = _split_on_silence(sentences)
    if len(silence_groups) > 1:
        return silence_groups
    return _split_on_length(sentences)


def _split_on_speaker_change(
    sentences: list[TranscriptSentence],
) -> list[list[TranscriptSentence]]:
    groups: list[list[TranscriptSentence]] = [[sentences[0]]]
    for sentence in sentences[1:]:
        current_label = speaker_label(sentence.speaker_id)
        previous_label = speaker_label(groups[-1][-1].speaker_id)
        if current_label != previous_label:
            groups.append([sentence])
        else:
            groups[-1].append(sentence)
    return groups


def _split_on_silence(
    sentences: list[TranscriptSentence],
) -> list[list[TranscriptSentence]]:
    groups: list[list[TranscriptSentence]] = [[sentences[0]]]
    for sentence in sentences[1:]:
        gap_ms = sentence.start_ms - groups[-1][-1].end_ms
        if gap_ms >= FASTER_WHISPER_MIN_SILENCE_DURATION_MS:
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
    return groups or [sentences]


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
