"""PDF shard splitting: physical split of planned page ranges."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace

import pymupdf
from loguru import logger

_SOURCE_OBJECT_OUT_OF_RANGE = "source object number out of range"
_REWRITTEN_SOURCE_NAME = "_rewritten_source.pdf"


@dataclass
class MergedShard:
    """A contiguous page range to extract into its own PDF."""

    shard_index: int
    page_start: int  # 1-based inclusive
    page_end: int  # 1-based inclusive

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1


@dataclass(frozen=True)
class LiveShard:
    """A planned shard that still has pages after TOC exclusion.

    ``shard_index`` is the original plan index, not the compressed position
    among surviving PDFs. Downstream page maps must use this identity.
    """

    shard_index: int
    page_start: int
    page_end: int
    page_numbers: list[int]
    pdf_path: str = ""

    @property
    def is_first_shard(self) -> bool:
        return self.shard_index == 0


def shard_page_numbers(shard: MergedShard, exclude_pages: set[int]) -> list[int]:
    """Original 1-based page numbers physically present in the shard PDF, in order.

    Index ``i`` is the original page of 0-based ``page_idx == i`` for this
    shard, because excluded (e.g. TOC) pages are removed before the split.
    """
    return [
        page_num
        for page_num in range(shard.page_start, shard.page_end + 1)
        if page_num not in exclude_pages
    ]


def select_live_shards(
    shards: Sequence[MergedShard],
    exclude_pages: set[int] | None = None,
) -> list[LiveShard]:
    """Planned shards that still have at least one page after exclusion.

    Order matches document order. Skipped shards are omitted, not padded.
    ``pdf_path`` is empty until ``split_pdf`` writes the file.
    """
    excluded = exclude_pages or set()
    live: list[LiveShard] = []
    for shard in shards:
        page_numbers = shard_page_numbers(shard, excluded)
        if not page_numbers:
            continue
        live.append(
            LiveShard(
                shard_index=shard.shard_index,
                page_start=shard.page_start,
                page_end=shard.page_end,
                page_numbers=page_numbers,
            )
        )
    return live


def _is_source_object_out_of_range(exc: BaseException) -> bool:
    """True only for MuPDF xref copy failure during insert_pdf."""
    return isinstance(exc, RuntimeError) and _SOURCE_OBJECT_OUT_OF_RANGE in str(exc)


def split_pdf(
    pdf_path: str,
    shards: list[MergedShard],
    work_dir: str,
    exclude_pages: set[int] | None = None,
) -> list[LiveShard]:
    """Physically split PDF into sub-PDFs using PyMuPDF.

    Args:
        pdf_path: Path to the source PDF.
        shards: Merged shard ranges to extract.
        work_dir: Directory for temporary shard PDFs.
        exclude_pages: Optional set of 1-based page numbers to strip
            (e.g. TOC pages detected during PROFILE).

    Returns:
        One live shard per planned shard that kept at least one page. Page
        ``i`` of each PDF is original page ``live.page_numbers[i]``.
    """
    doc = pymupdf.open(pdf_path)
    rewritten_path: str | None = None
    try:
        try:
            return _extract_shards(doc, shards, work_dir, exclude_pages)
        except RuntimeError as exc:
            if not _is_source_object_out_of_range(exc):
                raise
            rewritten_path = os.path.join(work_dir, _REWRITTEN_SOURCE_NAME)
            logger.warning(
                "PDF split hit source object number out of range; "
                "rewriting a clean copy and retrying once"
            )
            doc.save(rewritten_path, garbage=4, deflate=True)
    finally:
        doc.close()

    assert rewritten_path is not None
    rewritten = pymupdf.open(rewritten_path)
    try:
        return _extract_shards(rewritten, shards, work_dir, exclude_pages)
    finally:
        rewritten.close()


def _extract_shards(
    doc: pymupdf.Document,
    shards: list[MergedShard],
    work_dir: str,
    exclude_pages: set[int] | None,
) -> list[LiveShard]:
    if exclude_pages:
        logger.info(
            f"📌 Excluding {len(exclude_pages)} pages from PDF: "
            f"{sorted(exclude_pages)}"
        )

    live_plan = select_live_shards(shards, exclude_pages)
    live_indexes = {live.shard_index for live in live_plan}
    for shard in shards:
        if shard.shard_index not in live_indexes:
            logger.warning(
                f"  ⚠️ shard_{shard.shard_index}: all pages excluded, skipping"
            )
            logger.info(
                f"  ✂️ shard_{shard.shard_index}: "
                f"pages {shard.page_start}-{shard.page_end} "
                f"(0 included, {shard.page_count} excluded)"
            )

    live_shards: list[LiveShard] = []
    for live in live_plan:
        sub_doc = pymupdf.open()
        try:
            for page_num in live.page_numbers:
                sub_doc.insert_pdf(
                    doc,
                    from_page=page_num - 1,
                    to_page=page_num - 1,
                )
            shard_path = os.path.join(work_dir, f"shard_{live.shard_index}.pdf")
            sub_doc.save(shard_path)
            live_shards.append(replace(live, pdf_path=shard_path))
        finally:
            sub_doc.close()

        excluded_in_shard = (live.page_end - live.page_start + 1) - len(live.page_numbers)
        logger.info(
            f"  ✂️ shard_{live.shard_index}: "
            f"pages {live.page_start}-{live.page_end} "
            f"({len(live.page_numbers)} included"
            f"{f', {excluded_in_shard} excluded' if excluded_in_shard else ''})"
        )
    return live_shards
