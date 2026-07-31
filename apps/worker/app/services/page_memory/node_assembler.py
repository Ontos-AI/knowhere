"""Node-granularity assembly for the page-memory track.

Page-track historically emitted one ``type=page`` chunk per physical page,
so a section spanning N pages produced N chunks sharing the same ``path``.
This module switches the unit of assembly to the **leaf section node**:

- One chunk per leaf node (path).  Internal/structural nodes live in the
  navigation tree only (their summaries aggregate bottom-up).
- A page that belongs to multiple nodes is *referenced* by each of them via
  page numbers; retrieval resolves those pages to a cropped PDF on demand.
- A page's body text is stored **once**, under the first leaf (in reading
  order) that covers it.  Other nodes that share the page emit a
  ``SAME-AS <owner path>`` marker instead of repeating the text.  Body text is
  not duplicated across page-track nodes.

Summary/entities follow the same ownership: only owned pages contribute page
tags; SAME-AS aliases keep empty summary/entities and an explicit ``same_as``
connection to the owner chunk.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from loguru import logger

from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import serialize_entities
from app.services.page_memory.page_assets import (
    PageAsset,
    build_asset_rows,
)
from app.services.page_memory.page_tagger import PageTagResult, normalize_entities
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from app.services.page_memory._utils import slice_text_from_anchor
from app.services.page_memory.toc_page_policy import TocPagePolicy
from shared.services.ai.summary.engine import transcribe
from shared.services.chunks.path_segments import join_document_path

SAME_AS_PREFIX = "SAME-AS"

_PAGE_CITATION_ASSET_SOURCE = "knowhere-rendered-page-citation-source"
_PAGE_CITATION_ASSET_CONTENT_TYPE = "image/png"


@dataclass(frozen=True)
class LeafNode:
    """A leaf section node = one node-granularity chunk."""

    section_path: str
    title: str
    level: int
    start_page: int
    end_page: int
    parent_path: str | None = None
    body_pages: tuple[int, ...] | None = None


@dataclass
class NodePageView:
    """Resolved page assignment for a single leaf node."""

    leaf: LeafNode
    pages: list[int] = field(default_factory=list)
    """All body pages covered by this leaf (owned + shared), in order."""

    owned_pages: list[int] = field(default_factory=list)
    """Pages whose body text is stored under this node."""


def identify_leaf_nodes(skeletons: list[SectionSkeleton]) -> list[LeafNode]:
    """Return body-bearing skeleton nodes in reading order.

    A skeleton is included when either:
    - no other skeleton declares it as ``parent_path``; or
    - it is an internal section with pages not covered by descendant sections.

    The second case preserves parent-section body pages, such as the first page
    of a section before the first child heading. Internal nodes remain
    structural unless they carry such own body pages.
    """
    parent_paths = {skel.parent_path for skel in skeletons if skel.parent_path}
    leaves: list[tuple[int, int, LeafNode]] = []
    for index, skel in enumerate(skeletons):
        effective_end_page = _exclusive_end(skeletons, index)
        is_internal = skel.section_path in parent_paths
        body_pages: tuple[int, ...] | None = None
        if is_internal:
            body_page_list = _internal_body_pages(skel, skeletons, index=index)
            if not body_page_list:
                continue
            body_pages = tuple(body_page_list)

        sort_page = body_pages[0] if body_pages else skel.start_page
        leaves.append(
            (
                sort_page,
                index,
                LeafNode(
                    section_path=skel.section_path,
                    title=skel.title,
                    level=skel.level,
                    start_page=skel.start_page,
                    end_page=effective_end_page,
                    parent_path=skel.parent_path,
                    body_pages=body_pages,
                ),
            )
        )
    leaves.sort(key=lambda item: (item[0], item[1]))
    return [leaf for _, _, leaf in leaves]


def _internal_body_pages(
    skel: SectionSkeleton,
    skeletons: list[SectionSkeleton],
    *,
    index: int,
) -> list[int]:
    """Pages owned by an internal section itself, excluding descendants."""
    pages = set(range(skel.start_page, _exclusive_end(skeletons, index) + 1))
    descendant_prefix = f"{skel.section_path}/"
    for other_index, other in enumerate(skeletons):
        if other.section_path == skel.section_path:
            continue
        if not other.section_path.startswith(descendant_prefix):
            continue
        pages.difference_update(
            range(other.start_page, _exclusive_end(skeletons, other_index) + 1)
        )
    return sorted(pages)


def _exclusive_end(skeletons: list[SectionSkeleton], index: int) -> int:
    """Return the last page owned by a skeleton before the next sibling starts.

    The locator emits closed-closed section ranges, so adjacent sections can
    overlap at the boundary page. Ownership is exclusive at the next start page.
    """
    skeleton = skeletons[index]
    later_starts = [
        skel.start_page for skel in skeletons if skel.start_page > skeleton.start_page
    ]
    if not later_starts:
        return skeleton.end_page
    return min(skeleton.end_page, min(later_starts) - 1)


def assign_pages_to_leaves(
    leaves: list[LeafNode],
    *,
    available_pages: set[int],
) -> tuple[list[NodePageView], dict[int, LeafNode]]:
    """Map every leaf to its pages and decide per-page text ownership.

    Parameters
    ----------
    leaves:
        Leaf nodes in reading order.
    available_pages:
        Pages that actually have a rendered/body chunk (excludes pages outside
        the document or with no content).

    Returns
    -------
    (views, page_owner)
        ``views`` is one ``NodePageView`` per leaf (same order as ``leaves``).
        ``page_owner`` maps a page index to the leaf that owns its body text
        (the first leaf, in reading order, that covers it).
    """
    page_owner: dict[int, LeafNode] = {}
    views: list[NodePageView] = []
    for leaf in leaves:
        source_pages = (
            list(leaf.body_pages)
            if leaf.body_pages is not None
            else list(range(leaf.start_page, leaf.end_page + 1))
        )
        pages = [page for page in source_pages if page in available_pages]
        owned: list[int] = []
        for page in pages:
            if page not in page_owner:
                page_owner[page] = leaf
                owned.append(page)
        views.append(NodePageView(leaf=leaf, pages=pages, owned_pages=owned))
    return views, page_owner


def build_node_content(
    view: NodePageView,
    *,
    page_owner: dict[int, LeafNode],
    page_text: dict[int, str],
) -> str:
    """Assemble a node's body content with per-page text deduplication.

    Owned pages contribute their resolved body text; shared pages contribute a
    ``SAME-AS <owner path> p<page>`` reference so the text is stored once.
    """
    segments: list[str] = []
    for page in view.pages:
        owner = page_owner.get(page)
        if owner is not None and owner.section_path == view.leaf.section_path:
            segments.append((page_text.get(page) or "").strip())
        elif owner is not None:
            segments.append(f"[{SAME_AS_PREFIX} {owner.section_path} p{page}]")
    return "\n\n".join(segment for segment in segments if segment).strip()


def pages_by_leaf_count(views: list[NodePageView]) -> dict[int, list[LeafNode]]:
    """Map each page to the leaves that cover it (reading order)."""
    page_to_leaves: dict[int, list[LeafNode]] = {}
    for view in views:
        for page in view.pages:
            page_to_leaves.setdefault(page, []).append(view.leaf)
    return page_to_leaves


def format_owned_page_coverage(pages: list[int]) -> str:
    """Readable coverage phrase for owned pages (contiguous → range)."""
    if not pages:
        return ""
    if len(pages) == 1:
        return f"page {pages[0]}"
    contiguous = pages[-1] - pages[0] + 1 == len(pages) and pages == list(
        range(pages[0], pages[-1] + 1)
    )
    if contiguous:
        return f"pages {pages[0]}-{pages[-1]}"
    return "pages " + ", ".join(str(page) for page in pages)


def aggregate_owned_page_tags(
    *,
    owned_pages: list[int],
    tag_by_page: dict[int, PageTagResult],
) -> tuple[str, list[str], list[dict[str, str]]]:
    """Aggregate page-level tags for pages this node owns.

    SAME-AS shared pages are excluded: only ``owned_pages`` contribute.
    """
    if not owned_pages:
        return "", [], []

    if len(owned_pages) == 1:
        tag = tag_by_page.get(owned_pages[0])
        if tag is None:
            return "", [], []
        entities = normalize_entities(tag.entities)
        keywords = [entity["text"] for entity in entities]
        summary = (tag.summary or "").strip()
        if summary.upper() == "EMPTY":
            summary = ""
        return summary, keywords, entities

    page_lines: list[str] = []
    entities: list[dict[str, str]] = []
    seen_entities: set[tuple[str, str]] = set()
    for page in owned_pages:
        tag = tag_by_page.get(page)
        if tag is None:
            continue
        summary = (tag.summary or "").strip()
        if summary and summary.upper() != "EMPTY":
            page_lines.append(f"Page {page}: {summary}")
        for entity in normalize_entities(tag.entities):
            key = (entity["type"].casefold(), entity["text"].casefold())
            if key in seen_entities:
                continue
            seen_entities.add(key)
            entities.append(entity)

    keywords = [entity["text"] for entity in entities]
    if not page_lines:
        return "", keywords, entities
    header = f"This section covers {format_owned_page_coverage(owned_pages)}."
    return f"{header}\n\n" + "\n".join(page_lines), keywords, entities


# ── VLM-backed helpers ───────────────────────────────────────────────


def resolve_page_text(
    *,
    page: int,
    raw_text: str,
    image_path: str | None,
    vlm_model: str | None,
    budget: Any | None = None,
    body_start_text: str = "",
) -> str:
    """Body text for an owned page: PyMuPDF text, or VLM OCR for scanned pages.

    Electronic PDFs already have PyMuPDF text; scanned pages have (near) empty
    text and fall back to the shared ``transcribe()`` OCR primitive (§4.2).
    When ``body_start_text`` is set, keep only content from that anchor onward.
    """
    del budget
    text = (raw_text or "").strip()
    if not text:
        if not vlm_model or not image_path or not os.path.exists(image_path):
            return ""
        text = transcribe(
            image_paths=[image_path],
            model=vlm_model,
            max_tokens=1500,
            usage_task="page_memory.node_ocr",
        )
    if not body_start_text:
        return text
    sliced, matched = slice_text_from_anchor(text, body_start_text)
    if not matched:
        logger.warning(
            "[node_assembler] body_start_text not found on page {}; keeping full text",
            page,
        )
    return sliced


# ── Orchestration ────────────────────────────────────────────────────


def build_node_rows(
    *,
    skeletons: list[SectionSkeleton],
    raw_text_by_page: dict[int, str],
    image_path_by_page: dict[int, str],
    kind_by_page: dict[int, str],
    tag_by_page: dict[int, PageTagResult],
    filename: str,
    verdict: str,
    budget: Any | None = None,
    vlm_model: str | None = None,
    page_assets_by_page: dict[int, list[PageAsset]] | None = None,
    node_assembly_concurrency: int = 3,
    body_start_by_page: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Assemble one row per leaf section node (node-granularity chunks)."""
    del kind_by_page, verdict, budget
    available_pages = set(raw_text_by_page.keys())
    leaves = identify_leaf_nodes(skeletons)
    views, page_owner = assign_pages_to_leaves(leaves, available_pages=available_pages)
    page_to_leaves = pages_by_leaf_count(views)
    resolved_concurrency = max(1, node_assembly_concurrency)
    anchors = body_start_by_page or {}

    # Resolve body text once per owned page (PyMuPDF, OCR fallback for scanned).
    resolved_text: dict[int, str] = {}
    owned_pages = sorted({page for view in views for page in view.owned_pages})
    if owned_pages:
        import gevent
        from gevent.pool import Pool as GeventPool

        def _resolve_one(page: int) -> tuple[int, str]:
            return page, resolve_page_text(
                page=page,
                raw_text=raw_text_by_page.get(page, ""),
                image_path=image_path_by_page.get(page),
                vlm_model=vlm_model,
                body_start_text=anchors.get(page, ""),
            )

        with stage_timer(
            "page_memory.node_ocr",
            page_count=len(owned_pages),
            concurrency=resolved_concurrency,
        ):
            pool = GeventPool(size=min(resolved_concurrency, len(owned_pages)))
            greenlets = [pool.spawn(_resolve_one, page) for page in owned_pages]
            gevent.joinall(greenlets, raise_error=True)
            resolved_pairs = [
                cast(tuple[int, str], greenlet.value)
                for greenlet in greenlets
            ]
            resolved_text = {page: text for page, text in resolved_pairs}

    rows: list[dict[str, Any]] = []
    rows_by_path: dict[str, dict[str, Any]] = {}
    with stage_timer("page_memory.node_rows", node_count=len(views)):
        for view in views:
            leaf = view.leaf
            content = build_node_content(
                view,
                page_owner=page_owner,
                page_text=resolved_text,
            )
            summary, keywords, entities = aggregate_owned_page_tags(
                owned_pages=view.owned_pages,
                tag_by_page=tag_by_page,
            )
            know_id = f"node_{gen_str_codes(f'{filename}::{leaf.section_path}')}"
            extra_metadata = _build_page_extra_metadata(
                pages=view.pages,
                image_path_by_page=image_path_by_page,
            )
            row = {
                "content": content,
                "path": leaf.section_path,
                "type": "page",
                "length": len(content),
                "keywords": ";".join(keywords),
                "summary": summary,
                "know_id": know_id,
                "tokens": "",
                "connectto": "",
                "addtime": get_str_time(),
                "page_nums": ",".join(str(page) for page in view.pages),
                "owned_page_nums": ",".join(str(page) for page in view.owned_pages),
                "entities": serialize_entities(entities),
                "asset_title": "",
                "extra_metadata": extra_metadata,
            }
            rows.append(row)
            rows_by_path[leaf.section_path] = row

        for view in views:
            row = rows_by_path[view.leaf.section_path]
            _attach_same_as_connections(
                row=row,
                view=view,
                page_owner=page_owner,
                rows_by_path=rows_by_path,
            )

    asset_rows: list[dict[str, Any]] = []
    if page_assets_by_page:
        asset_rows = build_asset_rows(page_assets_by_page)
        _attach_asset_connections(
            page_assets_by_page=page_assets_by_page,
            page_owner=page_owner,
            page_to_leaves=page_to_leaves,
            rows_by_path=rows_by_path,
        )

    logger.info(
        "[node_assembler] assembled {} asset rows + {} node rows from {} leaves ({} pages)",
        len(asset_rows),
        len(rows),
        len(leaves),
        len(available_pages),
    )
    return asset_rows + rows


