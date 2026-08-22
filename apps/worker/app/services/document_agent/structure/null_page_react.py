"""Bounded ReAct locate for TOC leaves with ``printed_page=None``.

After Phase-2 printed-page bulk/bisect, null-page leaves use a serial probe:
text LLM plans a ``grep.text`` query inside a sibling window, then
``inspect.pages`` confirms only the first hit page (binary section-start check).
Later hits from the same grep are discarded; a reject advances to the next
planner turn.

Budget (``REACT_PLANNER_GREP_BUDGET``):
- Seed full-title grep is free (does not consume the budget).
- Planner-chosen greps: at most ``REACT_PLANNER_GREP_BUDGET`` times.
- strip_header / strip_footer each auto re-grep the last query once for free.
- Planner turn cap = budget + 2 (room for the two strip actions).

No offset seed, no fixed page-count cap, and no fallback to normalized-strict
unique hit / RTL / ``scan_title_forward``.
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
    last_leaf_start_under,
)

# Planner grep rounds after the free seed full-title probe. Not a page count.
REACT_PLANNER_GREP_BUDGET = 5


def react_budget() -> int:
    """Planner grep-loop budget (and shared visual-confirm cap).

    Seed full-title grep does not consume this. Strip auto re-greps do not
    consume it. Value is ``REACT_PLANNER_GREP_BUDGET``, independent of TOC
    page-window constants.
    """
    return int(REACT_PLANNER_GREP_BUDGET)

_REACT_INSTRUCTIONS = """\
You are the search planner in a small ReAct loop. Propose the next action to
find the physical START page of a section. Grep collapses whitespace/newlines
to one space between non-CJK words, removes whitespace adjacent to CJK, and
matches case-insensitively. After each grep, a visual check confirms only the
first hit page (binary section-start). Later hits are discarded; a reject
means try a different query next.

The system already grepped the full TOC title once before this loop (see
previous_attempts). Do not repeat that exact full-title query.

Return one strict json object with action one of (no other keys):
{"action":"grep","query":"..."}
{"action":"strip_header","query":""}
{"action":"strip_footer","query":""}
{"action":"give_up","query":""}

Do not include a reason field. Use give_up only when no useful untried query
or strip remains.

Ordered query strategy after the automatic full-title grep (follow this order;
skip a step only if already tried or not applicable to the TOC title / parent
path). Pattern-level only — do not invent document-specific titles:
1. Remove the leading number / letter / punctuation prefix from the TOC title
   and grep the remaining title body.
2. When the parent path indicates appendices/annexes (or the TOC label is a
   lettered appendix-style entry): grep the structural form
   "Appendix <letter>" using the letter taken from the TOC label. Prefer this
   before inventing other phrases.
3. Only after the above: try other variants such as "Appendix <letter>" plus
   the title body, a shorter distinctive fragment of the title, or another
   structural prefix (chapter / part / section / annex) when supported by the
   title or parent path.
4. Prefer queries specific enough to avoid running headers and passing mentions.
   Do not guess page numbers.

Reflection rules (mandatory):
- Read previous_attempts. Reflect on hit_count and observation before answering.
- If the last observation is no_normalized_hits, visual_rejected,
  empty_normalized_query, grep_tool_error, or duplicate_normalized_query, you
  MUST change the query when choosing grep. Emitting the same grep query again
  (same text after whitespace/case normalization) is invalid for planner-chosen
  greps.
- Prefer strip_header or strip_footer when hits look scattered by running
  headers/footers. Each strip automatically re-greps the last query once for
  free (same action; do not spend a planner turn to repeat that query).
  Otherwise go to the next step in the ordered strategy (narrower / different
  query).
- strip_header / strip_footer only update a temporary search view; they do not
  change stored page text. They do NOT consume react_budget. Call each at most
  once per locate.
- Planner greps consume react_budget (grep_loops_remaining). The automatic
  full-title seed grep is free and does not consume it.
