"""Fine-grained hierarchy builder for page-memory native hierarchy (Step 3).

Page-memory candidates are already confirmed visual headings from PDF/PPT pages.
They must not run through the chunk-track heading executor, whose compact/body
row semantics are designed for raw text.  This module calls a dedicated
``page-memory-hierarchy`` prompt and builds deeper ``SectionSkeleton`` entries
under each coarse TOC leaf.

Boundary trimming is code-side: VLM extracts all outline titles in reading
order; ``_collect_candidates`` then drops titles at/before the coarse start
anchor and at/after the next-leaf end anchor.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from app.services.page_memory._utils import page_scope_info
from shared.services.ai.llm_overrides import get_text_client
from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.response_process_service import eval_response
from shared.services.chunks.path_segments import append_document_path


def refine_fat_leaf_skeletons(
    *,
    coarse_skeletons: list[SectionSkeleton],
    tag_results: list[PageTagResult],
    fat_leaf_pages: set[int],
    next_title_by_path: dict[str, str | None] | None = None,
    model_name: str | None = None,
    max_tokens: int = 2000,
    max_depth: int = 6,
    trace_recorder: Any | None = None,
) -> list[SectionSkeleton]:
    """Refine coarse TOC leaf skeletons using VLM-observed title candidates.

    For each coarse skeleton that overlaps fat-leaf pages:
    1. Collect ``observed_titles`` in reading order and trim by start/end
       coarse anchors.
    2. Ask the page-memory hierarchy prompt to assign relative levels.
    3. Rebuild the nested tree and graft it under the coarse leaf.

    Thin leaves (not in ``fat_leaf_pages``) are passed through unchanged.
    """
    if not fat_leaf_pages:
        return coarse_skeletons

    next_map = next_title_by_path or {}
    refined: list[SectionSkeleton] = []

    for index, skeleton in enumerate(coarse_skeletons):
        exclusive_end = _exclusive_end(coarse_skeletons, index)
        skel_pages = set(range(skeleton.start_page, exclusive_end + 1))
        overlap = skel_pages & fat_leaf_pages

        if not overlap:
            refined.append(skeleton)
            continue

        end_title = next_map.get(skeleton.section_path)
        candidates = _collect_candidates(
            skeleton=skeleton,
            exclusive_end=exclusive_end,
            tag_results=tag_results,
            fat_leaf_pages=fat_leaf_pages,
            start_title=skeleton.title,
            end_title=end_title,
        )

        if not candidates:
            refined.append(skeleton)
            continue

        deeper = _run_hierarchy_on_candidates(
            candidates=candidates,
            skeleton=skeleton,
            model_name=model_name,
            max_tokens=max_tokens,
            max_depth=max_depth,
            trace_recorder=trace_recorder,
        )

        if deeper:
            refined.extend(deeper)
        else:
            refined.append(skeleton)

    logger.info(
        "[page_memory.fine_hierarchy] refined {} → {} skeletons ({} fat-leaf pages)",
        len(coarse_skeletons),
        len(refined),
        len(fat_leaf_pages),
    )
    return refined


def compute_fat_leaf_pages(
    skeletons: list[SectionSkeleton],
    min_pages: int,
) -> set[int]:
    """Compute the set of page indices belonging to fat-leaf sections.

    A "fat leaf" is a coarse skeleton (TOC leaf node) whose page range
    exceeds ``min_pages``.  Only these pages get VLM title detection.

    Uses exclusive end boundaries when sibling starts are visible in
    ``skeletons``. For single-leaf scopes the closed ``end_page`` is used,
    which includes the shared boundary page with the next leaf.
    """
    fat_pages: set[int] = set()
    for idx, skel in enumerate(skeletons):
        exclusive_end = _exclusive_end(skeletons, idx)
        page_span = exclusive_end - skel.start_page + 1
        if page_span > min_pages:
            fat_pages.update(range(skel.start_page, exclusive_end + 1))
    return fat_pages


def build_next_title_by_path(
    skeletons: list[SectionSkeleton],
) -> dict[str, str | None]:
    """Map each leaf ``section_path`` to the next leaf's title (by start_page)."""
    ordered = sorted(
        skeletons,
        key=lambda item: (
            int(getattr(item, "start_page", 0) or 0),
            str(getattr(item, "section_path", "") or ""),
        ),
    )
    next_map: dict[str, str | None] = {}
    for index, skeleton in enumerate(ordered):
        next_title: str | None = None
        start_page = int(getattr(skeleton, "start_page", 0) or 0)
        for later in ordered[index + 1 :]:
            later_start = int(getattr(later, "start_page", 0) or 0)
            if later_start > start_page:
                title = str(getattr(later, "title", "") or "").strip()
                next_title = title or None
                break
        next_map[str(skeleton.section_path)] = next_title
    return next_map


# ── Internal helpers ─────────────────────────────────────────────────


