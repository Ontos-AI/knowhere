from __future__ import annotations

from collections.abc import Sequence

from loguru import logger

from app.services.document_parser.formats.media.segments import TranscriptSegment

from shared.services.ai.llm_overrides import get_text_client
from shared.services.ai.response_process_service import eval_response

# One chaptering prompt covers at most this much audio; a free-tier file
# (<= 60 min) is one call, longer paid files are chaptered window by window.
CHAPTER_WINDOW_MS = 60 * 60 * 1000
CHAPTER_USAGE_TASK = "parser.media.chapters"
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


def chapter_titles(segments: Sequence[TranscriptSegment]) -> list[str] | None:
    """Return one chapter title per segment, or None to keep the transcript flat."""
    if not segments:
        return None
    titles: list[str] = []
    for window_start, window_end in _windows(segments):
        window_titles = _chapter_window(segments, window_start, window_end)
        if window_titles is None:
            return None
        titles.extend(window_titles)
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


def _chapter_window(
    segments: Sequence[TranscriptSegment],
    window_start: int,
    window_end: int,
) -> list[str] | None:
    lines = [
        f"[{index}] {_clock(segments[index].start_ms)}-{_clock(segments[index].end_ms)}\n"
        f"{segments[index].text}"
        for index in range(window_start, window_end)
    ]
    try:
        client, _model = get_text_client()
        raw = client.chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(lines)},
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
