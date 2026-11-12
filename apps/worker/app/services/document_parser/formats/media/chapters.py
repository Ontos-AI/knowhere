from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger

from app.services.document_parser.formats.media.segments import TranscriptSegment
from app.services.document_parser.support.gevent_map import gevent_map
from app.services.document_parser.support.choice_judge import (
    ChoiceJudge,
    FallbackChoiceJudge,
    GatewayLunaJudge,
    TextLlmChoiceJudge,
)

from shared.core.config import settings
from shared.services.chunks.time_ranges import format_clock_ms
from shared.services.ai.llm_overrides import get_text_client
from shared.services.ai.response_process_service import eval_response

# One chaptering prompt covers at most this much audio; a free-tier file
# (<= 60 min) is one call, longer paid files are chaptered window by window.
CHAPTER_WINDOW_MS = 60 * 60 * 1000
CHAPTER_USAGE_TASK = "parser.media.chapters"
CHAPTER_BOUNDARY_USAGE_TASK = "parser.media.chapter_boundary"
PARTS_USAGE_TASK = "parser.media.parts"
_BOUNDARY_QUESTION_ID = "chapter_boundary"
_CHOICE_MERGE = "merge"
_CHOICE_SEPARATE = "separate"

_SYSTEM_PROMPT = """You split a spoken-audio transcript into chapters for a listener's table of contents.
The transcript is a numbered list of consecutive segments with time ranges.
A chapter is one sustained discussion of a subject that a listener would want to jump to directly.
A chapter should be large enough to include a distinct topic, similar to an entire chapter or section in a book. Try not to leave an individual segment as a chapter unless it really contains rich content of an independent topic.
Start a new chapter only when the conversation turns to a different subject and stays on it.
A question and its answer, a follow-up, an example, a joke, a short aside, a greeting, or a sponsor read stays inside the surrounding chapter; it never forms a chapter of its own.
Return JSON only: an array of objects {"start": <segment number>, "title": <title>}.
Rules:
- The first chapter starts at the first segment number shown.
- "start" values strictly increase and are segment numbers from the list.
- Titles are short noun phrases in the transcript's own language that name the subject.
- Do not summarize, quote, or rewrite the transcript."""

_PARTS_SYSTEM_PROMPT = """You build the top level of a table of contents for a long spoken-audio transcript.
The transcript is already split into numbered chapters, each shown with its time range, title, and summary.
Group consecutive chapters into parts: a part is a broad theme or stage of the conversation that spans several chapters, so a reader can scan the parts to see the overall arc.
Return JSON only: an array of objects {"start": <chapter number>, "title": <title>}.
Rules:
- The first part starts at chapter 0.
- "start" values strictly increase and are chapter numbers from the list.
- Each part covers several consecutive chapters; never make one part per chapter.
- Titles are short noun phrases in the transcript's own language and name the broader theme, not a single chapter."""


@dataclass(frozen=True)
class ChapterDigest:
    title: str
    start_ms: int
    end_ms: int
    summary: str


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
    windows = _windows(segments)
    # Windows are chaptered independently in parallel; edges are joined in order.
    windows_titles = gevent_map(
        lambda window: _chapter_window(segments, window[0], window[1]),
        windows,
        max_concurrent=settings.SUMMARY_LLM_MAX_CONCURRENT,
    )
    resolved_windows_titles = [
        window_titles for window_titles in windows_titles if window_titles is not None
    ]
    if len(resolved_windows_titles) != len(windows):
        return None
    titles: list[str] = []
    previous_window_start = 0
    for (window_start, _window_end), window_titles in zip(
        windows, resolved_windows_titles, strict=True
    ):
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
        f"[{index}] {format_clock_ms(segment.start_ms)}-{format_clock_ms(segment.end_ms)}\n"
        f"{segment.text}"
    )


def part_boundaries(chapters: Sequence[ChapterDigest]) -> list[tuple[int, str]] | None:
    """Group consecutive chapters into parts as (first chapter index, title).

    Returns None when grouping fails, yields fewer than two parts, or puts every
    chapter in its own part, so the outline keeps a single chapter level.
    """
    if not chapters:
        return None
    listing = "\n\n".join(
        f"[{index}] {format_clock_ms(chapter.start_ms)}-{format_clock_ms(chapter.end_ms)} {chapter.title}\n"
        f"{chapter.summary}"
        for index, chapter in enumerate(chapters)
    )
    boundaries = _ask_boundaries(
        system_prompt=_PARTS_SYSTEM_PROMPT,
        user_text=listing,
        usage_task=PARTS_USAGE_TASK,
        start=0,
        end=len(chapters),
        unit="chapters",
    )
    if boundaries is None or len(boundaries) < 2:
        return None
    if max(end - start for _title, start, end in boundary_spans(boundaries, len(chapters))) < 2:
        # Every part holds one chapter: no grouping happened.
        return None
    return boundaries


def _chapter_window(
    segments: Sequence[TranscriptSegment],
    window_start: int,
    window_end: int,
) -> list[str] | None:
    boundaries = _ask_boundaries(
        system_prompt=_SYSTEM_PROMPT,
        user_text=_chapter_text(segments, window_start, window_end),
        usage_task=CHAPTER_USAGE_TASK,
        start=window_start,
        end=window_end,
        unit="segments",
    )
    if boundaries is None:
        return None
    return [
        title
        for title, start, end in boundary_spans(boundaries, window_end)
        for _index in range(start, end)
    ]


def boundary_spans(
    boundaries: Sequence[tuple[int, str]],
    end: int,
) -> list[tuple[str, int, int]]:
    """Expand ordered (start, title) boundaries into (title, start, end) spans."""
    return [
        (title, start, boundaries[position + 1][0] if position + 1 < len(boundaries) else end)
        for position, (start, title) in enumerate(boundaries)
    ]


def _ask_boundaries(
    *,
    system_prompt: str,
    user_text: str,
    usage_task: str,
    start: int,
    end: int,
    unit: str,
) -> list[tuple[int, str]] | None:
    try:
        client, _model = get_text_client()
        raw = client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature=0.0,
            usage_task=usage_task,
        )
        parsed = eval_response(raw)
    except Exception as exc:
        logger.warning("Media {} grouping failed for {} {}-{}: {}", usage_task, unit, start, end - 1, exc)
        return None
    boundaries = _validated_boundaries(parsed, start, end)
    if boundaries is None:
        logger.warning(
            "Media {} returned invalid boundaries for {} {}-{}: {}",
            usage_task,
            unit,
            start,
            end - 1,
            parsed,
        )
    return boundaries


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