- no_normalized_hits / visual_rejected: advance to the next ordered strategy
  step rather than repeating the same query.
"""


def _react_history_item(item: dict[str, Any]) -> dict[str, Any]:
    hit_pages = [int(page) for page in (item.get("hit_pages") or [])]
    out = {
        "query": item.get("query"),
        "normalized_query": item.get("normalized_query"),
        "hit_count": int(item.get("hit_count") or len(hit_pages)),
        "observation": item.get("observation"),
        "visual_selected_page": item.get("visual_selected_page"),
        "visual_reason": item.get("visual_reason"),
        "visual_pages_checked": item.get("visual_pages_checked"),
    }
    if item.get("seed_full_title"):
        out["seed_full_title"] = True
    if item.get("post_strip"):
        out["post_strip"] = item.get("post_strip")
    return out


def _next_located_bound(
    *,
    sibling_nodes: list[TitleNode],
    index: int,
    parent_titles: tuple[str, ...],
    overrides: dict[tuple[str, ...], TitleMatch],
) -> int | None:
    for later in sibling_nodes[index + 1 :]:
        path = (*parent_titles, later.title)
        if path in overrides:
            return int(overrides[path].page)
        bound = first_leaf_start_under(later, parent_titles, overrides)
        if bound is not None:
            return int(bound)
    return None


def _normalized_grep(
    *,
    ctx: ToolContext,
    query: str,
    left: int,
    right: int,
    body_pages: list[int],
) -> tuple[str, str, list[int], int]:
    """Grep only ``body_pages ∩ [left, right]``.

    Returns ``(status, needle_or_error, hit_pages, match_count)``.
    ``status`` is ``"ok"`` or ``"error"``; on error the second field is a short
    error summary (not a normalized needle).
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

    prev_view = ctx.blackboard.page_text_search_view
    ctx.blackboard.page_text_search_view = texts
    try:
        result = grep_text(
            ctx,
            {
                "query": query,
                "start_page": min(texts),
                "end_page": max(texts),
            },
        )
    finally:
        ctx.blackboard.page_text_search_view = prev_view

    if result.status != "ok":
        return "error", str(result.error or "grep.text failed"), [], 0
    payload = result.payload or {}
    hit_pages = [
        int(page)
        for page in (payload.get("hit_pages") or [])
        if int(page) in body_set
    ]
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
        "toc_title": title,
        "parent_path": list(parent_titles),
        "physical_search_scope": [left, right],
        "react_budget": budget,
        "grep_loops_used": grep_loops_used,
        "grep_loops_remaining": max(0, budget - grep_loops_used),
        "visual_budget": budget,
        "previous_attempts": [_react_history_item(item) for item in attempts],
        "note": (
            "Full TOC title was already grepped automatically before this loop "
            "(free; does not consume react_budget). Planner greps consume "
            "react_budget. strip_header/strip_footer are free and each "
            "auto-retries the last grep query once for free."
        ),
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
    return (
        {
            "action": action,
            "query": query,
        },
        {"usage": usage},
    )


