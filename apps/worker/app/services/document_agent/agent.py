"""DocumentAnatomyAgent — produce a PageMap for a PDF via LLM tool-calling.

Architecture
------------
The agent holds a **minimal tool set** — only operations that genuinely require
structured data from the PDF are exposed as tools.  Classification and
heuristic logic run deterministically inside the agent; only the final
cut-point decision is delegated to the LLM (which has full context by then).

Two-phase tool calling:
1. ``scan_all_page_features`` — collects per-page structural signals.
2. ``find_h1_boundaries`` — locates level-1 headings via text search.

The LLM then reasons over the collected evidence to call
``propose_cut_points``, producing the final shard plan.

Design constraints
------------------
- No hardcoded page counts, thresholds, or prompt examples.
- Every parameter that influences splitting comes from ``settings`` or is
  passed explicitly by the caller.
- Graceful degradation at every step: tools return empty/safe results rather
  than raising; the agent falls back to deterministic cuts if the LLM fails.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from typing import Any

from app.services.document_agent.page_map import (
    CutPoint,
    H1BoundaryResult,
    H1Match,
    PageFeature,
    PageMap,
    Shard,
)
from app.services.document_agent.tools.classify_special_pages import (
    heuristic_classify_special_pages,
)
from app.services.document_agent.tools.find_h1_boundaries import find_h1_boundaries
from app.services.document_agent.tools.scan_all_page_features import (
    scan_all_page_features,
)
from loguru import logger


# ── Tool schemas ───────────────────────────────────────────────────────────────

_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "scan_all_page_features",
            "description": (
                "Perform a full structural scan of every page in the PDF. "
                "Returns per-page signals: text length, image coverage, table count, "
                "orientation, blank-page flag, and a text preview. "
                "Always call this first."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_h1_boundaries",
            "description": (
                "Locate level-1 headings in the document body by grepping page "
                "texts against TOC entries.  Returns the page numbers where each "
                "level-1 heading physically starts.  Call after scan_all_page_features."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_cut_points",
            "description": (
                "Propose a list of shard boundaries for the document. "
                "Each cut_point marks where one shard ends (the next starts at "
                "cut_after_page + 1). "
                "Constraints you must respect:\n"
                "- Shards may not exceed max_pages_per_shard pages.\n"
                "- Shards should be at least min_pages_per_shard pages.\n"
                "- Do not cut through landscape blocks or table-heavy pages.\n"
                "- Prefer h1_heading boundaries; fall back to blank/sparse pages; "
                "use forced cuts only as a last resort.\n"
                "- If the document is short enough that no split is needed, "
                "return an empty cut_points list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cut_points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cut_after_page": {"type": "integer"},
                                "anchor_type": {
                                    "type": "string",
                                    "enum": [
                                        "h1_heading",
                                        "blank",
                                        "sparse",
                                        "forced",
                                    ],
                                },
                                "rationale": {"type": "string"},
                                "confidence": {"type": "number"},
                            },
                            "required": [
                                "cut_after_page",
                                "anchor_type",
                                "rationale",
                            ],
                        },
                    }
                },
                "required": ["cut_points"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": "Signal that cut planning is complete. Call last.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _build_system_prompt(
    split_threshold: int,
    max_pages_per_shard: int,
    min_pages_per_shard: int,
) -> str:
    return (
        "You are a Document Anatomy Agent. Your task is to analyse a PDF and "
        "decide how to split it into semantically coherent shards for downstream "
        "hierarchical heading extraction.\n\n"
        "Call tools in this order:\n"
        "1. scan_all_page_features — always first.\n"
        "2. find_h1_boundaries — always second.\n"
        "3. propose_cut_points — reason carefully over the evidence:\n"
        f"   - Only propose cuts when the document exceeds {split_threshold} pages.\n"
        f"   - Each shard: between {min_pages_per_shard} and {max_pages_per_shard} pages.\n"
        "   - Prefer h1_heading anchors (level-1 heading start pages).\n"
        "   - Fall back to blank or sparse pages when headings are ambiguous.\n"
        "   - Use forced cuts only when no semantic boundary is available; "
        "avoid cutting through landscape blocks or dense table regions.\n"
        "4. finalize — always last.\n\n"
        "Be concise. Do not repeat data already returned by tools."
    )


# ── Agent ──────────────────────────────────────────────────────────────────────


class DocumentAnatomyAgent:
    """LLM-orchestrated agent producing a ``PageMap`` for a PDF.

    Parameters
    ----------
    model_name:
        Override the LLM model.  Falls back to ``settings.HIERARCHY_LLM_MODEL``
        then ``settings.NORMOL_MODEL``.
    split_threshold:
        Minimum page count before physical splitting is considered.  If
        ``None``, read from ``settings.PDF_ANATOMY_SPLIT_THRESHOLD`` (default
        behaviour); the caller can pass an explicit value for testing.
    max_pages_per_shard:
        Hard cap on shard size.  Same settings fallback pattern.
    min_pages_per_shard:
        Prevent micro-shards that would cause overhead without benefit.
    max_iterations:
        Hard limit on the LLM tool-calling loop to prevent runaway calls.
    """

    def __init__(
        self,
        model_name: str | None = None,
        split_threshold: int | None = None,
        max_pages_per_shard: int | None = None,
        min_pages_per_shard: int | None = None,
        max_iterations: int = 10,
    ) -> None:
        self._model_name = model_name
        self._split_threshold = split_threshold
        self._max_pages_per_shard = max_pages_per_shard
        self._min_pages_per_shard = min_pages_per_shard
        self._max_iterations = max_iterations

        # Per-run state (reset by run())
        self._pdf_path: str = ""
        self._page_features: list[PageFeature] = []
        self._page_labels: list[dict[str, Any]] = []
        self._h1_result: H1BoundaryResult | None = None
        self._cut_points: list[CutPoint] = []
        self._decision_log: list[str] = []
        self._finalized: bool = False

    # ── Threshold resolution (defer to settings to avoid hardcoding) ───────────

    def _resolve_thresholds(self) -> tuple[int, int, int]:
        """Return (split_threshold, max_per_shard, min_per_shard) from settings."""
        try:
            from shared.core.config import settings

            split = self._split_threshold or getattr(
                settings, "PDF_ANATOMY_SPLIT_THRESHOLD", 200
            )
            max_s = self._max_pages_per_shard or getattr(
                settings, "PDF_ANATOMY_MAX_PAGES_PER_SHARD", 200
            )
            min_s = self._min_pages_per_shard or getattr(
                settings, "PDF_ANATOMY_MIN_PAGES_PER_SHARD", 20
            )
        except Exception:
            split = self._split_threshold or 200
            max_s = self._max_pages_per_shard or 200
            min_s = self._min_pages_per_shard or 20
        return int(split), int(max_s), int(min_s)

    def _resolve_model(self) -> str:
        try:
            from shared.core.config import settings

            return (
                self._model_name
                or getattr(settings, "HIERARCHY_LLM_MODEL", None)
                or getattr(settings, "NORMOL_MODEL", None)
                or "deepseek-chat"
            )
        except Exception:
            return self._model_name or "deepseek-chat"

    # ── Public entry point ─────────────────────────────────────────────────────

    def run(self, pdf_path: str, job_id: str) -> PageMap:
        """Run the anatomy agent; always returns a valid ``PageMap``."""
        self._reset(pdf_path)
        split_threshold, max_per_shard, min_per_shard = self._resolve_thresholds()
        logger.info(
            f"[DocumentAnatomyAgent] start '{os.path.basename(pdf_path)}' "
            f"split_threshold={split_threshold} max_shard={max_per_shard}"
        )
        try:
            page_map = self._run_loop(
                job_id, split_threshold, max_per_shard, min_per_shard
            )
        except Exception as exc:
            logger.error(
                f"[DocumentAnatomyAgent] unrecoverable error: {exc}\n"
                + traceback.format_exc()
            )
            page_map = self._fallback_page_map(job_id, reason=str(exc))

        logger.info(
            f"[DocumentAnatomyAgent] done: {page_map.page_count} pages, "
            f"needs_split={page_map.needs_split}, shards={len(page_map.shards)}"
        )
        return page_map

    # ── Internal state ─────────────────────────────────────────────────────────

    def _reset(self, pdf_path: str) -> None:
        self._pdf_path = pdf_path
        self._page_features = []
        self._page_labels = []
        self._h1_result = None
        self._cut_points = []
        self._decision_log = []
        self._finalized = False

    def _log(self, msg: str) -> None:
        logger.info(f"[DocumentAnatomyAgent] {msg}")
        self._decision_log.append(msg)

    # ── Tool dispatch ──────────────────────────────────────────────────────────

    def _tool_scan_all_page_features(self, _args: dict) -> dict[str, Any]:
        self._log("→ scan_all_page_features")
        features = scan_all_page_features(self._pdf_path)
        self._page_features = features

        # Run heuristic classification immediately (deterministic, no LLM cost)
        probe_fmt = [
            {
                "page_number": f.page,
                "text_length": f.text_length,
                "image_coverage": f.image_coverage,
                "table_count": f.table_count,
                "drawings_count": f.drawings_count,
                "orientation": f.orientation,
                "is_blank_like": f.is_blank_like,
                "text_preview": f.text_preview,
            }
            for f in features
        ]
        labels = heuristic_classify_special_pages(probe_fmt)
        self._page_labels = labels.get("pages") or []

        # Return a compact summary for the LLM context (not the full feature list)
        counts: dict[str, int] = {}
        for lbl in self._page_labels:
            kind = str(lbl.get("special_kind") or "normal")
            counts[kind] = counts.get(kind, 0) + 1
        self._log(
            f"  {len(features)} pages | "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        return {
            "page_count": len(features),
            "page_type_counts": counts,
            # Pass sparse structural summary so LLM can reason without wall of text
            "page_summary": [
                {
                    "page": f.page,
                    "kind": next(
                        (
                            lbl.get("special_kind", "normal")
                            for lbl in self._page_labels
                            if lbl.get("page") == f.page
                        ),
                        "normal",
                    ),
                    "orientation": f.orientation,
                    "is_blank": f.is_blank_like,
                }
                for f in features
            ],
        }

    def _tool_find_h1_boundaries(self, _args: dict) -> dict[str, Any]:
        self._log("→ find_h1_boundaries")
        result = find_h1_boundaries(self._pdf_path, self._page_features)
        self._h1_result = result
        self._log(
            f"  method={result.method}, "
            f"toc_pages={result.toc_pages}, "
            f"h1_matches={len(result.h1_matches)}"
        )
        return result.to_dict()

    def _tool_propose_cut_points(self, args: dict) -> dict[str, Any]:
        self._log("→ propose_cut_points")
        raw_cuts = args.get("cut_points") or []
        parsed: list[CutPoint] = []
        for raw in raw_cuts:
            try:
                parsed.append(
                    CutPoint(
                        cut_after_page=int(raw["cut_after_page"]),
                        anchor_type=raw.get("anchor_type", "forced"),
                        rationale=str(raw.get("rationale", ""))[:400],
                        confidence=float(raw.get("confidence", 1.0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._cut_points = sorted(parsed, key=lambda c: c.cut_after_page)
        self._log(f"  {len(self._cut_points)} cuts: {[c.cut_after_page for c in self._cut_points]}")
        return {"accepted": len(self._cut_points)}

    def _tool_finalize(self, _args: dict) -> dict[str, Any]:
        self._log("→ finalize")
        self._finalized = True
        return {"status": "ok"}

    _DISPATCH: dict[str, Any] = {
        "scan_all_page_features": _tool_scan_all_page_features,
        "find_h1_boundaries": _tool_find_h1_boundaries,
        "propose_cut_points": _tool_propose_cut_points,
        "finalize": _tool_finalize,
    }

    def _dispatch(self, name: str, args: dict) -> dict[str, Any]:
        handler = self._DISPATCH.get(name)
        if handler is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return handler(self, args)
        except Exception as exc:
            logger.warning(f"[DocumentAnatomyAgent] tool '{name}' error: {exc}")
            return {"error": str(exc)}

    # ── LLM loop ───────────────────────────────────────────────────────────────

    def _run_loop(
        self,
        job_id: str,
        split_threshold: int,
        max_per_shard: int,
        min_per_shard: int,
    ) -> PageMap:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        model = self._resolve_model()
        client = get_openai_client(model=model)
        system_prompt = _build_system_prompt(split_threshold, max_per_shard, min_per_shard)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Analyse and plan shards for: {os.path.basename(self._pdf_path)}"
                ),
            },
        ]

        for iteration in range(self._max_iterations):
            self._log(f"iteration {iteration + 1}/{self._max_iterations}")
            try:
                response = client.chat_completion(
                    messages=messages,
                    model=model,
                    temperature=0.0,
                    max_tokens=2000,
                    tools=_TOOL_SCHEMAS,
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.error(f"[DocumentAnatomyAgent] LLM call failed: {exc}")
                break

            tool_calls = _parse_tool_calls(response)
            if not tool_calls:
                self._log("no tool calls — exiting loop")
                break

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"], ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls:
                result = self._dispatch(tc["name"], tc["args"])
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

            if self._finalized:
                self._log("finalized — exiting loop")
                break
        else:
            self._log(f"reached max_iterations={self._max_iterations}")

        # Ensure data collection happened even if LLM skipped a step
        if not self._page_features:
            self._log("fallback: direct scan_all_page_features")
            self._tool_scan_all_page_features({})

        if self._h1_result is None:
            self._log("fallback: direct find_h1_boundaries")
            self._tool_find_h1_boundaries({})

        page_count = len(self._page_features)

        # Deterministic fallback cuts if LLM proposed nothing and doc is long
        if not self._cut_points and page_count > split_threshold:
            self._log("no cuts proposed — applying deterministic fallback")
            self._cut_points = _deterministic_cuts(
                page_count=page_count,
                page_labels=self._page_labels,
                h1_result=self._h1_result,
                max_per_shard=max_per_shard,
                min_per_shard=min_per_shard,
            )

        return self._assemble_page_map(job_id, page_count)

    # ── PageMap assembly ───────────────────────────────────────────────────────

    def _assemble_page_map(self, job_id: str, page_count: int) -> PageMap:
        shards = _cuts_to_shards(self._cut_points, page_count)
        h1_result = self._h1_result or H1BoundaryResult(
            toc_pages=[], h1_matches=[], method="none"
        )
        return PageMap(
            job_id=job_id,
            file_path=self._pdf_path,
            page_count=page_count,
            h1_result=h1_result,
            page_features=self._page_features,
            shards=shards,
            needs_split=len(shards) > 1,
            global_signals=_global_signals(self._page_features, self._page_labels, h1_result),
            agent_decision_log=list(self._decision_log),
            created_at=datetime.now(timezone.utc),
        )

    def _fallback_page_map(self, job_id: str, reason: str) -> PageMap:
        page_count = len(self._page_features)
        shards = [Shard(page_start=1, page_end=max(page_count, 1), page_offset=0)]
        return PageMap(
            job_id=job_id,
            file_path=self._pdf_path,
            page_count=page_count,
            h1_result=H1BoundaryResult(toc_pages=[], h1_matches=[], method="none"),
            page_features=self._page_features,
            shards=shards,
            needs_split=False,
            global_signals={},
            agent_decision_log=self._decision_log + [f"FALLBACK: {reason}"],
            created_at=datetime.now(timezone.utc),
        )


# ── Module helpers ─────────────────────────────────────────────────────────────


def _parse_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Normalise an LLM response into [{id, name, args}] dicts."""
    if isinstance(response, str):
        return []
    choices = getattr(response, "choices", None)
    if not choices:
        return []
    tc_list = getattr(choices[0].message, "tool_calls", None) or []
    result = []
    for tc in tc_list:
        fn = getattr(tc, "function", None)
        if not fn:
            continue
        try:
            args = json.loads(getattr(fn, "arguments", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        result.append({"id": getattr(tc, "id", ""), "name": getattr(fn, "name", ""), "args": args})
    return result


def _deterministic_cuts(
    page_count: int,
    page_labels: list[dict[str, Any]],
    h1_result: H1BoundaryResult | None,
    max_per_shard: int,
    min_per_shard: int,
) -> list[CutPoint]:
    """Produce cuts without LLM: h1 pages → blank/sparse pages → forced."""
    by_page: dict[int, str] = {
        int(p.get("page") or 0): str(p.get("special_kind") or "normal")
        for p in page_labels
        if p.get("page")
    }
    avoid = {"table_heavy", "landscape"}
    cuts: list[CutPoint] = []

    # Option A: h1 heading boundaries
    h1_pages = sorted({m.page for m in (h1_result.h1_matches if h1_result else [])})
    if h1_pages:
        prev = 0
        for h1_page in h1_pages:
            cut_page = h1_page - 1
            if cut_page <= 0 or cut_page <= prev:
                continue
            shard_len = cut_page - prev
            if shard_len < min_per_shard:
                continue
            if shard_len > max_per_shard:
                # Need an intermediate forced cut
                forced = prev + max_per_shard
                while forced < cut_page:
                    actual = forced
                    for offset in range(5):
                        cand = forced - offset
                        if cand > prev and by_page.get(cand, "normal") not in avoid:
                            actual = cand
                            break
                    cuts.append(CutPoint(cut_after_page=actual, anchor_type="forced",
                                         rationale="intermediate forced cut before h1", confidence=0.5))
                    prev = actual
                    forced = prev + max_per_shard
            cuts.append(CutPoint(cut_after_page=cut_page, anchor_type="h1_heading",
                                  rationale=f"h1 heading starts at page {h1_page}", confidence=0.9))
            prev = cut_page
        if cuts:
            return cuts

    # Option B: blank/sparse pages near shard boundaries
    prev = 0
    while prev + max_per_shard < page_count:
        target = prev + max_per_shard
        actual = target
        for offset in range(min(10, max_per_shard // 2)):
            for cand in [target - offset, target + offset]:
                if prev < cand <= page_count:
                    kind = by_page.get(cand, "normal")
                    if kind in {"blank", "sparse"} and cand - prev >= min_per_shard:
                        actual = cand
                        break
            else:
                continue
            break
        anchor = "blank" if by_page.get(actual, "normal") == "blank" else (
            "sparse" if by_page.get(actual, "normal") == "sparse" else "forced"
        )
        cuts.append(CutPoint(cut_after_page=actual, anchor_type=anchor,
                              rationale=f"deterministic cut near shard boundary", confidence=0.6))
        prev = actual

    return cuts


def _cuts_to_shards(cuts: list[CutPoint], page_count: int) -> list[Shard]:
    if page_count <= 0:
        return []
    if not cuts:
        return [Shard(page_start=1, page_end=page_count, page_offset=0)]
    shards: list[Shard] = []
    prev = 0
    for cut in sorted(cuts, key=lambda c: c.cut_after_page):
        cap = min(int(cut.cut_after_page), page_count)
        if cap <= prev:
            continue
        shards.append(Shard(page_start=prev + 1, page_end=cap, page_offset=prev))
        prev = cap
    if prev < page_count:
        shards.append(Shard(page_start=prev + 1, page_end=page_count, page_offset=prev))
    return shards


def _global_signals(
    features: list[PageFeature],
    labels: list[dict[str, Any]],
    h1_result: H1BoundaryResult,
) -> dict[str, Any]:
    total = len(features)
    if not total:
        return {}
    label_counts: dict[str, int] = {}
    for lbl in labels:
        k = str(lbl.get("special_kind") or "normal")
        label_counts[k] = label_counts.get(k, 0) + 1
    return {
        "total_pages": total,
        "h1_method": h1_result.method,
        "toc_pages": h1_result.toc_pages,
        "h1_match_count": len(h1_result.h1_matches),
        "h1_cut_candidates": h1_result.cut_candidate_pages(),
        "landscape_ratio": round(
            sum(1 for f in features if f.orientation == "landscape") / total, 3
        ),
        "blank_ratio": round(
            sum(1 for f in features if f.is_blank_like) / total, 3
        ),
        "page_type_counts": label_counts,
    }
