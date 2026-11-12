from __future__ import annotations

from collections.abc import Sequence

from loguru import logger

from app.services.document_parser.formats.media.segments import TranscriptSegment
from app.services.document_parser.support.choice_judge import (
    ChoiceJudge,
    FallbackChoiceJudge,
    GatewayLunaJudge,
    TextLlmChoiceJudge,
)

from shared.services.ai.llm_overrides import get_text_client
from shared.services.ai.response_process_service import eval_response

# One chaptering prompt covers at most this much audio; a free-tier file
# (<= 60 min) is one call, longer paid files are chaptered window by window.
CHAPTER_WINDOW_MS = 60 * 60 * 1000
CHAPTER_USAGE_TASK = "parser.media.chapters"
CHAPTER_BOUNDARY_USAGE_TASK = "parser.media.chapter_boundary"
_BOUNDARY_QUESTION_ID = "chapter_boundary"
_CHOICE_MERGE = "merge"
_CHOICE_SEPARATE = "separate"
_MS_PER_SECOND = 1000
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600

_SYSTEM_PROMPT = """You split a spoken-audio transcript into chapters by topic.
The transcript is a numbered list of consecutive segments with time ranges.
Start a new chapter only where the conversation moves to a new topic.
Return JSON only: an array of objects {"start": <segment number>, "title": <title>}.
Rules:
- The first chapter starts at the first segment number shown.
- "start" values strictly increase and are segment numbers from the list.
- Titles are short noun phrases in the transcript's own language.
- Do not summarize, quote, or rewrite the transcript."""


def chapter_titles(
    segments: Sequence[TranscriptSegment],
    *,
    judge: ChoiceJudge | None = None,
) -> list[str] | None:
    """Return one chapter title per segment, or None to keep the transcript flat.

    At each window edge, the previous window's last chapter and the next
    window's first chapter are judged merge-or-separate (Luna, then the text
    LLM); with no answer they stay separate.
    """
    if not segments:
        return None
    resolved_judge = judge or FallbackChoiceJudge(
        (
            GatewayLunaJudge(),
            TextLlmChoiceJudge(usage_task=CHAPTER_BOUNDARY_USAGE_TASK),
        )
    )
    titles: list[str] = []
    previous_window_start = 0
    for window_start, window_end in _windows(segments):
        window_titles = _chapter_window(segments, window_start, window_end)
        if window_titles is None:
            return None
        if titles:
            window_titles = _join_window_edge(
                segments,
                titles=titles,
                window_titles=window_titles,
                previous_window_start=previous_window_start,
                window_start=window_start,
                judge=resolved_judge,
            )
        titles.extend(window_titles)
        previous_window_start = window_start
    return titles


def _windows(segments: Sequence[TranscriptSegment]) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    window_start = 0
    for index, segment in enumerate(segments):
        if segment.start_ms - segments[window_start].start_ms >= CHAPTER_WINDOW_MS:
            windows.append((window_start, index))
            window_start = index
    windows.append((window_start, len(segments)))
    return windows


