#!/usr/bin/env python3
# ruff: noqa: E402
"""TEMP: locate null-page TOC nodes with normalized grep ReAct + VLM.

Experiment only — patches production symbols for one run, then restores them.

Policy under test:
  - Prune only printed-page leaves that failed anchoring; keep null-page nodes.
  - Null-page parents and leaves use a bounded mini-ReAct search planner.
  - Loop budget and hit/visual budget both equal PROFILE TOC
    ``BOUNDARY_STEP_PAGES`` (currently 5).
  - ReAct grep uses the registered ``grep.text`` normalized-text tool.
  - hit_count > budget → too many; reflect and change query (no VLM).
  - hit_count in 1..budget → confirm one page at a time until accepted or budget used.
  - Under one parent, siblings are serial: left cursor advances on success;
    on first failure, remaining null siblings are skipped (no window reset).
  - Search scope ends at the next located sibling or the enclosing parent scope;
    there is no fixed 22-page cap and no peer-TOC homepage clip.

Usage:
  cd apps/worker
  uv run python scripts/page_memory/tmp_probe_null_page_leaves.py \\
    --file "/path/to/EN_Sydney Streets Code.pdf"
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path as _Path
from typing import Any, cast

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from loguru import logger

from _debug_pm_shared import (
    _build_debug_coordinator,
    base_argparser,
    load_anatomy_cache,
    load_stage0_into_coordinator,
    page_text_cache_path,
    require_file,
    resolve_anatomy_cache_path,
    resolve_paths,
    stage0_state_path,
    write_debug_json,
)

# Same constant as PROFILE TOC boundary / confirm batch size.
from app.services.document_agent.tools.extract_toc_with_boundaries import (
    BOUNDARY_STEP_PAGES,
)

REACT_BUDGET = int(BOUNDARY_STEP_PAGES)
_HISTORY_SAMPLE_PAGES = 3

_REACT_INSTRUCTIONS = """\
You are the search planner in a small ReAct loop. Propose one plain-text grep
query that may appear on the physical START page of a section. Grep collapses
whitespace/newlines to one space between non-CJK words, removes whitespace
adjacent to CJK, and matches case-insensitively. A separate visual check
confirms candidates one page at a time.

Return one strict json object:
{"action":"grep","query":"...","reason":"..."}
or, only when no useful untried query remains:
{"action":"give_up","query":"","reason":"..."}

General query tactics:
- Do not guess page numbers.
- Account for differences between TOC labels and body headings.
- Try removing numbering, lettering, punctuation, or decorative prefixes.
- When supported by the title or parent path, try a structural prefix such as
  chapter, part, section, annex, or appendix.
- Try a distinctive leading, middle, or trailing title phrase when the full
  title is unlikely to be printed verbatim.
- Prefer queries specific enough to avoid running headers and passing mentions.

Reflection rules (mandatory):
- Read previous_attempts. Reflect on hit_count and observation before answering.
- If the last observation is no_normalized_hits, too_many_hits, visual_rejected,
  or duplicate_normalized_query, you MUST change the query. Emitting the same
  query again (same text after whitespace/case normalization) is invalid.
- too_many_hits means hit_count exceeded the visual budget; narrow the query.
- no_normalized_hits means broaden, rephrase, add/drop a structural prefix, or
  try another title fragment.
- visual_rejected means the pages were not the section beginning; change the
  query rather than repeating it.

Generic cases:
- A TOC label like "B Safety requirements" under an appendices parent may be
  printed as "Appendix B", "Safety requirements", or both together.
- A TOC label like "4.2 Access control — Technical requirements" may be printed
  with the number removed or with only one distinctive title phrase.