def _verify_section_beginning_page(
    *,
    ctx: ToolContext,
    title: str,
    page: int,
    query: str,
) -> tuple[bool, str, int]:
    """Confirm one physical page as the section beginning."""
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
    body_pages: list[int],
    ctx: ToolContext,
) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
    budget = react_budget()
    attempts: list[dict[str, Any]] = []
    attempted_needles: set[str] = set()
    visual_calls = 0
    visual_remaining = budget
    stripped: set[str] = set()
    last_grep_query: str | None = None
    # Each locate starts from stored content (no cross-node strip leakage).
    ctx.blackboard.page_text_search_view = None
    # Seed full-title grep is free. Planner greps consume budget (≤ budget).
    # strip_* each auto re-greps last query once for free.
    # +2 planner turns: room for strip_header and strip_footer actions.
    grep_loops_used = 0
    planner_turn = 0
    max_planner_turns = budget + 2

    def _visual_confirm(
        *,
        query: str,
        hit_pages: list[int],
        attempt: dict[str, Any],
    ) -> TitleMatch | None:
        """Confirm only the first hit page; discard the rest of this grep."""
        nonlocal visual_calls, visual_remaining
        if visual_remaining <= 0:
            attempt["observation"] = "visual_budget_exhausted"
            attempts.append(attempt)
            return None

        page = int(hit_pages[0])
        visual_remaining -= 1
        visual_calls += 1
        ok, reason, tokens = _verify_section_beginning_page(
            ctx=ctx,
            title=title,
            page=page,
            query=query,
        )
        checked = [
            {
                "page": page,
                "confirmed": ok,
                "reason": reason,
                "tokens_used": tokens,
            }
        ]
        attempt["visual_pages_checked"] = checked
        attempt["visual_selected_page"] = page if ok else None
        attempt["visual_reason"] = reason
        attempt["visual_budget_remaining_after"] = visual_remaining
        if ok:
            attempt["observation"] = "section_start_confirmed"
            attempts.append(attempt)
            return TitleMatch(
                page=page,
                source="react_normalized_grep_vlm",
                matched_line=query,
                candidates=[page],
                evidence={
                    "accept": "react_normalized_grep_vlm",
                    "null_page_react": True,
                    "loop": grep_loops_used,
                    "normalized_query": attempt.get("normalized_query"),
                    "visual_reason": reason,
                    "visual_pages_checked": [page],
                    "post_strip": attempt.get("post_strip"),
                    "seed_full_title": attempt.get("seed_full_title"),
                },
            )

        attempt["observation"] = "visual_rejected"
        attempts.append(attempt)
        return None

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
        """Grep + classify. Appends to attempts; returns match on visual confirm."""
        nonlocal grep_loops_used, last_grep_query

        status, needle_or_error, hit_pages, match_count = _normalized_grep(
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
            "hit_count": len(hit_pages),
            "hit_pages": hit_pages,
            "match_count": match_count,
            "visual_budget_remaining_before": visual_remaining,
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
                "post_strip_no_normalized_hits" if post_strip else "no_normalized_hits"
            )
            attempts.append(attempt)
            return None

        return _visual_confirm(query=query, hit_pages=hit_pages, attempt=attempt)

    try:
        # Free automatic probe: full TOC title (no planner, no react_budget).
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
            return seed_match, attempts, visual_calls, "react_normalized_grep_vlm"

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
                        "loop": planner_turn,
                        "grep_loop": grep_loops_used,
                        **proposal,
                        "hit_count": 0,
                        "hit_pages": [],
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
                            "hit_count": 0,
                            "hit_pages": [],
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
                        "hit_count": 0,
                        "hit_pages": [],
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

                # Same action: auto re-grep last query on stripped view (no budget).
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
                    return match, attempts, visual_calls, "react_normalized_grep_vlm"
                continue

            query = proposal["query"]
            match = _apply_grep_result(
                query=query,
                planner_turn_index=planner_turn,
                consume_budget=True,
                allow_duplicate=False,
                post_strip=None,
                planner_meta=planner_meta,
            )
            if match is not None:
                return match, attempts, visual_calls, "react_normalized_grep_vlm"

        return None, attempts, visual_calls, "react_loop_limit"
    finally:
        # Do not leak strip view across nodes / locate paths.
        ctx.blackboard.page_text_search_view = None