def _join_window_edge(
    segments: Sequence[TranscriptSegment],
    *,
    titles: list[str],
    window_titles: list[str],
    previous_window_start: int,
    window_start: int,
    judge: ChoiceJudge,
) -> list[str]:
    """Relabel the next window's first chapter when it continues the previous one."""
    previous_title = titles[-1]
    next_title = window_titles[0]
    if previous_title == next_title:
        return window_titles
    previous_chapter_start = window_start - 1
    while (
        previous_chapter_start > previous_window_start
        and titles[previous_chapter_start - 1] == previous_title
    ):
        previous_chapter_start -= 1
    next_chapter_length = 1
    while (
        next_chapter_length < len(window_titles)
        and window_titles[next_chapter_length] == next_title
    ):
        next_chapter_length += 1
    state = {
        "previous_chapter": {
            "title": previous_title,
            "transcript": _chapter_text(segments, previous_chapter_start, window_start),
        },
        "next_chapter": {
            "title": next_title,
            "transcript": _chapter_text(
                segments, window_start, window_start + next_chapter_length
            ),
        },
    }
    questions = {
        _BOUNDARY_QUESTION_ID: {
            "type": "choice",
            "instructions": (
                "previous_chapter ends exactly where next_chapter begins; the cut "
                "comes from a fixed processing window, not from the speakers. "
                "Pick merge if next_chapter continues the same topic as "
                "previous_chapter. Pick separate if it starts a new topic."
            ),
            "criteria": {
                _CHOICE_MERGE: "Same topic; next_chapter continues previous_chapter.",
                _CHOICE_SEPARATE: "Different topic; next_chapter starts a new chapter.",
            },
        }
    }
    chosen = judge.choose(
        state=state,
        questions=questions,
        required_ids=(_BOUNDARY_QUESTION_ID,),
        allowed={_BOUNDARY_QUESTION_ID: frozenset({_CHOICE_MERGE, _CHOICE_SEPARATE})},
    )
    choice = chosen.get(_BOUNDARY_QUESTION_ID) if chosen is not None else None
    logger.info(
        "Media chapter window edge at segment {}: {!r} | {!r} -> {}",
        window_start,
        previous_title,
        next_title,
        choice or f"{_CHOICE_SEPARATE} (no judge answer)",
    )
    if choice != _CHOICE_MERGE:
        return window_titles
    return [previous_title] * next_chapter_length + window_titles[next_chapter_length:]


def _chapter_text(
    segments: Sequence[TranscriptSegment],
    start: int,
    end: int,
) -> str:
    return "\n\n".join(_segment_line(segments, index) for index in range(start, end))


def _segment_line(segments: Sequence[TranscriptSegment], index: int) -> str:
    segment = segments[index]
    return (
        f"[{index}] {_clock(segment.start_ms)}-{_clock(segment.end_ms)}\n"
        f"{segment.text}"
    )


def _chapter_window(
    segments: Sequence[TranscriptSegment],
    window_start: int,
    window_end: int,
) -> list[str] | None:
    transcript = _chapter_text(segments, window_start, window_end)
    try:
        client, _model = get_text_client()
        raw = client.chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            usage_task=CHAPTER_USAGE_TASK,
        )
        parsed = eval_response(raw)
    except Exception as exc:
        logger.warning(
            "Media chaptering failed for segments {}-{}: {}",
            window_start,
            window_end - 1,
            exc,
        )
        return None
    boundaries = _validated_boundaries(parsed, window_start, window_end)
    if boundaries is None:
        logger.warning(
            "Media chaptering returned invalid chapters for segments {}-{}: {}",
            window_start,
            window_end - 1,
            parsed,
        )
        return None
    titles: list[str] = []
    for position, (start, title) in enumerate(boundaries):
        end = (
            boundaries[position + 1][0]
            if position + 1 < len(boundaries)
            else window_end
        )
        titles.extend([title] * (end - start))
    return titles


def _validated_boundaries(
    parsed: object,
    window_start: int,
    window_end: int,
) -> list[tuple[int, str]] | None:
    if not isinstance(parsed, list) or not parsed:
        return None
    boundaries: list[tuple[int, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        start = item.get("start")
        title = item.get("title")
        if isinstance(start, bool) or not isinstance(start, int):
            return None
        if not isinstance(title, str) or not title.strip():
            return None
        if not window_start <= start < window_end:
            return None
        if boundaries and start <= boundaries[-1][0]:
            return None
        boundaries.append((start, title.strip()))
    if boundaries[0][0] != window_start:
        return None
    return boundaries


def _clock(ms: int) -> str:
    total_seconds = ms // _MS_PER_SECOND
    hours, remainder = divmod(total_seconds, _SECONDS_PER_HOUR)
    minutes, seconds = divmod(remainder, _SECONDS_PER_MINUTE)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
