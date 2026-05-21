"""shard_splitter — PDF physical splitting utilities.

Provides two public functions:

- ``split_pdf_by_shards()``: write each ``Shard`` as a temporary sub-PDF file.
- ``merge_shard_dataframes()``: merge per-shard DataFrames and correct
  ``page_nums`` offsets so they reflect the original document's page numbers.

Design notes
------------
- Splitting uses PyMuPDF ``doc.select()`` which preserves all page content
  (images, fonts, annotations) but discards cross-shard hyperlinks — acceptable
  for a parsing pipeline.
- Each sub-PDF is written to a temp directory under the job's ``output_dir``
  so it lives on the same filesystem as the rest of the job artifacts.
- ``page_offset`` from ``Shard`` is added to every ``page_nums`` value during
  merge, keeping page references consistent with the source document.
"""

from __future__ import annotations

import os
import gc
from typing import Any

import pandas as pd
from app.services.document_agent.page_map import Shard
from loguru import logger


# ── Split ──────────────────────────────────────────────────────────────────────


def split_pdf_by_shards(
    pdf_path: str,
    shards: list[Shard],
    output_dir: str,
) -> list[dict[str, Any]]:
    """Write each shard as an independent sub-PDF under ``output_dir``.

    Returns a list of dicts::

        [
            {
                "shard_index": 0,
                "shard": Shard(...),
                "sub_pdf_path": "/tmp/.../shard_0_p1-p50.pdf",
                "sub_output_dir": "/tmp/.../shard_0/",
            },
            …
        ]

    Raises ``RuntimeError`` if the source PDF cannot be opened.
    Never raises for individual shard write failures — they are logged and
    skipped (the caller falls back to parsing the full PDF in that case).
    """
    try:
        import pymupdf  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("PyMuPDF (pymupdf) is required for PDF splitting") from exc

    shards_dir = os.path.join(output_dir, "_shards")
    os.makedirs(shards_dir, exist_ok=True)

    try:
        doc = pymupdf.open(pdf_path)
        total_pages = int(doc.page_count)
    except Exception as exc:
        raise RuntimeError(f"Cannot open PDF '{pdf_path}': {exc}") from exc

    results: list[dict[str, Any]] = []

    try:
        for shard_idx, shard in enumerate(shards):
            # Clamp to actual page count
            p_start = max(1, int(shard.page_start))
            p_end = min(total_pages, int(shard.page_end))

            if p_start > total_pages or p_start > p_end:
                logger.warning(
                    f"[shard_splitter] shard {shard_idx} range [{p_start},{p_end}] "
                    f"is out of bounds (total={total_pages}) — skipping"
                )
                continue

            # PyMuPDF uses 0-based indices
            page_indices = list(range(p_start - 1, p_end))

            sub_pdf_name = f"shard_{shard_idx}_p{p_start}-p{p_end}.pdf"
            sub_pdf_path = os.path.join(shards_dir, sub_pdf_name)
            sub_output_dir = os.path.join(shards_dir, f"shard_{shard_idx}")
            os.makedirs(sub_output_dir, exist_ok=True)

            try:
                sub_doc = pymupdf.open()  # empty document
                sub_doc.insert_pdf(doc, from_page=p_start - 1, to_page=p_end - 1)
                sub_doc.save(sub_pdf_path, garbage=4, deflate=True)
                sub_doc.close()

                logger.info(
                    f"[shard_splitter] shard {shard_idx}: pages {p_start}-{p_end} "
                    f"({len(page_indices)} pages) → {sub_pdf_path}"
                )
                results.append(
                    {
                        "shard_index": shard_idx,
                        "shard": shard,
                        "sub_pdf_path": sub_pdf_path,
                        "sub_output_dir": sub_output_dir,
                    }
                )
            except Exception as exc:
                logger.error(
                    f"[shard_splitter] failed to write shard {shard_idx} "
                    f"(pages {p_start}-{p_end}): {exc}"
                )
    finally:
        doc.close()
        gc.collect()

    return results


# ── Merge ──────────────────────────────────────────────────────────────────────


def merge_shard_dataframes(
    shard_dfs: list[tuple[Shard, pd.DataFrame]],
) -> pd.DataFrame:
    """Merge per-shard DataFrames into a single DataFrame.

    For each shard, adds ``shard.page_offset`` to every value in the
    ``page_nums`` column so that page references reflect the original
    document rather than the sub-PDF's local page numbers.

    Args:
        shard_dfs:  Ordered list of ``(Shard, DataFrame)`` pairs.
                    DataFrames must have the columns produced by the parser
                    (``content``, ``path``, ``type``, ``page_nums``, …).

    Returns:
        A single DataFrame with all rows, sorted by ``page_nums`` (ascending).
        Returns an empty DataFrame if ``shard_dfs`` is empty.
    """
    if not shard_dfs:
        return pd.DataFrame()

    adjusted: list[pd.DataFrame] = []

    for shard, df in shard_dfs:
        if df is None or df.empty:
            logger.warning(
                f"[shard_splitter] shard pages {shard.page_start}-{shard.page_end} "
                f"produced an empty DataFrame — skipping"
            )
            continue

        df_copy = df.copy()

        # Correct page_nums: add page_offset to each page number in the list
        if "page_nums" in df_copy.columns:
            offset = int(shard.page_offset)

            def _shift_page_nums(val: Any, off: int = offset) -> Any:
                if isinstance(val, list):
                    return [p + off for p in val if isinstance(p, int)]
                if isinstance(val, str):
                    # Stored as comma-separated string in some paths
                    try:
                        nums = [int(x.strip()) for x in val.split(",") if x.strip()]
                        return ",".join(str(p + off) for p in nums)
                    except ValueError:
                        return val
                return val

            df_copy["page_nums"] = df_copy["page_nums"].apply(_shift_page_nums)

        adjusted.append(df_copy)
        logger.info(
            f"[shard_splitter] merged shard pages "
            f"{shard.page_start}-{shard.page_end} ({len(df_copy)} rows, "
            f"offset={shard.page_offset})"
        )

    if not adjusted:
        return pd.DataFrame()

    merged = pd.concat(adjusted, ignore_index=True)

    # Sort by the first page number of each row's page_nums for document order
    if "page_nums" in merged.columns:
        def _first_page(val: Any) -> int:
            if isinstance(val, list) and val:
                return int(val[0])
            if isinstance(val, str):
                try:
                    parts = [int(x.strip()) for x in val.split(",") if x.strip()]
                    return parts[0] if parts else 0
                except ValueError:
                    return 0
            return 0

        merged["_sort_page"] = merged["page_nums"].apply(_first_page)
        merged = merged.sort_values("_sort_page").drop(columns=["_sort_page"])
        merged = merged.reset_index(drop=True)

    logger.info(f"[shard_splitter] merge complete: {len(merged)} total rows")
    return merged
