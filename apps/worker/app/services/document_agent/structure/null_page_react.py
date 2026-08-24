"""Parent-first ReAct locate for every TOC node with ``printed_page=None``.

Each query uses normalized whole-line text only to nominate physical pages.
Candidate pages are then VLM-confirmed in non-overlapping 2/4/6/10 physical
windows. A text hit, including a single unique hit, never writes an override
without visual confirmation.
"""

from __future__ import annotations

import json
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    first_leaf_start_under,
)

# Planner grep rounds after the free seed full-title probe. Not a page count.
REACT_PLANNER_GREP_BUDGET = 5


def react_budget() -> int:
    """Return the planner grep-loop budget."""
    return int(REACT_PLANNER_GREP_BUDGET)


_REACT_INSTRUCTIONS = """\
Return exactly one strict json object and no other text.
Fields: action is one of grep, strip_header, strip_footer, give_up; query is a string.

Ordered query strategy (follow this order; skip a step only if already tried
or not applicable to the given title / parent path). Pattern-level only —
do not invent document-specific titles:
1. Derive the search line from the given title by removing leading number /
   letter / punctuation prefixes and trailing metadata qualifiers (document
   identifiers/codes, revision labels, and similar). Keep the semantic title
   body. Prefer that body over a metadata-only query when both are present.
2. When the parent path indicates appendices/annexes (or the title is a
   lettered appendix-style entry): grep the structural form
   "Appendix <letter>" using the letter taken from the title. Prefer this
   before inventing other phrases.
3. Only after the above: try other variants such as "Appendix <letter>" plus
   the title body, a shorter distinctive complete-line title variant, or
   another structural prefix (chapter / part / section / annex) when supported
   by the title or parent path.

Rules:
- Prefer queries specific enough to avoid running headers and passing mentions.
  Do not guess page numbers.
- If the last observation is no_line_hits, visual_rejected,
  empty_normalized_query, grep_tool_error, or duplicate_normalized_query, you
  must choose a different normalized query when using grep.
- Use strip_header or strip_footer at most once each when margin text causes
  false hits; either action re-runs the last query.
- Use give_up only when no useful untried query or strip action remains.
"""


def _react_history_item(item: dict[str, Any]) -> dict[str, Any]:
    """Planner-facing history only. Runtime audit fields stay on the attempt."""
    out: dict[str, Any] = {
        "action": item.get("action"),
        "query": item.get("query"),
        "normalized_query": item.get("normalized_query"),
        "hit_page_count": int(item.get("hit_page_count") or 0),
        "observation": item.get("observation"),
    }
    return out


def _next_located_bound(
    *,
    sibling_nodes: list[TitleNode],
    index: int,
    parent_titles: tuple[str, ...],
    overrides: dict[tuple[str, ...], TitleMatch],
) -> int | None:
    """Return the next sibling's own or earliest descendant start."""
    for later in sibling_nodes[index + 1 :]:
        path = (*parent_titles, later.title)
        own = overrides.get(path)
        if own is not None:
            return int(own.page)
        descendant = first_leaf_start_under(later, parent_titles, overrides)
        if descendant is not None:
            return int(descendant)
    return None