def _attach_same_as_connections(
    *,
    row: dict[str, Any],
    view: NodePageView,
    page_owner: dict[int, LeafNode],
    rows_by_path: dict[str, dict[str, Any]],
) -> None:
    """Emit explicit same_as links for pages this node references but does not own."""
    for page in view.pages:
        owner = page_owner.get(page)
        if owner is None or owner.section_path == view.leaf.section_path:
            continue
        owner_row = rows_by_path.get(owner.section_path)
        if owner_row is None:
            continue
        owner_know_id = str(owner_row.get("know_id") or "").strip()
        if not owner_know_id:
            continue
        _append_connect_to(
            row,
            {
                "target": owner_know_id,
                "relation": "same_as",
                "ref": f"[{SAME_AS_PREFIX} {owner.section_path} p{page}]",
                "page": page,
            },
        )


def format_toc_entries_content(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        heading = str(entry.get("heading") or entry.get("title") or "").strip()
        if not heading:
            continue
        level = entry.get("level", 1)
        try:
            depth = max(int(level), 1)
        except (TypeError, ValueError):
            depth = 1
        page_number = entry.get("page_number")
        suffix = f" ...... {page_number}" if page_number is not None else ""
        lines.append(f"{'  ' * (depth - 1)}{heading}{suffix}")
    return "\n".join(lines).strip()


def build_toc_nav_skeletons(
    *,
    anatomy: Any | None,
    filename: str,
) -> list[SectionSkeleton]:
    """Navigation-only skeletons for TOC regions (not used by body ownership)."""
    policy = TocPagePolicy.from_anatomy(anatomy)
    if not policy.regions:
        return []

    skeletons: list[SectionSkeleton] = []
    for index, region in enumerate(policy.regions, start=1):
        pages = sorted({int(page) for page in region.toc_pages if int(page) > 0})
        if not pages:
            continue
        title = "Table of Contents" if index == 1 else f"Table of Contents ({index})"
        skeletons.append(
            SectionSkeleton(
                section_path=join_document_path([filename, title]),
                level=1,
                start_page=pages[0],
                end_page=pages[-1],
                title=title,
                parent_path=filename,
                evidence={
                    "source": "toc_static",
                    "content_kind": "table_of_contents",
                    "pure_toc_pages": list(region.pure_toc_pages),
                    "mixed_page": region.mixed_page,
                },
            )
        )
    return skeletons


def build_toc_node_rows(
    *,
    anatomy: Any | None,
    filename: str,
) -> list[dict[str, Any]]:
    """Synthetic TOC rows from Stage-1 entries; bypass SAME-AS ownership."""
    hierarchies = list(getattr(anatomy, "toc_hierarchies", None) or [])
    policy = TocPagePolicy.from_anatomy(anatomy)
    regions = list(policy.regions)
    if not regions and not hierarchies:
        return []

    rows: list[dict[str, Any]] = []
    count = max(len(regions), len(hierarchies), 1 if hierarchies else 0)
    for index in range(count):
        region = regions[index] if index < len(regions) else None
        hierarchy = hierarchies[index] if index < len(hierarchies) else {}
        entries = list((hierarchy or {}).get("toc_with_level") or [])
        content = format_toc_entries_content(entries)
        if region is not None:
            pages = sorted({int(page) for page in region.toc_pages if int(page) > 0})
        else:
            toc_range = (hierarchy or {}).get("toc_range") or []
            pages = sorted({int(page) for page in toc_range if int(page) > 0})
        if not pages and not content:
            continue
        title = "Table of Contents" if index == 0 else f"Table of Contents ({index + 1})"
        path = join_document_path([filename, title])
        know_id = f"node_{gen_str_codes(f'{filename}::{path}')}"
        rows.append(
            {
                "content": content,
                "path": path,
                "type": "page",
                "length": len(content),
                "keywords": "",
                "summary": "Table of Contents",
                "know_id": know_id,
                "tokens": "",
                "connectto": "",
                "addtime": get_str_time(),
                "page_nums": ",".join(str(page) for page in pages),
                "owned_page_nums": "",
                "entities": "",
                "asset_title": "",
                "extra_metadata": {
                    "content_kind": "table_of_contents",
                    "pure_toc_pages": list(region.pure_toc_pages) if region else pages,
                    "mixed_page": region.mixed_page if region else None,
                    "body_start_text": (
                        region.body_start_text if region else hierarchy.get("body_start_text", "")
                    ),
                },
            }
        )
    return rows


def merge_rows_by_first_page(
    toc_rows: list[dict[str, Any]],
    body_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Insert TOC rows by first physical page while preserving body order."""

    def _first_page(row: dict[str, Any]) -> int:
        raw = str(row.get("page_nums") or "").split(",")[0].strip()
        try:
            return int(raw)
        except ValueError:
            return 0

    asset_rows = [row for row in body_rows if row.get("type") != "page"]
    page_rows = [row for row in body_rows if row.get("type") == "page"]
    merged = sorted(
        [*toc_rows, *page_rows],
        key=lambda row: (
            _first_page(row),
            0 if (row.get("extra_metadata") or {}).get("content_kind") == "table_of_contents" else 1,
            str(row.get("path") or ""),
        ),
    )
    return asset_rows + merged


def _build_page_extra_metadata(
    *,
    pages: list[int],
    image_path_by_page: dict[int, str],
) -> dict[str, Any]:
    page_assets = _build_page_citation_assets(
        pages=pages,
        image_path_by_page=image_path_by_page,
    )
    if not page_assets:
        return {}
    return {"page_assets": page_assets}


def _build_page_citation_assets(
    *,
    pages: list[int],
    image_path_by_page: dict[int, str],
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    for page in pages:
        if page in seen_pages:
            continue
        seen_pages.add(page)
        image_path = image_path_by_page.get(page)
        if not image_path or not os.path.exists(image_path):
            continue
        artifact_ref = _promote_page_citation_asset(page=page, image_path=image_path)
        if not artifact_ref:
            continue
        width, height = _read_image_dimensions(image_path)
        asset = {
            "page_num": page,
            "artifact_ref": artifact_ref,
            "content_type": _PAGE_CITATION_ASSET_CONTENT_TYPE,
            "source": _PAGE_CITATION_ASSET_SOURCE,
        }
        if width is not None:
            asset["width"] = width
        if height is not None:
            asset["height"] = height
        assets.append(asset)
    return assets


def _promote_page_citation_asset(*, page: int, image_path: str) -> str:
    source_path = Path(image_path)
    output_dir = source_path.parent.parent
    target_dir = output_dir / "page_citation_assets"
    target_path = target_dir / f"page-{page}.png"
    artifact_ref = f"page_citation_assets/page-{page}.png"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != target_path.resolve():
            shutil.copyfile(source_path, target_path)
        return artifact_ref
    except Exception as exc:
        logger.warning(
            "[node_assembler] failed to promote page citation asset page={} path={}: {}",
            page,
            image_path,
            exc,
        )
        return ""


def _read_image_dimensions(image_path: str) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            return int(image.width), int(image.height)
    except Exception as exc:
        logger.debug(
            "[node_assembler] failed to read page citation image dimensions {}: {}",
            image_path,
            exc,
        )
        return None, None


def _attach_asset_connections(
    *,
    page_assets_by_page: dict[int, list[PageAsset]],
    page_owner: dict[int, LeafNode],
    page_to_leaves: dict[int, list[LeafNode]],
    rows_by_path: dict[str, dict[str, Any]],
) -> None:
    for page_index, assets in page_assets_by_page.items():
        owner_leaf = page_owner.get(page_index)
        leaves_on_page = page_to_leaves.get(page_index, [])
        for asset in assets:
            # target = bare URI path (resolvable via target_map → chunk_id)
            # ref = bracketed display reference (matches chunk-track convention)
            uri = (
                asset.html_uri
                if asset.kind == "table" and asset.html_uri
                else asset.image_uri
            )
            if not uri:
                continue
            ref = f"[{uri}]"
            if owner_leaf is not None:
                owner_row = rows_by_path.get(owner_leaf.section_path)
                if owner_row is not None:
                    _append_connect_to(
                        owner_row,
                        {
                            "target": uri,
                            "relation": "embeds",
                            "ref": ref,
                        },
                    )
            for leaf in leaves_on_page:
                if (
                    owner_leaf is not None
                    and leaf.section_path == owner_leaf.section_path
                ):
                    continue
                row = rows_by_path.get(leaf.section_path)
                if row is None:
                    continue
                connection: dict[str, Any] = {
                    "target": uri,
                    "relation": "related",
                    "ref": ref,
                }
                if owner_leaf is not None:
                    connection["same_as_owner"] = owner_leaf.section_path
                _append_connect_to(row, connection)


def _append_connect_to(row: dict[str, Any], connection: dict[str, Any]) -> None:
    existing = row.get("connectto")
    if isinstance(existing, str) and existing.strip():
        try:
            connections = json.loads(existing)
        except json.JSONDecodeError:
            connections = []
    elif isinstance(existing, list):
        connections = list(existing)
    else:
        connections = []
    connections.append(connection)
    row["connectto"] = json.dumps(connections, ensure_ascii=False)
