from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.connect_builder.summary_builder import (
    SUMMARY_MAX_LEN,
    llm_summarize_scope,
)
from app.services.document_parser.formats.media.chapters import (
    ChapterDigest,
    chapter_titles,
    part_boundaries,
)
from app.services.document_parser.formats.media.segments import TranscriptSegment
from app.services.document_parser.support.gevent_map import gevent_map

from shared.core.config import settings

from shared.services.chunks.path_segments import append_document_path

CHAPTER_SUMMARY_USAGE_TASK = "parser.media.chapter_summary"
PART_SUMMARY_USAGE_TASK = "parser.media.part_summary"


@dataclass(frozen=True)
class MediaOutline:
    """Per-segment chunk paths plus section path -> summary for doc_nav."""

    paths: list[str]
    section_summaries: dict[str, str]


@dataclass(frozen=True)
class _ChapterSpan:
    title: str
    start: int
    end: int


def flat_outline(document_root: str, segments: Sequence[TranscriptSegment]) -> MediaOutline:
    return MediaOutline(paths=[document_root] * len(segments), section_summaries={})


def build_media_outline(
    document_root: str,
    segments: Sequence[TranscriptSegment],
) -> MediaOutline:
    """Chapters from segments, then chapter summaries, parts, and part summaries.

    Without chapters the transcript stays flat at the document root; without
    parts the chapters sit directly under the root.
    """
    titles = chapter_titles(segments)
    if titles is None:
        return flat_outline(document_root, segments)
    chapters = _chapter_spans(titles)
    chapter_summaries = gevent_map(
        lambda chapter: llm_summarize_scope(
            node_name=chapter.title,
            self_only="\n".join(
                segment.text for segment in segments[chapter.start : chapter.end]
            ),
            child_rows=[],
            max_tokens=SUMMARY_MAX_LEN,
            usage_task=CHAPTER_SUMMARY_USAGE_TASK,
        ),
        chapters,
        max_concurrent=settings.SUMMARY_LLM_MAX_CONCURRENT,
    )
    digests = [
        ChapterDigest(
            title=chapter.title,
            start_ms=segments[chapter.start].start_ms,
            end_ms=segments[chapter.end - 1].end_ms,
            summary=summary,
        )
        for chapter, summary in zip(chapters, chapter_summaries, strict=True)
    ]
    parts = part_boundaries(digests)
    paths = [document_root] * len(segments)
    section_summaries: dict[str, str] = {}
    part_ranges = (
        _unique_titles(
            [
                (title, start, parts[position + 1][0] if position + 1 < len(parts) else len(chapters))
                for position, (start, title) in enumerate(parts)
            ]
        )
        if parts is not None
        else [("", 0, len(chapters))]
    )
    for part_title, first_chapter, end_chapter in part_ranges:
        parent_path = (
            append_document_path(document_root, part_title) if part_title else document_root
        )
        for chapter, digest in zip(
            chapters[first_chapter:end_chapter],
            digests[first_chapter:end_chapter],
            strict=True,
        ):
            chapter_path = append_document_path(parent_path, chapter.title)
            paths[chapter.start : chapter.end] = [chapter_path] * (chapter.end - chapter.start)
            if digest.summary:
                section_summaries[chapter_path] = digest.summary
    titled_parts = [part for part in part_ranges if part[0]]
    part_summaries = gevent_map(
        lambda part: llm_summarize_scope(
            node_name=part[0],
            self_only="",
            child_rows=[(digest.title, digest.summary) for digest in digests[part[1] : part[2]]],
            max_tokens=SUMMARY_MAX_LEN,
            usage_task=PART_SUMMARY_USAGE_TASK,
        ),
        titled_parts,
        max_concurrent=settings.SUMMARY_LLM_MAX_CONCURRENT,
    )
    for (part_title, _first_chapter, _end_chapter), part_summary in zip(
        titled_parts, part_summaries, strict=True
    ):
        if part_summary:
            section_summaries[append_document_path(document_root, part_title)] = part_summary
    return MediaOutline(paths=paths, section_summaries=section_summaries)


def _chapter_spans(titles: Sequence[str]) -> list[_ChapterSpan]:
    """Collapse per-segment titles into chapters; reused titles take `_N`."""
    spans: list[_ChapterSpan] = []
    start = 0
    for index in range(1, len(titles) + 1):
        if index == len(titles) or titles[index] != titles[start]:
            spans.append(_ChapterSpan(title=titles[start], start=start, end=index))
            start = index
    unique = _unique_titles([(span.title, span.start, span.end) for span in spans])
    return [_ChapterSpan(title=title, start=start, end=end) for title, start, end in unique]


def _unique_titles(
    items: Sequence[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    """Give a reused title the markdown `_N` suffix so sibling paths stay distinct."""
    counts: dict[str, int] = {}
    unique: list[tuple[str, int, int]] = []
    for title, start, end in items:
        counts[title] = counts.get(title, 0) + 1
        unique.append(
            (title if counts[title] == 1 else f"{title}_{counts[title]}", start, end)
        )
    return unique
