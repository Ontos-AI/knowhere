from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.connect_builder.summary_builder import (
    SUMMARY_MAX_LEN,
    deterministic_section_summary,
    llm_summarize_scope,
)
from app.services.document_parser.formats.media.chapters import (
    ChapterDigest,
    boundary_spans,
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
    if parts is None:
        _place_chapters(document_root, chapters, digests, paths, section_summaries)
        return MediaOutline(paths=paths, section_summaries=section_summaries)
    part_spans = _unique_titles(boundary_spans(parts, len(chapters)))
    for part_title, first_chapter, end_chapter in part_spans:
        _place_chapters(
            append_document_path(document_root, part_title),
            chapters[first_chapter:end_chapter],
            digests[first_chapter:end_chapter],
            paths,
            section_summaries,
        )
    part_summaries = gevent_map(
        lambda span: _part_summary(span[0], digests[span[1] : span[2]]),
        part_spans,
        max_concurrent=settings.SUMMARY_LLM_MAX_CONCURRENT,
    )
    for (part_title, _first_chapter, _end_chapter), part_summary in zip(
        part_spans, part_summaries, strict=True
    ):
        section_summaries[append_document_path(document_root, part_title)] = part_summary
    return MediaOutline(paths=paths, section_summaries=section_summaries)


def _place_chapters(
    parent_path: str,
    chapters: Sequence[_ChapterSpan],
    digests: Sequence[ChapterDigest],
    paths: list[str],
    section_summaries: dict[str, str],
) -> None:
    for chapter, digest in zip(chapters, digests, strict=True):
        chapter_path = append_document_path(parent_path, chapter.title)
        paths[chapter.start : chapter.end] = [chapter_path] * (chapter.end - chapter.start)
        if digest.summary:
            section_summaries[chapter_path] = digest.summary


def _part_summary(part_title: str, digests: Sequence[ChapterDigest]) -> str:
    """LLM part summary over chapter summaries.

    On failure, use the covers text doc_nav enrichment would write for the part:
    one part left without a summary would make enrichment re-derive every part
    and replace the other parts' LLM summaries.
    """
    return llm_summarize_scope(
        node_name=part_title,
        self_only="",
        child_rows=[(digest.title, digest.summary) for digest in digests],
        max_tokens=SUMMARY_MAX_LEN,
        usage_task=PART_SUMMARY_USAGE_TASK,
    ) or deterministic_section_summary(
        is_top_level=False,
        self_only="",
        child_titles=[digest.title for digest in digests],
    )


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
