"""PDF shard splitting: bin-packing + physical split."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pymupdf
from loguru import logger

if TYPE_CHECKING:
    from app.services.document_agent.manifest import Shard


@dataclass
class MergedShard:
    """A contiguous page range within MinerU's per-request page limit."""

    shard_index: int
    page_start: int  # 1-based inclusive
    page_end: int  # 1-based inclusive

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1

    @property
    def page_offset(self) -> int:
        """Offset to add to MinerU's 0-based page_idx to get original page_idx."""
        return self.page_start - 1


def bin_pack_shards(
    agent_shards: list["Shard"],
    max_pages: int,
) -> list[MergedShard]:
    """1:1 mapping: each agent shard becomes its own MinerU shard.

    Agent shards are cut at semantic boundaries (H1/H2) by the document
    agent.  Merging them would cross those boundaries and degrade heading
    prediction quality, so we preserve them as-is.
    """
    return [
        MergedShard(idx, page_start=s.page_start, page_end=s.page_end)
        for idx, s in enumerate(agent_shards)
    ]


def split_pdf(
    pdf_path: str,
    shards: list[MergedShard],
    work_dir: str,
    exclude_pages: set[int] | None = None,
) -> tuple[list[str], dict[int, int] | None]:
    """Physically split PDF into sub-PDFs using PyMuPDF.

    Args:
        pdf_path: Path to the source PDF.
        shards: Merged shard ranges to extract.
        work_dir: Directory for temporary shard PDFs.
        exclude_pages: Optional set of 1-based page numbers to strip
            (e.g. TOC pages detected by DOC_AGENT).

    Returns:
        (shard_paths, page_remap)
        - shard_paths: one temp PDF path per shard.
        - page_remap: when pages are excluded, maps each shard's local
          0-based page index to the original 1-based page number.
          ``None`` when no pages are excluded.
    """
    doc = pymupdf.open(pdf_path)
    paths: list[str] = []
    page_remap: dict[int, int] | None = None

    if exclude_pages:
        page_remap = {}
        logger.info(
            f"📌 Excluding {len(exclude_pages)} pages from PDF: "
            f"{sorted(exclude_pages)}"
        )

    try:
        global_new_idx = 0  # running counter across all shards
        for shard in shards:
            sub_doc = pymupdf.open()
            shard_included = 0
            for page_num in range(shard.page_start, shard.page_end + 1):
                if exclude_pages and page_num in exclude_pages:
                    continue
                sub_doc.insert_pdf(
                    doc,
                    from_page=page_num - 1,
                    to_page=page_num - 1,
                )
                if page_remap is not None:
                    page_remap[global_new_idx] = page_num
                global_new_idx += 1
                shard_included += 1

            shard_path = os.path.join(work_dir, f"shard_{shard.shard_index}.pdf")
            if shard_included > 0:
                sub_doc.save(shard_path)
                paths.append(shard_path)
            else:
                logger.warning(
                    f"  ⚠️ shard_{shard.shard_index}: all pages excluded, skipping"
                )
            sub_doc.close()

            excluded_in_shard = shard.page_count - shard_included
            logger.info(
                f"  ✂️ shard_{shard.shard_index}: "
                f"pages {shard.page_start}-{shard.page_end} "
                f"({shard_included} included"
                f"{f', {excluded_in_shard} excluded' if excluded_in_shard else ''})"
            )
    finally:
        doc.close()
    return paths, page_remap