"""


def _react_history_item(item: dict[str, Any]) -> dict[str, Any]:
    hit_pages = [int(page) for page in (item.get("hit_pages") or [])]
    return {
        "query": item.get("query"),
        "normalized_query": item.get("normalized_query"),
        "hit_count": int(item.get("hit_count") or len(hit_pages)),
        "sample_pages": hit_pages[:_HISTORY_SAMPLE_PAGES],
        "observation": item.get("observation"),
        "visual_selected_page": item.get("visual_selected_page"),
        "visual_reason": item.get("visual_reason"),
        "visual_pages_checked": item.get("visual_pages_checked"),
    }


def prune_unanchored_keep_null_pages(
    nodes: list[Any],
    *,
    match_overrides: dict[tuple[str, ...], Any],
) -> tuple[list[Any], int]:
    """Drop only printed-page leaves that never got a physical override."""
    from app.services.document_agent.structure.hierarchy_locator import TitleNode

    removed = 0

    def _prune(node: TitleNode, parent_titles: tuple[str, ...]) -> TitleNode | None:
        nonlocal removed
        path = (*parent_titles, node.title)
        if node.children:
            children: list[TitleNode] = []
            for child in node.children:
                kept = _prune(child, path)
                if kept is not None:
                    children.append(kept)
            if children:
                return replace(node, children=children)
            if path in match_overrides or node.printed_page is None:
                return replace(node, children=[])
            removed += 1
            return None
        if path in match_overrides or node.printed_page is None:
            return node
        removed += 1
        return None

    out: list[TitleNode] = []
    for node in nodes:
        kept = _prune(node, ())
        if kept is not None:
            out.append(kept)
    if removed:
        logger.info(
            "[tmp.null_leaf] pruned {} printed-page unanchored leaves "
            "(null-page nodes kept)",
            removed,
        )
    return out, removed


def _next_located_bound(
    *,
    sibling_nodes: list[Any],
    index: int,
    parent_titles: tuple[str, ...],
    overrides: dict[tuple[str, ...], Any],
) -> int | None:
    from app.services.document_agent.structure.hierarchy_locator import (
        first_leaf_start_under,
    )

    for later in sibling_nodes[index + 1 :]:
        path = (*parent_titles, later.title)
        if path in overrides:
            return int(overrides[path].page)
        bound = first_leaf_start_under(later, parent_titles, overrides)
        if bound is not None:
            return int(bound)
    return None


def _infer_offset(
    nodes: list[Any],
    match_overrides: dict[tuple[str, ...], Any],
) -> int:
    from collections import Counter

    from app.services.document_agent.structure.hierarchy_locator import (
        iter_leaf_title_nodes,
    )

    diffs: list[int] = []
    for path, node in iter_leaf_title_nodes(nodes):
        if node.printed_page is None or path not in match_overrides:
            continue
        diffs.append(int(match_overrides[path].page) - int(node.printed_page))
    if not diffs:
        return 0
    return int(Counter(diffs).most_common(1)[0][0])


def _normalized_grep(
    *,
    ctx: Any,
    query: str,
    left: int,
    right: int,
) -> tuple[str, list[int], int]:
    from app.services.document_agent.tools.grep_text import grep_text

    result = grep_text(
        ctx,
        {
            "query": query,
            "start_page": left,
            "end_page": right,
        },
    )
    if result.status != "ok":
        return "", [], 0
    payload = result.payload or {}
    return (
        str(payload.get("normalized_query") or ""),
        [int(page) for page in (payload.get("hit_pages") or [])],
        int(payload.get("hit_count") or 0),
    )


def _propose_react_query(
    *,
    title: str,
    parent_titles: tuple[str, ...],
    left: int,
    right: int,
    attempts: list[dict[str, Any]],
    budget: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    state = {
        "toc_title": title,
        "parent_path": list(parent_titles),
        "physical_search_scope": [left, right],
        "react_budget": budget,
        "visual_budget": budget,
        "previous_attempts": [_react_history_item(item) for item in attempts],
    }
    prompt = (
        f"{_REACT_INSTRUCTIONS}\n\nCurrent state:\n"
        f"{json.dumps(state, ensure_ascii=False)}"
    )

    try:
        from shared.services.ai.llm_overrides import get_text_client

        client, model = get_text_client()
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": prompt}]),
            model=model,
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
            usage_task="document_agent.null_page_title_react",
        )
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        logger.warning("[tmp.null_react] planner failed for {!r}: {}", title, exc)
        return None, {"error": f"planner failed: {exc}"}

    action = str(payload.get("action") or "").strip().lower()
    query = str(payload.get("query") or "").strip()
    if action not in {"grep", "give_up"}:
        return None, {
            "error": f"unknown planner action: {action!r}",
            "usage": usage,
        }
    if action == "grep" and not query:
        return None, {"error": "planner returned empty grep query", "usage": usage}
    return (
        {
            "action": action,
            "query": query,
            "reason": str(payload.get("reason") or ""),
        },
        {"usage": usage},
    )


def _verify_section_beginning_page(
    *,
    ctx: Any,
    title: str,
    page: int,
    query: str,
) -> tuple[bool, str, int]:
    """Confirm one physical page as the section beginning. Returns (ok, reason, tokens)."""
    from app.services.document_agent.calibration.prompts import (
        coerce_found,
        coerce_found_page,
    )
    from app.services.document_agent.tools.inspect_pages import inspect_pages

    question = (
        f"Does this page mark the physical BEGINNING of the document section "
        f"corresponding to the TOC entry {title!r}? A cover page, section "
        "title page, or first body-heading page can be the beginning. Allow "
        "equivalent wording and added or omitted numbering, lettering, or "
        "structural prefixes. Do not accept a table-of-contents line, running "
        "header or footer, passing mention, or continuation page. "
        f"The normalized text query that nominated this page was {query!r}. "
        "Report the physical page number printed in the page label above the image."
    )
    verify_result = inspect_pages(
        ctx,
        {
            "pages": [page],
            "page_cap": 1,
            "question": question,
            "answer_keys": {
                "found": (
                    "boolean, true only when this page is the physical beginning "
                    "of the requested section"
                ),
                "found_page": (
                    "number|null, the physical page number where the section begins"
                ),
            },
            "folder_name": "null_page_react_verify",
            "prefix": "verify",
            "usage_task": "document_agent.null_page_react_verify",
        },
    )
    tokens = int(verify_result.tokens_used or 0)
    if verify_result.status != "ok":
        return False, str(verify_result.error or "inspect.pages failed"), tokens
    fields = (verify_result.payload or {}).get("fields") or {}
    found_page = coerce_found_page(fields.get("found_page"), pages=[page])
    ok = coerce_found(fields.get("found")) and found_page == page
    reason = str((verify_result.payload or {}).get("answer") or "")
    return ok, reason, tokens


def _locate_with_react(
    *,
    path_titles: tuple[str, ...],
    title: str,
    left: int,
    right: int,
    ctx: Any,
) -> tuple[Any | None, list[dict[str, Any]], int, str]:
    from app.services.document_agent.structure.hierarchy_locator import TitleMatch

    budget = REACT_BUDGET
    attempts: list[dict[str, Any]] = []
    attempted_needles: set[str] = set()
    visual_calls = 0
    visual_remaining = budget

    for loop_index in range(1, budget + 1):
        proposal, planner_meta = _propose_react_query(
            title=title,
            parent_titles=path_titles[:-1],
            left=left,
            right=right,
            attempts=attempts,
            budget=budget,
        )
        if proposal is None:
            attempts.append(
                {
                    "loop": loop_index,
                    "action": "planner_error",
                    "hit_count": 0,
                    "hit_pages": [],
                    **planner_meta,
                }
            )
            continue

        action = proposal["action"]
        if action == "give_up":
            attempts.append(
                {
                    "loop": loop_index,
                    **proposal,
                    "hit_count": 0,
                    "hit_pages": [],
                    **planner_meta,
                }
            )
            return None, attempts, visual_calls, "react_give_up"

        query = proposal["query"]
        needle, hit_pages, match_count = _normalized_grep(
            ctx=ctx,
            query=query,
            left=left,
            right=right,
        )
        attempt: dict[str, Any] = {
            "loop": loop_index,
            **proposal,
            "normalized_query": needle,
            "hit_count": len(hit_pages),
            "hit_pages": hit_pages,
            "match_count": match_count,
            "visual_budget_remaining_before": visual_remaining,
            **planner_meta,
        }
        if not needle or needle in attempted_needles:
            attempt["observation"] = "duplicate_normalized_query"
            attempts.append(attempt)
            continue
        attempted_needles.add(needle)

        if not hit_pages:
            attempt["observation"] = "no_normalized_hits"
            attempts.append(attempt)
            continue

        if len(hit_pages) > budget:
            attempt["observation"] = "too_many_hits"
            attempts.append(attempt)
            continue

        if visual_remaining <= 0:
            attempt["observation"] = "visual_budget_exhausted"
            attempts.append(attempt)
            continue

        checked: list[dict[str, Any]] = []
        selected: int | None = None
        last_reason = ""
        for page in hit_pages:
            if visual_remaining <= 0:
                break
            visual_remaining -= 1
            visual_calls += 1
            ok, reason, tokens = _verify_section_beginning_page(
                ctx=ctx,
                title=title,
                page=page,
                query=query,
            )
            checked.append(
                {
                    "page": page,
                    "confirmed": ok,
                    "reason": reason,
                    "tokens_used": tokens,
                }
            )
            last_reason = reason
            if ok:
                selected = page
                break

        attempt["visual_pages_checked"] = checked
        attempt["visual_selected_page"] = selected
        attempt["visual_reason"] = last_reason
        attempt["visual_budget_remaining_after"] = visual_remaining
        if selected is not None:
            attempt["observation"] = "section_start_confirmed"
            attempts.append(attempt)
            return (
                TitleMatch(
                    page=int(selected),
                    source="react_normalized_grep_vlm",
                    matched_line=query,
                    candidates=hit_pages,
                    evidence={
                        "accept": "react_normalized_grep_vlm",
                        "null_page_react": True,
                        "loop": loop_index,
                        "normalized_query": needle,
                        "visual_reason": last_reason,
                        "visual_pages_checked": [item["page"] for item in checked],
                    },
                ),
                attempts,
                visual_calls,
                "react_normalized_grep_vlm",
            )

        if visual_remaining <= 0 and len(checked) < len(hit_pages):
            attempt["observation"] = "visual_budget_exhausted"
        else:
            attempt["observation"] = "visual_rejected"
        attempts.append(attempt)

    return None, attempts, visual_calls, "react_loop_limit"


def locate_null_page_nodes_unified(
    *,
    nodes: list[Any],
    match_overrides: dict[tuple[str, ...], Any],
    page_texts: dict[int, str],
    body_pages: list[int],
    ctx: Any,
    offset: int | None = None,
) -> tuple[dict[tuple[str, ...], Any], list[dict[str, Any]]]:
    """Locate null-page nodes serially with normalized grep ReAct + VLM."""
    from app.services.document_agent.structure.hierarchy_locator import (
        TitleMatch,
        last_leaf_start_under,
    )

    if not nodes or not body_pages:
        return dict(match_overrides), []

    out = dict(match_overrides)
    body_set = set(body_pages)
    report: list[dict[str, Any]] = []
    primary_offset = (
        int(offset) if offset is not None else _infer_offset(nodes, out)
    )

    def _seed_printed(
        sibling_nodes: list[Any], parent_titles: tuple[str, ...]
    ) -> None:
        for node in sibling_nodes:
            path = (*parent_titles, node.title)
            if node.printed_page is not None and path not in out:
                page = int(node.printed_page) + primary_offset
                if page in body_set:
                    out[path] = TitleMatch(
                        page=page,
                        source="offset_seed",
                        matched_line="",
                        candidates=[page],
                        evidence={
                            "accept": "printed_plus_offset_seed",
                            "tmp_null_leaf_probe": True,
                        },
                    )
            if node.children:
                _seed_printed(node.children, path)

    def _skip_rest(
        sibling_nodes: list[Any],
        start_index: int,
        parent_titles: tuple[str, ...],
        failed_title: str,
    ) -> None:
        for later in sibling_nodes[start_index:]:
            path = (*parent_titles, later.title)
            if later.printed_page is not None or path in out:
                continue
            report.append(
                {
                    "path_titles": list(path),
                    "title": later.title,
                    "kind": "leaf" if not later.children else "parent",
                    "printed_page": None,
                    "search_scope": None,
                    "result": "skipped_after_sibling_failure",
                    "page": None,
                    "accept": None,
                    "visual_verify_calls": 0,
                    "react_attempts": [],
                    "failed_sibling": failed_title,
                }
            )

    def _probe_succeeded(entry: dict[str, Any]) -> bool:
        return entry.get("page") is not None

    _seed_printed(nodes, ())

    def walk(
        sibling_nodes: list[Any],
        parent_titles: tuple[str, ...],
        scope_start: int,
        scope_end: int,
    ) -> None:
        cursor = int(scope_start)
        for index, node in enumerate(sibling_nodes):
            path_titles = (*parent_titles, node.title)
            next_bound = _next_located_bound(
                sibling_nodes=sibling_nodes,
                index=index,
                parent_titles=parent_titles,
                overrides=out,
            )
            node_scope_end = (
                min(int(next_bound), scope_end)
                if next_bound is not None
                else int(scope_end)
            )

            if path_titles in out:
                cursor = max(cursor, int(out[path_titles].page))

            needs_probe = node.printed_page is None and path_titles not in out
            if needs_probe:
                is_leaf = not node.children
                entry: dict[str, Any] = {
                    "path_titles": list(path_titles),
                    "title": node.title,
                    "kind": "leaf" if is_leaf else "parent",
                    "printed_page": None,
                    "search_scope": None,
                    "result": "unresolved",
                    "page": None,
                    "accept": None,
                    "visual_verify_calls": 0,
                    "react_attempts": [],
                }

                left = int(cursor)
                right = int(node_scope_end)
                if right < left:
                    entry["result"] = "skipped_bad_window"
                    entry["search_scope"] = [left, right]
                    report.append(entry)
                    _skip_rest(
                        sibling_nodes, index + 1, parent_titles, node.title
                    )
                    return

                entry["search_scope"] = [left, right]
                match, attempts, visual_calls, result = _locate_with_react(
                    path_titles=path_titles,
                    title=node.title,
                    left=left,
                    right=right,
                    ctx=ctx,
                )
                entry["react_attempts"] = attempts
                entry["visual_verify_calls"] = visual_calls
                entry["result"] = result
                if match is not None:
                    out[path_titles] = match
                    entry["page"] = int(match.page)
                    entry["accept"] = match.evidence.get("accept")
                report.append(entry)

                if not _probe_succeeded(entry):
                    _skip_rest(
                        sibling_nodes, index + 1, parent_titles, node.title
                    )
                    return

                cursor = int(entry["page"])

            if node.children:
                child_scope_start = (
                    int(out[path_titles].page)
                    if path_titles in out
                    else cursor
                )
                walk(
                    node.children,
                    path_titles,
                    child_scope_start,
                    node_scope_end,
                )
                last_under = last_leaf_start_under(node, parent_titles, out)
                if last_under is not None:
                    cursor = max(cursor, int(last_under))
                elif path_titles in out:
                    cursor = max(cursor, int(out[path_titles].page))

    walk(nodes, (), body_pages[0], body_pages[-1])
    located = sum(1 for row in report if row.get("page") is not None)
    skipped = sum(
        1
        for row in report
        if row.get("result") == "skipped_after_sibling_failure"
    )
    logger.info(
        "[tmp.null_leaf] serial null-page ReAct: attempted={} located={} "
        "unresolved={} skipped_after_fail={} budget={}",
        len(report),
        located,
        sum(
            1
            for row in report
            if row.get("result")
            in {"react_give_up", "react_loop_limit", "planner_error"}
        ),
        skipped,
        REACT_BUDGET,
    )
    return out, report


def main() -> int:
    parser = base_argparser(
        "TEMP: null-page normalized grep ReAct + VLM (stop siblings on fail)"
    )
    args = parser.parse_args()

    from app.services.document_agent.calibration import procedure as procedure_mod
    from app.services.document_agent.structure import anchoring_primitives as ap
    from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
    from app.services.document_agent.validators import single_shard_plan
    from shared.core.config import settings

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    require_file(stage0_state_path(out_dir), hint="Run Stage 0 first")
    require_file(page_text_cache_path(out_dir), hint="Re-run Stage 0")
    require_file(anatomy_cache, hint="Run Stage 1 first")

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = int(anatomy.page_count or 0)
    hierarchies = list(getattr(anatomy, "toc_hierarchies", None) or [])

    original_prune = (
        procedure_mod.prune_unanchored_toc_leaves,
        ap.prune_unanchored_toc_leaves,
    )
    original_locate = (
        procedure_mod.locate_null_page_parent_overrides,
        ap.locate_null_page_parent_overrides,
    )

    def _patched_locate(*, nodes, match_overrides, page_texts, body_pages, ctx):
        return locate_null_page_nodes_unified(
            nodes=nodes,
            match_overrides=match_overrides,
            page_texts=page_texts,
            body_pages=body_pages,
            ctx=ctx,
            offset=None,
        )

    procedure_mod.prune_unanchored_toc_leaves = prune_unanchored_keep_null_pages
    ap.prune_unanchored_toc_leaves = prune_unanchored_keep_null_pages
    procedure_mod.locate_null_page_parent_overrides = _patched_locate
    ap.locate_null_page_parent_overrides = _patched_locate

    logger.info("█" * 70)
    logger.info("  TEMP null-page normalized grep ReAct — {}", filename)
    logger.info("  OUTPUT: {}", out_dir)
    logger.info("█" * 70)

    t0 = time.time()
    previous_image_model = settings.IMAGE_MODEL
    try:
        coordinator = _build_debug_coordinator(
            pdf_path=pdf_path,
            job_id=filename,
            out_dir=out_dir,
            model=None if args.no_vlm else args.model,
            settings_extra={"skip_toc_anchoring": False},
        )
        load_stage0_into_coordinator(coordinator, out_dir)
        bb = coordinator.blackboard
        bb.toc_result = anatomy.toc_result
        bb.toc_hierarchies = hierarchies
        bb.shard_plan = anatomy.shard_plan or single_shard_plan(page_count)
        bb.skeleton_anchor = None
        bb.skeleton_nodes = None
        bb.pending_skeleton_anchors = []

        run_toc_anchoring(coordinator.ctx)

        anchor = bb.skeleton_anchor or {}
        report = list(anchor.get("null_page_report") or [])
        overrides = dict(anchor.get("match_overrides") or {})
        react_hits = []
        for path, match in overrides.items():
            source = (
                match.get("source")
                if isinstance(match, dict)
                else getattr(match, "source", None)
            )
            if source != "react_normalized_grep_vlm":
                continue
            titles = path if isinstance(path, (list, tuple)) else (path,)
            page = (
                match.get("page")
                if isinstance(match, dict)
                else getattr(match, "page", None)
            )
            react_hits.append({"path": list(titles), "page": page})

        payload = {
            "policy": {
                "prune": "keep_null_page_nodes; drop printed-page unanchored leaves only",
                "probe": (
                    "normalized-grep ReAct; loop/hit/visual budget="
                    f"{REACT_BUDGET} (=BOUNDARY_STEP_PAGES); "
                    "hit_count>budget → too_many_hits + reflect; "
                    "else confirm one page at a time; "
                    "search next located sibling or enclosing parent scope; "
                    "serial under parent; stop siblings after first failure"
                ),
                "react_budget": REACT_BUDGET,
                "boundary_step_pages": BOUNDARY_STEP_PAGES,
            },
            "offset": anchor.get("offset"),
            "pruned_count": anchor.get("pruned_count"),
            "bulk_count": anchor.get("bulk_count"),
            "override_count": len(overrides),
            "null_page_report": report,
            "react_override_hits": react_hits,
            "elapsed_s": round(time.time() - t0, 2),
        }

        out_path = out_dir / "_doc_agent" / "tmp_null_page_leaf_probe.json"
        write_debug_json(out_path, payload)
        logger.info("wrote {}", out_path)
        logger.info(
            "null_page rows={} react_override_hits={}",
            len(report),
            len(react_hits),
        )
        for row in report:
            logger.info(
                "  [{}] {} search_scope={} result={} page={} loops={} failed_sibling={}",
                row.get("kind"),
                row.get("path_titles"),
                row.get("search_scope"),
                row.get("result"),
                row.get("page"),
                len(row.get("react_attempts") or []),
                row.get("failed_sibling"),
            )
        for hit in react_hits:
            logger.info("  OVERRIDE {} -> p{}", hit["path"], hit["page"])
    finally:
        procedure_mod.prune_unanchored_toc_leaves = original_prune[0]
        ap.prune_unanchored_toc_leaves = original_prune[1]
        procedure_mod.locate_null_page_parent_overrides = original_locate[0]
        ap.locate_null_page_parent_overrides = original_locate[1]
        if args.model:
            settings.IMAGE_MODEL = previous_image_model

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