def _whole_line_grep(
    *,
    ctx: ToolContext,
    query: str,
    left: int,
    right: int,
    body_pages: list[int],
) -> tuple[str, str, list[int], int]:
    """Search only ``body_pages ∩ [left, right]`` using complete-line equality.

    Returns ``(status, normalized_query_or_error, hit_pages, line_match_count)``.
    """
    from app.services.document_agent.pdf_text import page_content_map
    from app.services.document_agent.tools.grep_text import grep_text

    scope_pages = [page for page in body_pages if left <= page <= right]
    if not scope_pages:
        return "ok", "", [], 0

    body_set = set(scope_pages)
    view = ctx.blackboard.page_text_search_view
    if view is not None:
        texts = {
            int(page): str(view[page])
            for page in scope_pages
            if page in view
        }
    else:
        full = page_content_map(ctx.blackboard.page_full_text_cache)
        texts = {
            int(page): str(full[page])
            for page in scope_pages
            if page in full
        }
    if not texts:
        return "ok", "", [], 0

    previous_view = ctx.blackboard.page_text_search_view
    ctx.blackboard.page_text_search_view = texts
    try:
        result = grep_text(
            ctx,
            {
                "query": query,
                "whole_line": True,
                "start_page": min(texts),
                "end_page": max(texts),
            },
        )
    finally:
        ctx.blackboard.page_text_search_view = previous_view

    if result.status != "ok":
        return "error", str(result.error or "grep.text failed"), [], 0
    payload = result.payload or {}
    hit_pages = sorted(
        {
            int(page)
            for page in (payload.get("hit_pages") or [])
            if int(page) in body_set
        }
    )
    return (
        "ok",
        str(payload.get("normalized_query") or ""),
        hit_pages,
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
    grep_loops_used: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    state = {
        "title": title,
        "parent_path": list(parent_titles),
        "physical_search_scope": [left, right],
        "grep_loops_remaining": max(0, budget - grep_loops_used),
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
            max_tokens=120,
            response_format={"type": "json_object"},
            usage_task="document_agent.null_page_title_react",
        )
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        logger.warning("[null_page_react] planner failed for {!r}: {}", title, exc)
        return None, {"error": f"planner failed: {exc}"}

    action = str(payload.get("action") or "").strip().lower()
    query = str(payload.get("query") or "").strip()
    if action not in {"grep", "give_up", "strip_header", "strip_footer"}:
        return None, {
            "error": f"unknown planner action: {action!r}",
            "usage": usage,
        }
    if action == "grep" and not query:
        return None, {"error": "planner returned empty grep query", "usage": usage}
    return {"action": action, "query": query}, {"usage": usage}


def _verify_section_beginning_pages(
    *,
    ctx: ToolContext,
    title: str,
    pages: list[int],
) -> tuple[str, int | None, str, int]:
    """Ask the VLM to select a section start from the supplied candidate pages."""
    from app.services.document_agent.calibration.prompts import (
        SECTION_START_ANSWER_KEYS,
        build_section_start_question,
        coerce_found,
        coerce_found_page,
    )
    from app.services.document_agent.tools.inspect_pages import inspect_pages

    result = inspect_pages(
        ctx,
        {
            "pages": pages,
            "page_cap": len(pages),
            "question": build_section_start_question(title),
            "answer_keys": SECTION_START_ANSWER_KEYS,
            "folder_name": "null_page_react_verify",
            "prefix": "verify",
            "usage_task": "document_agent.null_page_react_verify",
        },
    )
    tokens = int(result.tokens_used or 0)
    if result.status != "ok":
        return "error", None, str(result.error or "inspect.pages failed"), tokens
    fields = (result.payload or {}).get("fields") or {}
    found_page = coerce_found_page(fields.get("found_page"), pages=pages)
    selected = found_page if coerce_found(fields.get("found")) else None
    reason = str((result.payload or {}).get("answer") or "")
    return "ok", selected, reason, tokens


def _locate_with_react(
    *,
    path_titles: tuple[str, ...],
    title: str,
    left: int,
    right: int,
    body_pages: list[int],
    ctx: ToolContext,
) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
    budget = react_budget()
    attempts: list[dict[str, Any]] = []
    attempted_needles: set[str] = set()
    stripped: set[str] = set()
    last_grep_query: str | None = None
    visual_calls = 0
    grep_loops_used = 0
    planner_turn = 0
    max_planner_turns = budget + 2

    # Each node starts from stored page text; strip views never cross nodes.
    ctx.blackboard.page_text_search_view = None

    def _visual_confirm(
        *,
        query: str,
        hit_pages: list[int],
        attempt: dict[str, Any],
    ) -> TitleMatch | None:
        nonlocal visual_calls
        from app.services.document_agent.calibration.scan import (
            DEFAULT_WINDOW_SCHEDULE,
            progressive_page_windows,
        )

        checked_pages: list[int] = []
        rounds: list[dict[str, Any]] = []
        selected_page: int | None = None
        selected_reason = ""
        visual_error = False

        for physical_pages in progressive_page_windows(
            start_page=hit_pages[0],
            end_page=right,
            window_schedule=DEFAULT_WINDOW_SCHEDULE,
        ):
            candidate_pages = [
                page
                for page in hit_pages
                if physical_pages[0] <= page <= physical_pages[-1]
            ]
            round_row: dict[str, Any] = {
                "physical_window": [physical_pages[0], physical_pages[-1]],
                "candidate_pages": candidate_pages,
                "selected_page": None,
            }
            if not candidate_pages:
                rounds.append(round_row)
                continue

            visual_calls += 1
            checked_pages.extend(candidate_pages)
            status, found_page, reason, tokens = _verify_section_beginning_pages(
                ctx=ctx,
                title=title,
                pages=candidate_pages,
            )
            round_row.update(
                {
                    "status": status,
                    "selected_page": found_page,
                    "reason": reason,
                    "tokens_used": tokens,
                }
            )
            rounds.append(round_row)
            if status != "ok":
                visual_error = True
                selected_reason = reason
                break
            if found_page is None:
                selected_reason = reason
                continue
            selected_page = int(found_page)
            selected_reason = reason
            break

        attempt["visual_rounds"] = rounds
        attempt["visual_pages_checked"] = checked_pages
        attempt["visual_selected_page"] = selected_page
        attempt["visual_reason"] = selected_reason
        if selected_page is None:
            attempt["observation"] = (
                "visual_tool_error" if visual_error else "visual_rejected"
            )
            attempts.append(attempt)
            return None

        attempt["observation"] = "section_start_confirmed"
        attempts.append(attempt)
        return TitleMatch(
            page=selected_page,
            source="react_line_grep_vlm",
            matched_line=query,
            candidates=list(hit_pages),
            evidence={
                "accept": "react_line_grep_vlm",
                "null_page_react": True,
                "grep_loop": grep_loops_used,
                "normalized_query": attempt.get("normalized_query"),
                "candidate_pages": list(hit_pages),
                "visual_pages_checked": checked_pages,
                "visual_rounds": rounds,
                "post_strip": attempt.get("post_strip"),
                "seed_full_title": attempt.get("seed_full_title"),
            },
        )

    def _apply_grep_result(
        *,
        query: str,
        planner_turn_index: int,
        consume_budget: bool,
        allow_duplicate: bool,
        post_strip: str | None,
        planner_meta: dict[str, Any],
        seed_full_title: bool = False,
    ) -> TitleMatch | None:
        nonlocal grep_loops_used, last_grep_query

        status, needle_or_error, hit_pages, line_match_count = _whole_line_grep(
            ctx=ctx,
            query=query,
            left=left,
            right=right,
            body_pages=body_pages,
        )
        needle = needle_or_error if status == "ok" else ""
        if consume_budget and status == "ok" and needle and needle not in attempted_needles:
            grep_loops_used += 1
        attempt: dict[str, Any] = {
            "loop": planner_turn_index,
            "grep_loop": grep_loops_used,
            "action": "grep",
            "query": query,
            "normalized_query": needle,
            "hit_page_count": len(hit_pages),
            "hit_pages": hit_pages,
            "line_match_count": line_match_count,
            **planner_meta,
        }
        if post_strip is not None:
            attempt["post_strip"] = post_strip
        if seed_full_title:
            attempt["seed_full_title"] = True

        if status != "ok":
            attempt["observation"] = "grep_tool_error"
            attempt["error"] = needle_or_error
            attempts.append(attempt)
            return None
        if not needle:
            attempt["observation"] = "empty_normalized_query"
            attempts.append(attempt)
            return None
        if needle in attempted_needles and not allow_duplicate:
            attempt["observation"] = "duplicate_normalized_query"
            attempts.append(attempt)
            return None

        last_grep_query = query
        attempted_needles.add(needle)
        if not hit_pages:
            attempt["observation"] = (
                "post_strip_no_line_hits" if post_strip else "no_line_hits"
            )
            attempts.append(attempt)
            return None
        return _visual_confirm(query=query, hit_pages=hit_pages, attempt=attempt)

    try:
        seed_match = _apply_grep_result(
            query=title,
            planner_turn_index=0,
            consume_budget=False,
            allow_duplicate=False,
            post_strip=None,
            planner_meta={},
            seed_full_title=True,
        )
        if seed_match is not None:
            return seed_match, attempts, visual_calls, "react_line_grep_vlm"

        while grep_loops_used < budget and planner_turn < max_planner_turns:
            planner_turn += 1
            proposal, planner_meta = _propose_react_query(
                title=title,
                parent_titles=path_titles[:-1],
                left=left,
                right=right,
                attempts=attempts,
                budget=budget,
                grep_loops_used=grep_loops_used,
            )
            if proposal is None:
                attempts.append(
                    {
                        "loop": planner_turn,
                        "grep_loop": grep_loops_used,
                        "action": "planner_error",
                        "hit_page_count": 0,
                        "line_match_count": 0,
                        **planner_meta,
                    }
                )
                continue

            action = proposal["action"]
            if action == "give_up":
                attempts.append(
                    {
                        "loop": planner_turn,
                        "grep_loop": grep_loops_used,
                        **proposal,
                        "hit_page_count": 0,
                        "line_match_count": 0,
                        **planner_meta,
                    }
                )
                return None, attempts, visual_calls, "react_give_up"

            if action in {"strip_header", "strip_footer"}:
                from app.services.document_agent.tools.text_strip_margins import (
                    strip_footer,
                    strip_header,
                )

                which = "header" if action == "strip_header" else "footer"
                if which in stripped:
                    attempts.append(
                        {
                            "loop": planner_turn,
                            "grep_loop": grep_loops_used,
                            **proposal,
                            "hit_page_count": 0,
                            "line_match_count": 0,
                            "observation": f"duplicate_strip_{which}",
                            **planner_meta,
                        }
                    )
                    continue
                strip_fn = strip_header if which == "header" else strip_footer
                strip_result = strip_fn(
                    ctx,
                    {"start_page": left, "end_page": right},
                )
                stripped.add(which)
                payload = strip_result.payload or {}
                strip_ok = strip_result.status == "ok"
                attempts.append(
                    {
                        "loop": planner_turn,
                        "grep_loop": grep_loops_used,
                        **proposal,
                        "hit_page_count": 0,
                        "line_match_count": 0,
                        "observation": (
                            f"stripped_{which}"
                            if strip_ok
                            else f"strip_{which}_failed"
                        ),
                        "pages_updated": int(payload.get("pages_updated") or 0),
                        "strip_error": strip_result.error,
                        **planner_meta,
                    }
                )
                if not strip_ok or not last_grep_query:
                    continue

                from app.services.document_parser.structure.body_boundary import (
                    normalize_match_text,
                )

                prior_needle = normalize_match_text(last_grep_query)
                if prior_needle:
                    attempted_needles.discard(prior_needle)
                match = _apply_grep_result(
                    query=last_grep_query,
                    planner_turn_index=planner_turn,
                    consume_budget=False,
                    allow_duplicate=True,
                    post_strip=which,
                    planner_meta={},
                )
                if match is not None:
                    return match, attempts, visual_calls, "react_line_grep_vlm"
                continue

            match = _apply_grep_result(
                query=proposal["query"],
                planner_turn_index=planner_turn,
                consume_budget=True,
                allow_duplicate=False,
                post_strip=None,
                planner_meta=planner_meta,
            )
            if match is not None:
                return match, attempts, visual_calls, "react_line_grep_vlm"

        return None, attempts, visual_calls, "react_loop_limit"
    finally:
        ctx.blackboard.page_text_search_view = None


def locate_null_page_overrides(
    *,
    nodes: list[TitleNode],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    body_pages: list[int],
    ctx: ToolContext | None,
    structural_parent_paths: set[tuple[str, ...]] | None = None,
) -> tuple[dict[tuple[str, ...], TitleMatch], list[dict[str, Any]]]:
    """Locate all null-page nodes in parent-first DFS order.

    Left bound is the max confirmed page among all preorder predecessors
    (fallback: body start). Right bound is the earliest of the node's first
    located descendant, the next located sibling/subtree start, and the
    inherited scope right. No fixed page-window cap on either side.
    """
    if not nodes or not body_pages:
        return dict(match_overrides), []

    out = dict(match_overrides)
    parent_paths = structural_parent_paths or set()
    report: list[dict[str, Any]] = []
    cursor = int(body_pages[0])

    def walk(
        sibling_nodes: list[TitleNode],
        parent_titles: tuple[str, ...],
        scope_right: int,
    ) -> None:
        nonlocal cursor
        for index, node in enumerate(sibling_nodes):
            path_titles = (*parent_titles, node.title)
            next_bound = _next_located_bound(
                sibling_nodes=sibling_nodes,
                index=index,
                parent_titles=parent_titles,
                overrides=out,
            )
            subtree_right = (
                min(int(next_bound), int(scope_right))
                if next_bound is not None
                else int(scope_right)
            )
            first_descendant = (
                first_leaf_start_under(node, parent_titles, out)
                if node.children
                else None
            )
            probe_right = (
                min(int(first_descendant), subtree_right)
                if first_descendant is not None
                else subtree_right
            )
            probe_left = cursor

            is_parent = bool(node.children) or path_titles in parent_paths
            needs_probe = (
                node.printed_page is None
                and path_titles not in out
            )
            if needs_probe:
                entry: dict[str, Any] = {
                    "path_titles": list(path_titles),
                    "title": node.title,
                    "kind": "parent" if is_parent else "leaf",
                    "printed_page": None,
                    "search_scope": [probe_left, probe_right],
                    "result": "unresolved",
                    "page": None,
                    "accept": None,
                    "visual_verify_calls": 0,
                    "react_attempts": [],
                }
                scope_pages = [
                    page
                    for page in body_pages
                    if probe_left <= page <= probe_right
                ]
                if probe_right < probe_left:
                    entry["result"] = "skipped_bad_window"
                elif not scope_pages:
                    entry["result"] = "no_scope_pages"
                elif ctx is None:
                    entry["result"] = "unresolved_no_ctx"
                else:
                    match, attempts, visual_calls, result = _locate_with_react(
                        path_titles=path_titles,
                        title=node.title,
                        left=probe_left,
                        right=probe_right,
                        body_pages=body_pages,
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

            own_match = out.get(path_titles)
            if own_match is not None:
                cursor = max(cursor, int(own_match.page))

            if node.children:
                walk(node.children, path_titles, subtree_right)

    walk(nodes, (), body_pages[-1])
    located = sum(1 for row in report if row.get("page") is not None)
    logger.info(
        "[null_page_react] parent-first null-page ReAct: attempted={} "
        "located={} unresolved={} planner_budget={}",
        len(report),
        located,
        len(report) - located,
        react_budget(),
    )
    return out, report