def locate_null_page_node_overrides(
    *,
    nodes: list[TitleNode],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    body_pages: list[int],
    ctx: ToolContext | None,
    structural_parent_paths: set[tuple[str, ...]] | None = None,
) -> tuple[dict[tuple[str, ...], TitleMatch], list[dict[str, Any]]]:
    """Locate null-page leaves with normalized grep ReAct + VLM.

    Grep reads ``ctx.blackboard.page_full_text_cache``. When ``ctx`` is None,
    every null-page leaf is recorded as unresolved (no text-unique fallback).

    ``structural_parent_paths`` are paths that had children *before*
    ``prune(keep_null=True)``. Empty-shell parents after prune must not be
    leaf-probed; they go to parent locate instead.
    """
    if not nodes or not body_pages:
        return dict(match_overrides), []

    parent_paths = structural_parent_paths or set()
    out = dict(match_overrides)
    report: list[dict[str, Any]] = []

    def _skip_entry(
        *,
        node: TitleNode,
        path: tuple[str, ...],
        result: str,
        failed_sibling: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path_titles": list(path),
            "title": node.title,
            "kind": "leaf",
            "printed_page": None,
            "search_scope": None,
            "result": result,
            "page": None,
            "accept": None,
            "visual_verify_calls": 0,
            "react_attempts": [],
        }
        if failed_sibling is not None:
            entry["failed_sibling"] = failed_sibling
        return entry

    def _record_unresolved_no_ctx(
        sibling_nodes: list[TitleNode],
        parent_titles: tuple[str, ...],
    ) -> None:
        for node in sibling_nodes:
            path = (*parent_titles, node.title)
            if (
                not node.children
                and path not in parent_paths
                and node.printed_page is None
                and path not in out
            ):
                report.append(
                    {
                        "path_titles": list(path),
                        "title": node.title,
                        "kind": "leaf",
                        "printed_page": None,
                        "search_scope": None,
                        "result": "unresolved_no_ctx",
                        "page": None,
                        "accept": None,
                        "visual_verify_calls": 0,
                        "react_attempts": [],
                    }
                )
            if node.children:
                _record_unresolved_no_ctx(node.children, path)

    if ctx is None:
        _record_unresolved_no_ctx(nodes, ())
        logger.info(
            "[null_page_react] ctx is None: {} null-page leaf/leaves unresolved "
            "(no LLM/VLM probe)",
            len(report),
        )
        return out, report

    def walk(
        sibling_nodes: list[TitleNode],
        parent_titles: tuple[str, ...],
        scope_start: int,
        scope_end: int,
    ) -> None:
        cursor = int(scope_start)
        # A failed leaf only invalidates the serial cursor for later leaves at
        # this level. Later parents keep their own walk (fresh flag).
        failed_sibling: str | None = None
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

            needs_probe = (
                not node.children
                and path_titles not in parent_paths
                and node.printed_page is None
                and path_titles not in out
            )
            if needs_probe:
                if failed_sibling is not None:
                    report.append(
                        _skip_entry(
                            node=node,
                            path=path_titles,
                            result="skipped_after_sibling_failure",
                            failed_sibling=failed_sibling,
                        )
                    )
                    continue

                entry: dict[str, Any] = {
                    "path_titles": list(path_titles),
                    "title": node.title,
                    "kind": "leaf",
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
                entry["search_scope"] = [left, right]
                scope_pages = [
                    page for page in body_pages if left <= page <= right
                ]
                if right < left:
                    entry["result"] = "skipped_bad_window"
                    report.append(entry)
                    failed_sibling = node.title
                    continue

                if not scope_pages:
                    entry["result"] = "no_scope_pages"
                    report.append(entry)
                    failed_sibling = node.title
                    continue

                match, attempts, visual_calls, result = _locate_with_react(
                    path_titles=path_titles,
                    title=node.title,
                    left=left,
                    right=right,
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

                if entry.get("page") is None:
                    failed_sibling = node.title
                    continue

                cursor = int(entry["page"])
                continue

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
    budget = react_budget()
    logger.info(
        "[null_page_react] serial null-page ReAct: attempted={} located={} "
        "unresolved={} skipped_after_fail={} budget={}",
        len(report),
        located,
        sum(
            1
            for row in report
            if row.get("result")
            in {
                "react_give_up",
                "react_loop_limit",
                "planner_error",
                "unresolved",
                "unresolved_no_ctx",
                "skipped_bad_window",
                "no_scope_pages",
            }
        ),
        skipped,
        budget,
    )
    return out, report
