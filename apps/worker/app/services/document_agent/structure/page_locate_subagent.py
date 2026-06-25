"""Per-title ReAct sub-agent for residual hierarchy page location.

This is intentionally NOT a fixed pipeline. For every title that strict anchoring
could not resolve, an LLM runs a bounded reasoning loop and decides — round by
round — which tool to call:

* ``grep.title_pages`` — find candidate body pages for a (possibly rewritten)
  query. The agent may shorten the title to a distinctive core when the full
  title carries trailing document-reference codes / ``《》`` wrappers / version
  notes that never appear contiguously in the body, or when the heading is split
  across lines.
* ``verify.section_page`` — render candidate pages and ask a VLM which one truly
  *starts* the section (vs a TOC row, header/footer, or a body citation).

The agent then ``submit``s a confirmed start page (or gives up). Tools are
dispatched through the shared registry, so the agent reuses the exact same
grep/VLM primitives the rest of the profile agent uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.structure.hierarchy_locator import TitleMatch

DecideFn = Callable[..., dict[str, Any] | None]

_ALLOWED_ACTIONS = {"grep", "verify", "submit", "give_up"}
VLM_MATCH_DEFAULT_CONFIDENCE = 0.7
HEURISTIC_MATCH_DEFAULT_CONFIDENCE = 0.55


@dataclass(frozen=True)
class SubAgentConfig:
    max_rounds: int = 6
    candidate_cap: int = 8
    verify_page_cap: int = 4
    decide_max_tokens: int = 500


@dataclass
class SubAgentResult:
    match: TitleMatch | None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    stop_reason: str = "exhausted"


class PageLocateSubAgent:
    """Bounded ReAct loop that locates ONE residual title via grep + VLM tools."""

    def __init__(
        self,
        *,
        ctx: ToolContext | None,
        scope_pages: list[int],
        page_count: int,
        config: SubAgentConfig | None = None,
        page_offset_hint: int | None = None,
        decide: DecideFn | None = None,
    ) -> None:
        self.ctx = ctx
        self.page_count = page_count
        self.scope_pages = sorted({p for p in scope_pages if 1 <= p <= page_count})
        self.config = config or SubAgentConfig()
        self.page_offset_hint = page_offset_hint
        self._decide = decide or decide_next_action
        # Ensure the page-locate tools are registered. Importing the light
        # ``structure`` module (not the heavy ``tools`` package) avoids pulling
        # in S3/DB-dependent modules and avoids import cycles.
        import app.services.document_agent.structure.page_locate_tools  # noqa: F401

    def locate(self, *, title: str, printed_page: int | None = None) -> SubAgentResult:
        if not self.scope_pages:
            return SubAgentResult(match=None, stop_reason="empty_scope")

        transcript: list[dict[str, Any]] = []
        seen_grep: dict[int, dict[str, Any]] = {}
        last_verify: dict[str, Any] | None = None

        for round_index in range(self.config.max_rounds):
            decision = self._decide(
                ctx=self.ctx,
                title=title,
                scope_pages=self.scope_pages,
                printed_page=printed_page,
                page_offset_hint=self.page_offset_hint,
                observations=transcript,
                seen_pages=sorted(seen_grep.keys()),
                last_verify=last_verify,
                round_index=round_index,
                max_rounds=self.config.max_rounds,
            )
            if decision is None:
                break

            action = str(decision.get("action") or "")
            entry: dict[str, Any] = {"round": round_index, "decision": decision}
            transcript.append(entry)

            if action == "grep":
                query = str(decision.get("query_title") or title).strip() or title
                pages = self._coerce_pages(decision.get("pages")) or self.scope_pages
                result = self._dispatch(
                    "grep.title_pages",
                    {
                        "title": query,
                        "pages": pages,
                        "candidate_cap": self.config.candidate_cap,
                    },
                )
                candidates = []
                if result.status == "ok" and result.payload:
                    candidates = list(result.payload.get("candidates") or [])
                for cand in candidates:
                    seen_grep[int(cand["page"])] = cand
                entry["observation"] = {
                    "tool": "grep.title_pages",
                    "query": query,
                    "candidates": candidates[: self.config.candidate_cap],
                }

            elif action == "verify":
                pages = self._coerce_pages(decision.get("pages")) or sorted(seen_grep.keys())
                pages = pages[: max(self.config.verify_page_cap, 1)]
                if not pages:
                    entry["observation"] = {
                        "tool": "verify.section_page",
                        "error": "no candidate pages to verify",
                    }
                    continue
                result = self._dispatch("verify.section_page", {"title": title, "pages": pages})
                choice = result.payload if (result.status == "ok" and result.payload) else {}
                last_verify = choice
                entry["observation"] = {
                    "tool": "verify.section_page",
                    "pages": pages,
                    "choice": choice,
                }

            elif action == "submit":
                selected = decision.get("selected_page")
                if selected is None:
                    return SubAgentResult(None, transcript, round_index + 1, "submit_null")
                page = int(selected)
                if page not in self.scope_pages:
                    entry["observation"] = {"error": f"submit page {page} outside scope"}
                    continue
                match = self._build_match(
                    title=title,
                    page=page,
                    seen_grep=seen_grep,
                    last_verify=last_verify,
                    decision=decision,
                    transcript=transcript,
                )
                return SubAgentResult(match, transcript, round_index + 1, "submit")

            elif action == "give_up":
                return SubAgentResult(None, transcript, round_index + 1, "give_up")

            else:
                entry["observation"] = {"error": f"unknown action {action!r}"}

        # Out of rounds / budget: accept a VLM-confirmed page if one exists.
        if last_verify and last_verify.get("selected_page") is not None:
            page = int(last_verify["selected_page"])
            if page in self.scope_pages:
                match = self._build_match(
                    title=title,
                    page=page,
                    seen_grep=seen_grep,
                    last_verify=last_verify,
                    decision={"confidence": last_verify.get("confidence")},
                    transcript=transcript,
                )
                return SubAgentResult(match, transcript, self.config.max_rounds, "round_cap_with_verify")
        return SubAgentResult(None, transcript, len(transcript), "exhausted")

    def _dispatch(self, name: str, args: dict[str, Any]):
        assert self.ctx is not None, "dispatch requires a non-None ToolContext"
        return REGISTRY.dispatch(name, self.ctx, args)

    def _coerce_pages(self, raw: Any) -> list[int]:
        if not isinstance(raw, (list, tuple)):
            return []
        pages: list[int] = []
        for value in raw:
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if page in self.scope_pages and page not in pages:
                pages.append(page)
        return pages

    def _build_match(
        self,
        *,
        title: str,
        page: int,
        seen_grep: dict[int, dict[str, Any]],
        last_verify: dict[str, Any] | None,
        decision: dict[str, Any],
        transcript: list[dict[str, Any]],
    ) -> TitleMatch:
        grep_hit = seen_grep.get(page) or {}
        vlm_confirmed = bool(last_verify and last_verify.get("selected_page") == page)
        if vlm_confirmed:
            assert last_verify is not None  # guaranteed by vlm_confirmed check
            source = cast(Any, last_verify.get("source") or "agent_vlm")
            confidence = float(last_verify.get("confidence") or VLM_MATCH_DEFAULT_CONFIDENCE)
            reason = last_verify.get("reason")
        else:
            source = cast(Any, "agent_heuristic")
            confidence = float(
                decision.get("confidence")
                or grep_hit.get("confidence")
                or HEURISTIC_MATCH_DEFAULT_CONFIDENCE
            )
            reason = decision.get("reason")
        return TitleMatch(
            page=page,
            confidence=confidence,
            source=source,
            matched_line=str(grep_hit.get("matched_line") or ""),
            score=float(grep_hit.get("score") or confidence),
            candidates=sorted(seen_grep.keys()),
            evidence={
                "page_locate_agent": {
                    "decision": source,
                    "selected_page": page,
                    "reason": reason,
                    "vlm_confirmed": vlm_confirmed,
                    "rounds": len(transcript),
                    "seen_pages": sorted(seen_grep.keys()),
                    "transcript": transcript[-8:],
                }
            },
        )


# ── Decision policy ────────────────────────────────────────────────────────


def decide_next_action(
    *,
    ctx: ToolContext | None,
    title: str,
    scope_pages: list[int],
    printed_page: int | None,
    page_offset_hint: int | None,
    observations: list[dict[str, Any]],
    seen_pages: list[int],
    last_verify: dict[str, Any] | None,
    round_index: int,
    max_rounds: int,
) -> dict[str, Any] | None:
    """Pick the next action.

    Primary path is LLM-driven (the agent may rewrite the query, expand scope,
    verify with a VLM, then submit). When no reasoning model is configured we
    fall back to a minimal deterministic policy (strict grep → verify → submit)
    that performs no query rewriting — only the LLM is allowed to relax the
    query, so offline mode never silently mis-anchors.
    """
    model = None
    if ctx is not None:
        model = ctx.settings.get("executor_model") or ctx.settings.get("model")
    if ctx is None or not model or getattr(ctx, "budget", None) is None:
        return _deterministic_decide(
            title=title,
            observations=observations,
            seen_pages=seen_pages,
            last_verify=last_verify,
        )

    from shared.utils.token_estimate import estimate_tokens

    prompt = _build_prompt(
        title=title,
        scope_pages=scope_pages,
        printed_page=printed_page,
        page_offset_hint=page_offset_hint,
        observations=observations,
        seen_pages=seen_pages,
        last_verify=last_verify,
        round_index=round_index,
        max_rounds=max_rounds,
    )
    est = estimate_tokens(prompt)
    if not ctx.budget.try_reserve("plan", est):
        logger.warning("[page_locate.subagent] planner budget exhausted for title={!r}", title)
        return None
    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            max_tokens=500,
            response_format={"type": "json_object"},
            usage_task="page_memory.page_locate_decide",
        )
        ctx.budget.commit("plan", actual=usage.get("total_tokens", est), est=est)
        return _normalize_decision(json.loads(raw))
    except Exception as exc:
        ctx.budget.refund("plan", est=est)
        logger.warning("[page_locate.subagent] decide failed for title={!r}: {}", title, exc)
        return None


def _deterministic_decide(
    *,
    title: str,
    observations: list[dict[str, Any]],
    seen_pages: list[int],
    last_verify: dict[str, Any] | None,
) -> dict[str, Any]:
    has_grep = any(
        entry.get("observation", {}).get("tool") == "grep.title_pages"
        for entry in observations
    )
    if not has_grep:
        return {"action": "grep", "query_title": title, "reason": "initial strict grep"}
    if last_verify is None:
        if seen_pages:
            return {"action": "verify", "pages": seen_pages, "reason": "verify grep candidates"}
        return {"action": "give_up", "reason": "strict grep found no candidates"}
    selected = last_verify.get("selected_page")
    if selected is not None:
        return {
            "action": "submit",
            "selected_page": selected,
            "confidence": last_verify.get("confidence", 0.6),
            "reason": "submit VLM-confirmed page",
        }
    return {"action": "give_up", "reason": "verify returned no page"}


def _normalize_decision(data: dict[str, Any]) -> dict[str, Any]:
    action = str(data.get("action") or "").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        action = "give_up"
    decision: dict[str, Any] = {"action": action, "reason": data.get("reason")}
    if action == "grep":
        decision["query_title"] = data.get("query_title")
        decision["pages"] = data.get("pages")
    elif action == "verify":
        decision["pages"] = data.get("pages")
    elif action == "submit":
        selected = data.get("selected_page")
        decision["selected_page"] = None if selected in (None, "", "null") else selected
        decision["confidence"] = data.get("confidence")
    return decision


def _build_prompt(
    *,
    title: str,
    scope_pages: list[int],
    printed_page: int | None,
    page_offset_hint: int | None,
    observations: list[dict[str, Any]],
    seen_pages: list[int],
    last_verify: dict[str, Any] | None,
    round_index: int,
    max_rounds: int,
) -> str:
    scope_desc = (
        f"{scope_pages[0]}-{scope_pages[-1]} ({len(scope_pages)} pages)"
        if scope_pages
        else "none"
    )
    hint = ""
    if printed_page is not None:
        guessed = printed_page + page_offset_hint if page_offset_hint is not None else None
        hint = f"\nPrinted page number in the title's TOC entry: {printed_page}" + (
            f" (rough physical-page guess: {guessed})" if guessed is not None else ""
        )
    compact_obs = json.dumps(
        [_compact_obs(entry) for entry in observations[-4:]],
        ensure_ascii=False,
    )
    return (
        "You are a sub-agent that locates the START page of ONE section title "
        "inside a PDF body. Decide the single next action and return strict JSON.\n\n"
        f"Title to locate: {title!r}\n"
        f"Allowed body pages: {scope_desc}{hint}\n"
        f"Round {round_index + 1} of {max_rounds}. "
        f"Candidate pages seen so far: {seen_pages}. "
        f"Last VLM verification: {json.dumps(last_verify, ensure_ascii=False)}\n"
        f"Observations (most recent last): {compact_obs}\n\n"
        "Available actions (pick exactly ONE, as a JSON object):\n"
        '1. {"action":"grep","query_title":"<text>","reason":"..."}\n'
        "   Searches body pages for the query (exact line, whitespace-insensitive "
        "compact text, and token overlap) and returns candidate pages with matched "
        "lines. IMPORTANT: the title may carry a trailing document-reference code "
        "that never appears contiguously in the body, and the heading may be split "
        "across two lines. If a strict search of the full title returns nothing, "
        "retry grep with a shortened, distinctive CORE of the title (drop the "
        "trailing brackets/codes/notes).\n"
        '2. {"action":"verify","pages":[<int>,...],"reason":"..."}\n'
        "   Renders those pages as images and asks a vision model which one truly "
        "STARTS the section (not a table-of-contents row, a running header/footer, "
        "or a body mention/citation). Use this to disambiguate.\n"
        '3. {"action":"submit","selected_page":<int|null>,"confidence":<0..1>,"reason":"..."}\n'
        "   Finalize. selected_page must be a page you have already seen as a "
        "candidate; use null only if the section is genuinely absent.\n"
        '4. {"action":"give_up","reason":"..."}\n\n'
        "Rules: when there is more than one candidate or any ambiguity, verify with "
        "the vision model before submitting. Never invent page numbers. "
        "Return ONLY the JSON object."
    )


def _compact_obs(entry: dict[str, Any]) -> dict[str, Any]:
    obs = entry.get("observation") or {}
    tool = obs.get("tool")
    if tool == "grep.title_pages":
        return {
            "action": "grep",
            "query": obs.get("query"),
            "candidates": [
                {"page": c.get("page"), "source": c.get("source"), "line": (c.get("matched_line") or "")[:60]}
                for c in (obs.get("candidates") or [])
            ],
        }
    if tool == "verify.section_page":
        choice = obs.get("choice") or {}
        return {
            "action": "verify",
            "pages": obs.get("pages"),
            "selected_page": choice.get("selected_page"),
            "source": choice.get("source"),
            "reason": (choice.get("reason") or "")[:80],
        }
    return {"action": entry.get("decision", {}).get("action"), "note": obs.get("error")}
