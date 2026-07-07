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

Summary/keywords are settled per node (see ``node_summary``): a node covering
multiple pages is summarized as a whole, and a page hosting multiple nodes is
summarized per node using a title boundary so the slices do not overlap.
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
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.services.ai.summary.engine import summarize, transcribe

SAME_AS_PREFIX = "SAME-AS"

_NODE_SUMMARY_MAX_PAGES_DEFAULT = 5
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


def next_title_on_page(
    leaf: LeafNode,
    *,
    page: int,
    leaves_on_page: list[LeafNode],
) -> str | None:
    """Return the title of the next leaf starting on the same page after *leaf*.

    Used to bound the summary slice when a page hosts multiple nodes.  Returns
    ``None`` when *leaf* is the last node beginning on this page.
    """
    ordered = [item for item in leaves_on_page if item.start_page == page]
    for index, item in enumerate(ordered):
        if item.section_path == leaf.section_path:
            if index + 1 < len(ordered):
                return ordered[index + 1].title
            return None
    return None


def pages_by_leaf_count(views: list[NodePageView]) -> dict[int, list[LeafNode]]:
    """Map each page to the leaves that cover it (reading order)."""
    page_to_leaves: dict[int, list[LeafNode]] = {}
    for view in views:
        for page in view.pages:
            page_to_leaves.setdefault(page, []).append(view.leaf)
    return page_to_leaves


# ── VLM-backed helpers ───────────────────────────────────────────────


def resolve_page_text(
    *,
    page: int,
    raw_text: str,
    image_path: str | None,
    vlm_model: str | None,
    budget: Any | None = None,
) -> str:
    """Body text for an owned page: PyMuPDF text, or VLM OCR for scanned pages.

    Electronic PDFs already have PyMuPDF text; scanned pages have (near) empty
    text and fall back to the shared ``transcribe()`` OCR primitive (§4.2).
    """
    text = (raw_text or "").strip()
    if text:
        return text
    if not vlm_model or not image_path or not os.path.exists(image_path):
        return ""
    return transcribe(
        image_paths=[image_path],
        model=vlm_model,
        max_tokens=1500,
        usage_task="page_memory.node_ocr",
    )


def compute_node_summary(
    *,
    view: NodePageView,
    page_to_leaves: dict[int, list[LeafNode]],
    tag_by_page: dict[int, PageTagResult],
    image_path_by_page: dict[int, str],
    vlm_model: str | None,
    budget: Any | None = None,
    node_summary_max_pages: int = _NODE_SUMMARY_MAX_PAGES_DEFAULT,
) -> tuple[str, list[str], list[dict[str, str]]]:
    """Settle a node's summary, keywords, and typed entities (§4.4).

    Reuses the per-page tag when the node is a single page that no sibling leaf
    shares.  Otherwise asks the VLM to summarize the node as a whole, bounding
    the slice with the next sibling title when the page hosts multiple nodes.
    Falls back to combining per-page tags when the VLM is unavailable.

    Returns ``(summary, keywords, entities)`` where ``keywords`` is the flattened
    surface-form list (transitional) and ``entities`` is the typed
    ``{"text","type"}`` list.
    """
    pages = view.pages
    if not pages:
        return "", [], []

    single_page = len(pages) == 1
    shared = any(len(page_to_leaves.get(page, [])) > 1 for page in pages)

    if single_page and not shared:
        tag = tag_by_page.get(pages[0])
        if tag is not None:
            return tag.summary, list(tag.keywords), list(tag.entities)
        return "", [], []

    if vlm_model:
        result = _vlm_node_summary(
            view=view,
            page_to_leaves=page_to_leaves,
            image_path_by_page=image_path_by_page,
            vlm_model=vlm_model,
            node_summary_max_pages=node_summary_max_pages,
        )
        if result is not None:
            return result

    return _combine_page_tags(pages=pages, tag_by_page=tag_by_page)


def _combine_page_tags(
    *,
    pages: list[int],
    tag_by_page: dict[int, PageTagResult],
) -> tuple[str, list[str], list[dict[str, str]]]:
    summaries: list[str] = []
    keywords: list[str] = []
    entities: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_entities: set[str] = set()
    for page in pages:
        tag = tag_by_page.get(page)
        if tag is None:
            continue
        summary = (tag.summary or "").strip()
        if summary and summary.upper() != "EMPTY":
            summaries.append(summary)
        for keyword in tag.keywords:
            key = keyword.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                keywords.append(keyword.strip())
        for entity in tag.entities:
            entity_text = str(entity.get("text", "")).strip()
            if not entity_text:
                continue
            entity_key = entity_text.casefold()
            if entity_key not in seen_entities:
                seen_entities.add(entity_key)
                entities.append(
                    {"text": entity_text, "type": str(entity.get("type", "")).strip()}
                )
    return " ".join(summaries).strip(), keywords, entities


