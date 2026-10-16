"""PDF shard splitting: 1:1 agent-shard mapping + physical split."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pymupdf
from loguru import logger

if TYPE_CHECKING:
    from app.services.document_agent.manifest import Shard

_SOURCE_OBJECT_OUT_OF_RANGE = "source object number out of range"
_REWRITTEN_SOURCE_NAME = "_rewritten_source.pdf"


@dataclass
class MergedShard:
    """A contiguous page range within MinerU's per-request page limit."""

    shard_index: int
    page_start: int  # 1-based inclusive
    page_end: int  # 1-based inclusive

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1


def map_agent_shards(
    agent_shards: list["Shard"],
) -> list[MergedShard]:
    """1:1 mapping: each agent shard becomes its own MinerU shard.

    Agent shards are cut at TOC hierarchy pack boundaries during PROFILE.
    Merging them would cross those boundaries and degrade heading prediction
    quality, so we preserve them as-is.
    """
    return [
        MergedShard(idx, page_start=s.page_start, page_end=s.page_end)
        for idx, s in enumerate(agent_shards)
    ]


def shard_page_numbers(shard: MergedShard, exclude_pages: set[int]) -> list[int]:
    """Original 1-based page numbers physically present in the shard PDF, in order.

    Index ``i`` is the original page of MinerU's 0-based ``page_idx == i`` for
    this shard, because excluded (e.g. TOC) pages are removed before MinerU runs.
    """
    return [
        page_num
        for page_num in range(shard.page_start, shard.page_end + 1)
        if page_num not in exclude_pages
    ]


def split_pdf_page_numbers(
    shards: list[MergedShard], exclude_pages: set[int]
) -> list[list[int]]:
    """Page map of each shard PDF ``split_pdf`` writes, in the same order.

    A shard whose pages are all excluded produces no PDF, so it has no entry
    here either; index ``i`` therefore lines up with ``split_pdf``'s ``i``-th path.
    """
    page_maps = [shard_page_numbers(shard, exclude_pages) for shard in shards]
    return [page_map for page_map in page_maps if page_map]


def _is_source_object_out_of_range(exc: BaseException) -> bool:
    """True only for MuPDF xref copy failure during insert_pdf."""
    return isinstance(exc, RuntimeError) and _SOURCE_OBJECT_OUT_OF_RANGE in str(exc)


def split_pdf(
    pdf_path: str,
    shards: list[MergedShard],
    work_dir: str,
    exclude_pages: set[int] | None = None,
) -> list[str]:
    """Physically split PDF into sub-PDFs using PyMuPDF.

    Args:
        pdf_path: Path to the source PDF.
        shards: Merged shard ranges to extract.
        work_dir: Directory for temporary shard PDFs.
        exclude_pages: Optional set of 1-based page numbers to strip
            (e.g. TOC pages detected during PROFILE).

    Returns:
        One temp PDF path per shard that kept at least one page. Page ``i`` of
        each PDF maps to the original page ``split_pdf_page_numbers`` reports.
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
) -> list[str]:
    paths: list[str] = []

    if exclude_pages:
        logger.info(
            f"📌 Excluding {len(exclude_pages)} pages from PDF: "
            f"{sorted(exclude_pages)}"
        )

    for shard in shards:
        sub_doc = pymupdf.open()
        try:
            shard_included = 0
            for page_num in shard_page_numbers(shard, exclude_pages or set()):
                sub_doc.insert_pdf(
                    doc,
                    from_page=page_num - 1,
                    to_page=page_num - 1,
                )
                shard_included += 1

            shard_path = os.path.join(work_dir, f"shard_{shard.shard_index}.pdf")
            if shard_included > 0:
                sub_doc.save(shard_path)
                paths.append(shard_path)
            else:
                logger.warning(
                    f"  ⚠️ shard_{shard.shard_index}: all pages excluded, skipping"
                )
        finally:
            sub_doc.close()

        excluded_in_shard = shard.page_count - shard_included
        logger.info(
            f"  ✂️ shard_{shard.shard_index}: "
            f"pages {shard.page_start}-{shard.page_end} "
            f"({shard_included} included"
            f"{f', {excluded_in_shard} excluded' if excluded_in_shard else ''})"
        )
    return paths
