"""Node-granularity assembly for the page-memory track.

Page-track historically emitted one ``type=page`` chunk per physical page,
so a section spanning N pages produced N chunks sharing the same ``path``.
This module switches the unit of assembly to the **leaf section node**:

- One chunk per leaf node (path).  Internal/structural nodes live in the
  navigation tree only (their summaries aggregate bottom-up).
- A page that belongs to multiple nodes is *referenced* by each of them; the
  page image is never duplicated (``page_image_uris`` is a list of shared
  references).
- A page's body text is stored **once**, under the first leaf (in reading
  order) that covers it.  Other nodes that share the page emit a
  ``SAME-AS <owner path>`` marker instead of repeating the text.  Body text is
  not a core asset in page-track — the page image is.

Summary/keywords are settled per node (see ``node_summary``): a node covering
multiple pages is summarized as a whole, and a page hosting multiple nodes is
summarized per node using a title boundary so the slices do not overlap.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

from loguru import logger

from app.services.document_agent.budget import BudgetTracker
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.page_memory.page_assets import (
    PageAsset,
    asset_reference,
    build_asset_rows,
)
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.services.ai.prompt_service import build_prompt
from shared.utils.token_estimate import estimate_tokens

SAME_AS_PREFIX = "SAME-AS"

_BUDGET_STAGE = "page_tagging"
_NODE_SUMMARY_MAX_PAGES_DEFAULT = 5


@dataclass(frozen=True)
class LeafNode:
    """A leaf section node = one node-granularity chunk."""

    section_path: str
    title: str
    level: int
    start_page: int
    end_page: int
    parent_path: str | None = None


@dataclass
class NodePageView:
    """Resolved page assignment for a single leaf node."""

    leaf: LeafNode
    pages: list[int] = field(default_factory=list)
    """All body pages covered by this leaf (owned + shared), in order."""

    owned_pages: list[int] = field(default_factory=list)
    """Pages whose body text is stored under this node."""


def identify_leaf_nodes(skeletons: list[SectionSkeleton]) -> list[LeafNode]:
    """Return leaf skeletons in reading order.

    A skeleton is a leaf when no other skeleton declares it as ``parent_path``.
    Reading order = ``(start_page, original index)`` so that siblings sharing a
    start page keep the top-to-bottom order produced by fine hierarchy.
    """
    parent_paths = {skel.parent_path for skel in skeletons if skel.parent_path}
    leaves: list[tuple[int, int, LeafNode]] = []
    for index, skel in enumerate(skeletons):
        if skel.section_path in parent_paths:
            continue
        leaves.append(
            (
                skel.start_page,
                index,
                LeafNode(
                    section_path=skel.section_path,
                    title=skel.title,
                    level=skel.level,
                    start_page=skel.start_page,
                    end_page=skel.end_page,
                    parent_path=skel.parent_path,
                ),
            )
        )
    leaves.sort(key=lambda item: (item[0], item[1]))
    return [leaf for _, _, leaf in leaves]


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
        pages = [
            page
            for page in range(leaf.start_page, leaf.end_page + 1)
            if page in available_pages
        ]
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


def _node_summary_max_pages() -> int:
    return int(
        os.environ.get(
            "PAGE_MEMORY_NODE_SUMMARY_MAX_PAGES",
            str(_NODE_SUMMARY_MAX_PAGES_DEFAULT),
        )
    )


def _read_image_b64(image_path: str) -> str | None:
    try:
        with open(image_path, "rb") as handle:
            return base64.b64encode(handle.read()).decode()
    except Exception as exc:  # pragma: no cover - filesystem edge
        logger.warning("[node_assembler] failed to read image {}: {}", image_path, exc)
        return None


def resolve_page_text(
    *,
    page: int,
    raw_text: str,
    image_path: str | None,
    vlm_model: str | None,
    budget: BudgetTracker | None,
) -> str:
    """Body text for an owned page: PyMuPDF text, or VLM OCR for scanned pages.

    Electronic PDFs already have PyMuPDF text; scanned pages have (near) empty
    text and fall back to a one-shot VLM OCR transcription.
    """
    text = (raw_text or "").strip()
    if text:
        return text
    if not vlm_model or not image_path or not os.path.exists(image_path):
        return ""

    img_b64 = _read_image_b64(image_path)
    if img_b64 is None:
        return ""

    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-vlm-ocr", "", "", paras={"max_tokens": 1500}
    )
    est = estimate_tokens(prompt) + 1000
    if budget is not None and not budget.try_reserve(
        "visual", est, stage=_BUDGET_STAGE
    ):
        logger.debug("[node_assembler] OCR budget exhausted for page {}", page)
        return ""

    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=vlm_model)
        content_parts = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            },
        ]
        raw_response, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=vlm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            usage_task="page_memory.node_ocr",
        )
        if budget is not None:
            budget.commit(
                "visual",
                actual=usage.get("total_tokens", est),
                est=est,
                stage=_BUDGET_STAGE,
            )
        try:
            data = json.loads(raw_response)
            return str(data.get("text", "")).strip()
        except (json.JSONDecodeError, TypeError):
            # VLM returned non-JSON; use raw text directly
            return raw_response.strip() if raw_response else ""
    except Exception as exc:
        logger.warning("[node_assembler] OCR failed for page {}: {}", page, exc)
        if budget is not None:
            budget.refund("visual", est=est, stage=_BUDGET_STAGE)
        return ""


def compute_node_summary(
    *,
    view: NodePageView,
    page_to_leaves: dict[int, list[LeafNode]],
    tag_by_page: dict[int, PageTagResult],
    image_path_by_page: dict[int, str],
    vlm_model: str | None,
    budget: BudgetTracker | None,
) -> tuple[str, list[str]]:
    """Settle a node's summary/keywords.

    Reuses the per-page tag when the node is a single page that no sibling leaf
    shares.  Otherwise asks the VLM to summarize the node as a whole, bounding
    the slice with the next sibling title when the page hosts multiple nodes.
    Falls back to combining per-page tags when the VLM is unavailable.
    """
    pages = view.pages
    if not pages:
        return "", []

    single_page = len(pages) == 1
    shared = any(len(page_to_leaves.get(page, [])) > 1 for page in pages)

    if single_page and not shared:
        tag = tag_by_page.get(pages[0])
        if tag is not None:
            return tag.summary, list(tag.keywords)
        return "", []

    if vlm_model:
        result = _vlm_node_summary(
            view=view,
            page_to_leaves=page_to_leaves,
            image_path_by_page=image_path_by_page,
            vlm_model=vlm_model,
            budget=budget,
        )
        if result is not None:
            return result

    return _combine_page_tags(pages=pages, tag_by_page=tag_by_page)


def _combine_page_tags(
    *,
    pages: list[int],
    tag_by_page: dict[int, PageTagResult],
) -> tuple[str, list[str]]:
    summaries: list[str] = []
    keywords: list[str] = []
    seen: set[str] = set()
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
    return " ".join(summaries).strip(), keywords


def _vlm_node_summary(
    *,
    view: NodePageView,
    page_to_leaves: dict[int, list[LeafNode]],
    image_path_by_page: dict[int, str],
    vlm_model: str,
    budget: BudgetTracker | None,
) -> tuple[str, list[str]] | None:
    leaf = view.leaf
    pages = view.pages[: _node_summary_max_pages()]

    # Boundary title: only meaningful when this node's start page hosts a later
    # sibling node, so the VLM can stop at that boundary.
    next_title = next_title_on_page(
        leaf,
        page=leaf.start_page,
        leaves_on_page=page_to_leaves.get(leaf.start_page, []),
    )

    image_parts: list[dict[str, Any]] = []
    for page in pages:
        path = image_path_by_page.get(page)
        if not path or not os.path.exists(path):
            continue
        img_b64 = _read_image_b64(path)
        if img_b64 is None:
            continue
        image_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )
    if not image_parts:
        return None

    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-node-summary",
        "",
        "",
        paras={
            "max_tokens": 400,
            "node_title": leaf.title,
            "next_title": next_title or "",
            "kw_num": 5,
        },
    )
    est = estimate_tokens(prompt) + 800 * len(image_parts)
    if budget is not None and not budget.try_reserve(
        "visual", est, stage=_BUDGET_STAGE
    ):
        logger.debug(
            "[node_assembler] node summary budget exhausted for {}",
            leaf.section_path,
        )
        return None

    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=vlm_model)
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content_parts.extend(image_parts)
        raw_response, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=vlm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            usage_task="page_memory.node_summary",
        )
        if budget is not None:
            budget.commit(
                "visual",
                actual=usage.get("total_tokens", est),
                est=est,
                stage=_BUDGET_STAGE,
            )
        data = json.loads(raw_response)
        kw_str = str(data.get("keywords", ""))
        keywords = [k.strip() for k in kw_str.split(";") if k.strip()]
        return str(data.get("summary", "")).strip(), keywords
    except Exception as exc:
        logger.warning(
            "[node_assembler] node summary VLM failed for {}: {}",
            leaf.section_path,
            exc,
        )
        if budget is not None:
            budget.refund("visual", est=est, stage=_BUDGET_STAGE)
        return None


# ── Orchestration ────────────────────────────────────────────────────


def build_node_rows(
    *,
    skeletons: list[SectionSkeleton],
    raw_text_by_page: dict[int, str],
    image_uri_by_page: dict[int, str],
    image_path_by_page: dict[int, str],
    kind_by_page: dict[int, str],
    tag_by_page: dict[int, PageTagResult],
    filename: str,
    verdict: str,
    native_hierarchy: bool,
    budget: BudgetTracker | None = None,
    vlm_model: str | None = None,
    page_assets_by_page: dict[int, list[PageAsset]] | None = None,
) -> list[dict[str, Any]]:
    """Assemble one row per leaf section node (node-granularity chunks)."""
    available_pages = set(raw_text_by_page.keys())
    leaves = identify_leaf_nodes(skeletons)
    views, page_owner = assign_pages_to_leaves(leaves, available_pages=available_pages)
    page_to_leaves = pages_by_leaf_count(views)

    # Resolve body text once per owned page (PyMuPDF, OCR fallback for scanned).
    resolved_text: dict[int, str] = {}
    for view in views:
        for page in view.owned_pages:
            resolved_text[page] = resolve_page_text(
                page=page,
                raw_text=raw_text_by_page.get(page, ""),
                image_path=image_path_by_page.get(page),
                vlm_model=vlm_model,
                budget=budget,
            )

    rows: list[dict[str, Any]] = []
    rows_by_path: dict[str, dict[str, Any]] = {}
    for view in views:
        leaf = view.leaf
        content = build_node_content(
            view,
            page_owner=page_owner,
            page_text=resolved_text,
        )
        summary, keywords = compute_node_summary(
            view=view,
            page_to_leaves=page_to_leaves,
            tag_by_page=tag_by_page,
            image_path_by_page=image_path_by_page,
            vlm_model=vlm_model,
            budget=budget,
        )
        page_image_uris = [
            image_uri_by_page[page]
            for page in view.pages
            if image_uri_by_page.get(page)
        ]
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
            "extra_metadata": {
                "granularity": "node",
                "section_path": leaf.section_path,
                "section_level": leaf.level,
                "page_indices": list(view.pages),
                "owned_pages": list(view.owned_pages),
                "page_image_uris": page_image_uris,
                "kind": kind_by_page.get(leaf.start_page, "normal"),
                "source_verdict": verdict,
                "native_hierarchy": native_hierarchy,
            },
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
            ref = asset_reference(asset)
            if not ref:
                continue
            if owner_leaf is not None:
                owner_row = rows_by_path.get(owner_leaf.section_path)
                if owner_row is not None:
                    _append_connect_to(
                        owner_row,
                        {
                            "target": ref,
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
                    "target": ref,
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