def _collect_candidates(
    *,
    skeleton: SectionSkeleton,
    exclusive_end: int,
    tag_results: list[PageTagResult],
    fat_leaf_pages: set[int],
    start_title: str,
    end_title: str | None,
) -> list[dict[str, Any]]:
    """Gather VLM-observed titles within a skeleton's page range.

    Preserves page ascending order and within-page VLM reading order, then
    trims by coarse start/end anchors:

    - Drop the start anchor and everything before it.
    - Drop the end anchor (next leaf title) and everything after it.

    Returns list of ``{id, heading, page, prominence}``.
    """
    raw: list[dict[str, Any]] = []
    tag_by_page = {t.page_index: t for t in tag_results}

    for page in range(skeleton.start_page, exclusive_end + 1):
        if page not in fat_leaf_pages:
            continue
        tag = tag_by_page.get(page)
        if tag is None or not tag.observed_titles:
            continue
        # Keep VLM reading order; do not re-sort by prominence.
        for title_entry in tag.observed_titles:
            text = str(title_entry.get("text", "") or "").strip()
            if not text or len(text) < 2:
                continue
            key = _title_key(text)
            if not key:
                continue
            raw.append({
                "heading": text,
                "page": page,
                "prominence": title_entry.get("prominence"),
                "key": key,
            })

    trimmed = _trim_by_coarse_anchors(
        raw,
        start_title=start_title,
        end_title=end_title,
        section_path=skeleton.section_path,
    )

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidate_id = 0
    for item in trimmed:
        key = str(item["key"])
        if key in seen:
            continue
        seen.add(key)
        candidate_id += 1
        candidates.append({
            "id": candidate_id,
            "heading": item["heading"],
            "page": item["page"],
            "prominence": item.get("prominence"),
        })
    return candidates


def _trim_by_coarse_anchors(
    raw: list[dict[str, Any]],
    *,
    start_title: str,
    end_title: str | None,
    section_path: str,
) -> list[dict[str, Any]]:
    """Hard-trim reading-order titles by start/end coarse anchors."""
    start_key = _title_key(start_title)
    end_key = _title_key(end_title) if end_title else ""

    trimmed = list(raw)
    if start_key:
        start_idx = next(
            (i for i, item in enumerate(trimmed) if item.get("key") == start_key),
            None,
        )
        if start_idx is None:
            logger.warning(
                "[page_memory.fine_hierarchy] start anchor not found for {}; "
                "keeping all candidates before end trim",
                section_path,
            )
            # Fallback: drop exact parent-title duplicates if present.
            trimmed = [item for item in trimmed if item.get("key") != start_key]
        else:
            trimmed = trimmed[start_idx + 1 :]

    if end_key:
        end_idx = next(
            (i for i, item in enumerate(trimmed) if item.get("key") == end_key),
            None,
        )
        if end_idx is None:
            logger.warning(
                "[page_memory.fine_hierarchy] end anchor {!r} not found for {}; "
                "skipping back trim",
                end_title,
                section_path,
            )
        else:
            trimmed = trimmed[:end_idx]

    return trimmed