def _vlm_node_summary(
    *,
    view: NodePageView,
    page_to_leaves: dict[int, list[LeafNode]],
    image_path_by_page: dict[int, str],
    vlm_model: str,
    node_summary_max_pages: int,
) -> tuple[str, list[str], list[dict[str, str]]] | None:
    leaf = view.leaf
    pages = view.pages[:node_summary_max_pages]

    # Boundary title: only meaningful when this node's start page hosts a later
    # sibling node, so the VLM can stop at that boundary.
    next_title = next_title_on_page(
        leaf,
        page=leaf.start_page,
        leaves_on_page=page_to_leaves.get(leaf.start_page, []),
    )

    image_paths = [
        path
        for page in pages
        if (path := image_path_by_page.get(page)) and os.path.exists(path)
    ]
    if not image_paths:
        return None

    result = summarize(
        mode="page",
        image_paths=image_paths,
        model=vlm_model,
        usage_task="page_memory.node_summary",
        prompt_task="page-memory-node-summary",
        prompt_paras={
            "max_tokens": 400,
            "node_title": leaf.title,
            "next_title": next_title or "",
            "kw_num": 5,
        },
    )
    if not result.summary and not result.entities:
        return None
    return (
        result.summary,
        [e.text for e in result.entities],
        [e.to_dict() for e in result.entities],
    )


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
    node_summary_max_pages: int = _NODE_SUMMARY_MAX_PAGES_DEFAULT,
    node_assembly_concurrency: int = 2,
) -> list[dict[str, Any]]:
    """Assemble one row per leaf section node (node-granularity chunks)."""
    available_pages = set(raw_text_by_page.keys())
    leaves = identify_leaf_nodes(skeletons)
    views, page_owner = assign_pages_to_leaves(leaves, available_pages=available_pages)
    page_to_leaves = pages_by_leaf_count(views)
    resolved_concurrency = max(1, node_assembly_concurrency)

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

    summaries: dict[int, tuple[str, list[str], list[dict[str, str]]]] = {}
    if views:
        import gevent
        from gevent.pool import Pool as GeventPool

        def _summarize_one(
            index: int,
        ) -> tuple[int, tuple[str, list[str], list[dict[str, str]]]]:
            return index, compute_node_summary(
                view=views[index],
                page_to_leaves=page_to_leaves,
                tag_by_page=tag_by_page,
                image_path_by_page=image_path_by_page,
                vlm_model=vlm_model,
                node_summary_max_pages=node_summary_max_pages,
            )

        with stage_timer(
            "page_memory.node_summary",
            node_count=len(views),
            concurrency=resolved_concurrency,
        ):
            pool = GeventPool(size=min(resolved_concurrency, len(views)))
            greenlets = [
                pool.spawn(_summarize_one, index)
                for index in range(len(views))
            ]
            gevent.joinall(greenlets, raise_error=True)
            summary_pairs = [
                cast(
                    tuple[int, tuple[str, list[str], list[dict[str, str]]]],
                    greenlet.value,
                )
                for greenlet in greenlets
            ]
            summaries = {index: result for index, result in summary_pairs}

    rows: list[dict[str, Any]] = []
    rows_by_path: dict[str, dict[str, Any]] = {}
    with stage_timer("page_memory.node_rows", node_count=len(views)):
        for index, view in enumerate(views):
            leaf = view.leaf
            content = build_node_content(
                view,
                page_owner=page_owner,
                page_text=resolved_text,
            )
            summary, keywords, entities = summaries.get(index, ("", [], []))
            know_id = f"node_{gen_str_codes(f'{filename}::{leaf.section_path}')}"
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
                "entities": serialize_entities(entities),
                "asset_title": "",
                "extra_metadata": _build_page_extra_metadata(
                    pages=view.pages,
                    image_path_by_page=image_path_by_page,
                ),
            }
            rows.append(row)
            rows_by_path[leaf.section_path] = row

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
