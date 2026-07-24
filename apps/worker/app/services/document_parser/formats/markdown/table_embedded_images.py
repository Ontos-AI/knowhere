"""Extract and rewrite <img> tags embedded inside MinerU HTML tables."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from loguru import logger

from app.services.document_parser.formats.markdown.image_asset import (
    MarkdownImageAsset,
    MarkdownImageAssetRequest,
    build_markdown_image_asset,
    build_markdown_image_name,
)
from app.services.document_parser.formats.markdown.parse_state import MarkdownParseState
from shared.utils.chunk_refs import build_chunk_ref

_IMG_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc\s*=\s*(?P<quote>["'])(?P<src>[^"']+)(?P=quote)""",
    re.IGNORECASE,
)
_IMG_TAG_WITH_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc\s*=\s*(?P<quote>["'])(?P<src>[^"']+)(?P=quote)[^>]*/?\s*>""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TableEmbeddedImagesResult:
    rewritten_html: str
    """HTML with <img src> rewritten to stable images/image-N-* paths."""

    image_assets: list[MarkdownImageAsset]
    """Newly created image assets that should be registered as rows."""

    image_refs: list[str]
    """Chunk refs ([images/...]) for text content_items and table type channel."""


def extract_table_embedded_images(
    *,
    table_html: str,
    parser_state: MarkdownParseState,
    output_dir: str,
    image_dir: str,
    summary_image: bool,
) -> TableEmbeddedImagesResult:
    """Pull <img> assets out of a table, rename them, and rewrite HTML srcs.

    Duplicate perceptual hashes and repeated srcs reuse the first stable path
    without creating an extra image row. Missing files leave the original src.
    """
    srcs = _unique_img_srcs(table_html)
    if not srcs:
        return TableEmbeddedImagesResult(
            rewritten_html=table_html,
            image_assets=[],
            image_refs=[],
        )

    naming_context = _first_cell_text(table_html)
    rewritten_html = table_html
    image_assets: list[MarkdownImageAsset] = []
    image_refs: list[str] = []
    src_to_relative: dict[str, str] = {}

    for src in srcs:
        if src in src_to_relative:
            continue

        image_name = build_markdown_image_name(
            image_count=parser_state.image_count,
            last_context=naming_context,
        )
        image_asset = build_markdown_image_asset(
            MarkdownImageAssetRequest(
                output_dir=output_dir,
                image_dir=image_dir,
                image_path=src,
                image_name=image_name,
                image_count=parser_state.image_count,
                last_context=naming_context,
                image_summary=naming_context or None,
                timestamp=parser_state.timestamp,
                seen_images=parser_state.seen_images,
                summary_image=summary_image,
                row_index=len(parser_state.rows) + len(image_assets),
                rename_on_summary=False,
            )
        )
        if image_asset.discarded_undersized:
            rewritten_html = _strip_img_tags(rewritten_html, src)
            continue
        if image_asset.relative_path is None:
            logger.warning(
                f"Table-embedded image not found, leaving original src: {src}"
            )
            continue

        src_to_relative[src] = image_asset.relative_path
        image_refs.append(build_chunk_ref(image_asset.relative_path))

        if image_asset.should_advance_image_count:
            image_assets.append(image_asset)
            # Advance immediately so the next distinct src gets a new image-N.
            parser_state.image_count += 1
            if (
                image_asset.cache_key is not None
                and image_asset.cache_entry is not None
            ):
                parser_state.seen_images[image_asset.cache_key] = (
                    image_asset.cache_entry
                )

    for old_src, new_relative in src_to_relative.items():
        rewritten_html = _rewrite_img_src(rewritten_html, old_src, new_relative)

    return TableEmbeddedImagesResult(
        rewritten_html=rewritten_html,
        image_assets=image_assets,
        image_refs=image_refs,
    )


def _unique_img_srcs(table_html: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _IMG_SRC_RE.finditer(table_html):
        src = match.group("src").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        ordered.append(src)
    return ordered


def _first_cell_text(table_html: str) -> str:
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    if not isinstance(table, Tag):
        return ""
    for cell in table.find_all(["td", "th"]):
        if not isinstance(cell, Tag):
            continue
        text = cell.get_text(strip=True)
        if text:
            return text
    return ""


def _rewrite_img_src(html: str, old_src: str, new_src: str) -> str:
    pattern = re.compile(
        r"""(<img\b[^>]*\bsrc\s*=\s*)(["'])"""
        + re.escape(old_src)
        + r"""\2""",
        re.IGNORECASE,
    )

    def _replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{new_src}{match.group(2)}"

    return pattern.sub(_replace, html)


def _strip_img_tags(html: str, src: str) -> str:
    def _drop(match: re.Match[str]) -> str:
        if match.group("src") == src:
            return ""
        return match.group(0)

    return _IMG_TAG_WITH_SRC_RE.sub(_drop, html)