def _run_hierarchy_on_candidates(
    *,
    candidates: list[dict[str, Any]],
    skeleton: SectionSkeleton,
    model_name: str | None,
    max_tokens: int,
    max_depth: int,
    trace_recorder: Any | None,
) -> list[SectionSkeleton] | None:
    """Run the page-memory hierarchy prompt and rebuild a nested skeleton tree.

    The LLM assigns *relative* levels (starting at 1) within this leaf.  We
    reconstruct parent/child nesting from those levels with a stack, so the
    fine structure (e.g. L1→L2→L3 inside the leaf) is preserved instead of
    being flattened, then offset every level by ``skeleton.level`` to make it
    absolute under the coarse leaf.
    """
    if not candidates:
        return None

    input_json = json.dumps(
        [
            {
                "id": cand["id"],
                "page": cand["page"],
                "prominence": cand.get("prominence"),
                "heading": cand["heading"],
            }
            for cand in candidates
        ],
        ensure_ascii=False,
        indent=2,
    )
    coarse_context = (
        f"title={skeleton.title}\n"
        f"path={skeleton.section_path}\n"
        f"pages={skeleton.start_page}-{skeleton.end_page}"
    )

    try:
        prompt, temperature, _top_p, prompt_max_tokens = build_prompt(
            "page-memory-hierarchy",
            input_json,
            "",
            paras={
                "max_depth": max_depth,
                "max_tokens": max_tokens,
                "coarse_context": coarse_context,
            },
        )
        resolved_model = (
            model_name
            or os.environ.get(
                "HIERARCHY_LLM_MODEL",
                os.environ.get("NORMOL_MODEL"),
            )
        )
        client, resolved_model = get_text_client(requested_model=resolved_model)
        answer = client.chat_completion(
            messages=[
                {"role": "system", "content": "you are a document structure expert"},
                {"role": "user", "content": prompt},
            ],
            model=resolved_model,
            max_tokens=prompt_max_tokens,
            temperature=temperature,
            usage_task="page_memory.hierarchy",
        )
        result = eval_response(answer)
    except Exception as exc:
        logger.warning(
            "[page_memory.fine_hierarchy] LLM failed for skeleton {}: {}",
            skeleton.section_path,
            exc,
        )
        return None

    levels_by_id = _parse_hierarchy_result(result, max_depth=max_depth)
    if not levels_by_id:
        logger.warning(
            "[page_memory.fine_hierarchy] empty hierarchy result for skeleton {}",
            skeleton.section_path,
        )
        return None

    # Keep candidate order (== id order == the sequence fed to the LLM).
    ordered: list[dict[str, Any]] = []
    candidate_by_id = {int(c["id"]): c for c in candidates}
    for row_id in sorted(levels_by_id):
        cand = candidate_by_id.get(row_id)
        if cand is None:
            continue
        rel_level = levels_by_id[row_id]
        if rel_level <= 0:
            continue
        ordered.append({
            "id": row_id,
            "rel_level": rel_level,
            "heading": str(cand["heading"]),
            "page": int(cand.get("page") or skeleton.start_page),
        })
    ordered.sort(key=lambda item: item["id"])

    if not ordered:
        return None

    # Stack-based nesting: a node hangs under the nearest preceding node whose
    # relative level is shallower (smaller rel_level).
    nodes: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []  # (rel_level, section_path)
    for item in ordered:
        rel_level = item["rel_level"]
        heading = item["heading"]
        while stack and stack[-1][0] >= rel_level:
            stack.pop()
        parent_path = stack[-1][1] if stack else skeleton.section_path
        section_path = append_document_path(parent_path, heading)
        stack.append((rel_level, section_path))
        nodes.append({
            "section_path": section_path,
            "parent_path": parent_path,
            "rel_level": rel_level,
            "abs_level": skeleton.level + rel_level,
            "title": heading,
            "start_page": item["page"],
        })

    # end_page: a section runs until the next section at the same-or-shallower
    # relative level starts; descendants are nested inside that span.
    deeper: list[SectionSkeleton] = []
    for idx, node in enumerate(nodes):
        end_page = skeleton.end_page
        for nxt in nodes[idx + 1:]:
            if nxt["rel_level"] <= node["rel_level"]:
                end_page = max(nxt["start_page"] - 1, node["start_page"])
                break
        deeper.append(SectionSkeleton(
            section_path=node["section_path"],
            level=node["abs_level"],
            start_page=node["start_page"],
            end_page=end_page,
            title=node["title"],
            parent_path=node["parent_path"],
            evidence={
                "source": "fine_hierarchy_vlm",
                "observed_page": node["start_page"],
                "parent_skeleton": skeleton.section_path,
                "llm_relative_level": node["rel_level"],
            },
        ))

    _record_trace_hierarchy(
        trace_recorder=trace_recorder,
        skeleton=skeleton,
        candidates=candidates,
        ordered=ordered,
    )
    return deeper or None


def _exclusive_end(skeletons: list[SectionSkeleton], index: int) -> int:
    skeleton = skeletons[index]
    later_starts = [
        skel.start_page
        for skel in skeletons
        if skel.start_page > skeleton.start_page
    ]
    if not later_starts:
        return skeleton.end_page
    return min(skeleton.end_page, min(later_starts) - 1)


def _title_key(title: str | None) -> str:
    normalized = re.sub(r"\s+", "", str(title or "")).casefold()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized


def _parse_hierarchy_result(result: Any, *, max_depth: int) -> dict[int, int]:
    if not isinstance(result, list):
        logger.warning(
            "[page_memory.fine_hierarchy] unexpected hierarchy response type: {}",
            type(result).__name__,
        )
        return {}
    parsed: dict[int, int] = {}
    for item in result:
        if not isinstance(item, dict):
            continue
        try:
            row_id = int(item["id"])
            level = int(item["level"])
        except (KeyError, TypeError, ValueError):
            continue
        if level < 1:
            continue
        parsed[row_id] = min(level, max_depth)
    return parsed


def _record_trace_hierarchy(
    *,
    trace_recorder: Any | None,
    skeleton: SectionSkeleton,
    candidates: list[dict[str, Any]],
    ordered: list[dict[str, Any]],
) -> None:
    if trace_recorder is None or not hasattr(trace_recorder, "record_stage"):
        return
    try:
        trace_recorder.record_stage(
            "C4b.fine_hierarchy.scope",
            page_info=page_scope_info(
                int(item.get("page") or 0) for item in candidates if item.get("page")
            ),
            variables={
                "parent": skeleton.to_dict(),
                "candidate_count": len(candidates),
                "candidates": candidates,
                "hierarchy": ordered,
            },
        )
    except Exception as exc:
        logger.debug(
            "[page_memory.fine_hierarchy] failed to record trace hierarchy: {}",
            exc,
        )
