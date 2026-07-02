"""Residual page-location agent for hierarchy titles."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    collect_title_candidate_matches,
    iter_leaf_title_nodes,
    locate_title_strict_exact,
    max_title_depth,
    prune_title_nodes_for_emit_depth,
)
from app.services.document_agent.structure.page_locate_subagent import (
    PageLocateSubAgent,
    SubAgentConfig,
)
from shared.models.schemas.page_memory_config import PageMemoryConfig

VLM_CONFIRMED_DEFAULT_CONFIDENCE = 0.75
GREP_ONLY_CONFIDENCE_CAP = 0.62
RENDER_FAILED_GREP_CONFIDENCE_CAP = 0.58
BUDGET_EXHAUSTED_GREP_CONFIDENCE_CAP = 0.56
VLM_FAILED_GREP_CONFIDENCE_CAP = 0.54


@dataclass(frozen=True)
class PageLocateConfig:
    residual_agent_limit: int = 50
    max_emit_depth: int = 5
    min_emit_depth: int = 2
    vlm_candidate_page_cap: int = 4
    full_leaf_sections: bool = False

    @classmethod
    def from_page_memory_config(
        cls,
        page_memory_config: PageMemoryConfig,
    ) -> "PageLocateConfig":
        return cls(
            residual_agent_limit=page_memory_config.page_locate_residual_agent_limit,
            max_emit_depth=page_memory_config.page_locate_max_emit_depth,
            min_emit_depth=page_memory_config.page_locate_min_emit_depth,
            vlm_candidate_page_cap=(
                page_memory_config.page_locate_vlm_candidate_page_cap
            ),
            full_leaf_sections=page_memory_config.page_locate_full_leaf_sections,
        )


@dataclass(frozen=True)
class ResidualRequest:
    path_titles: tuple[str, ...]
    node: TitleNode


@dataclass(frozen=True)
class PageLocatePrepareResult:
    nodes: list[TitleNode]
    match_overrides: dict[tuple[str, ...], TitleMatch]
    summary: dict[str, Any] = field(default_factory=dict)


class PageLocateResidualAgent:
    """Batch residual resolver for C4 title-to-page anchoring."""

    def __init__(
        self,
        *,
        ctx: ToolContext | None,
        page_texts: dict[int, str],
        body_pages: list[int],
        page_count: int,
        page_offset_hint: int | None,
        config: PageLocateConfig | None = None,
    ) -> None:
        self.ctx = ctx
        self.page_texts = page_texts
        self.body_pages = sorted({page for page in body_pages if 1 <= page <= page_count})
        self.page_count = page_count
        self.page_offset_hint = page_offset_hint
        self.config = config or PageLocateConfig()

    def prepare(self, nodes: list[TitleNode]) -> PageLocatePrepareResult:
        if not nodes:
            return PageLocatePrepareResult(nodes=[], match_overrides={}, summary={})

        actual_max_depth = max_title_depth(nodes)
        emit_depth = min(actual_max_depth, self.config.max_emit_depth)
        emit_depth = max(emit_depth, self.config.min_emit_depth)
        direct_matches: dict[tuple[str, ...], TitleMatch] = {}
        residuals: list[ResidualRequest] = []

        while True:
            selected_nodes = prune_title_nodes_for_emit_depth(nodes, emit_depth=emit_depth)
            direct_matches, residuals = self._classify_residuals(selected_nodes)
            if (
                self.config.full_leaf_sections
                or len(residuals) <= self.config.residual_agent_limit
                or emit_depth <= self.config.min_emit_depth
            ):
                break
            emit_depth -= 1

        residual_matches = self._resolve_residuals(residuals)
        match_overrides = {**direct_matches, **residual_matches}
        summary = {
            "agent": "page_locate_residual",
            "emit_depth": emit_depth,
            "actual_max_depth": actual_max_depth,
            "direct_exact_count": len(direct_matches),
            "residual_count": len(residuals),
            "resolved_residual_count": len(residual_matches),
            "residual_limit": self.config.residual_agent_limit,
            "vlm_candidate_page_cap": self.config.vlm_candidate_page_cap,
            "full_leaf_sections": self.config.full_leaf_sections,
        }
        logger.info("[page_locate.agent] summary={}", summary)
        return PageLocatePrepareResult(
            nodes=selected_nodes,
            match_overrides=match_overrides,
            summary=summary,
        )

    def _classify_residuals(
        self,
        nodes: list[TitleNode],
    ) -> tuple[dict[tuple[str, ...], TitleMatch], list[ResidualRequest]]:
        direct: dict[tuple[str, ...], TitleMatch] = {}
        residuals: list[ResidualRequest] = []
        for path_titles, node in iter_leaf_title_nodes(nodes):
            match = locate_title_strict_exact(
                node.title,
                scope_pages=self.body_pages,
                page_texts=self.page_texts,
            )
            if match is not None:
                direct[path_titles] = TitleMatch(
                    page=match.page,
                    confidence=match.confidence,
                    source=match.source,
                    matched_line=match.matched_line,
                    score=match.score,
                    candidates=match.candidates,
                    evidence={
                        **match.evidence,
                        "page_locate_agent": {
                            "decision": "direct_strict_exact",
                            "path_titles": list(path_titles),
                        },
                    },
                )
            else:
                residuals.append(ResidualRequest(path_titles=path_titles, node=node))
        return direct, residuals

    def _resolve_residuals(
        self,
        residuals: list[ResidualRequest],
    ) -> dict[tuple[str, ...], TitleMatch]:
        matches: dict[tuple[str, ...], TitleMatch] = {}
        if not residuals or self.ctx is None:
            # No agent runtime (ctx/budget) → leave residuals for physical-hint /
            # neighbor-boundary fallback in the resolver. The residual sub-agent
            # only runs when a real ToolContext is available.
            return matches

        sub_config = SubAgentConfig(
            candidate_cap=max(self.config.vlm_candidate_page_cap * 2, 4),
            verify_page_cap=max(self.config.vlm_candidate_page_cap, 1),
        )
        for residual in residuals:
            agent = PageLocateSubAgent(
                ctx=self.ctx,
                scope_pages=self.body_pages,
                page_count=self.page_count,
                config=sub_config,
                page_offset_hint=self.page_offset_hint,
            )
            result = agent.locate(
                title=residual.node.title,
                printed_page=residual.node.printed_page,
            )
            if result.match is None:
                logger.warning(
                    "[page_locate.agent] unresolved title={!r} path_titles={} stop={}",
                    residual.node.title,
                    residual.path_titles,
                    result.stop_reason,
                )
                continue
            base = result.match
            agent_evidence = dict(base.evidence.get("page_locate_agent", {}))
            agent_evidence["path_titles"] = list(residual.path_titles)
            agent_evidence["stop_reason"] = result.stop_reason
            matches[residual.path_titles] = TitleMatch(
                page=base.page,
                confidence=base.confidence,
                source=base.source,
                matched_line=base.matched_line,
                score=base.score,
                candidates=base.candidates,
                evidence={**base.evidence, "page_locate_agent": agent_evidence},
            )
        return matches


def grep_title_page_candidates(
    *,
    title: str,
    scope_pages: list[int],
    page_texts: dict[int, str],
    printed_page: int | None = None,
    page_offset_hint: int | None = None,
    limit: int | None = None,
) -> list[TitleMatch]:
    return collect_title_candidate_matches(
        title,
        scope_pages=scope_pages,
        page_texts=page_texts,
        printed_page=printed_page,
        page_offset_hint=page_offset_hint,
        limit=limit,
    )


def verify_section_page_choice(
    *,
    ctx: ToolContext | None,
    title: str,
    candidate_matches: list[TitleMatch],
    candidate_page_cap: int,
) -> dict[str, Any]:
    candidates = candidate_matches[: max(candidate_page_cap, 1)]
    if not candidates:
        return {
            "selected_page": None,
            "confidence": 0.0,
            "source": "agent_heuristic",
            "reason": "no grep candidates",
        }

    model = None
    if ctx is not None:
        model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    pages = [match.page for match in candidates]
    if ctx is None or not model or ctx.budget is None:
        best = candidates[0]
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, GREP_ONLY_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": "VLM unavailable; selected top grep candidate",
        }

    from app.services.document_agent.visual import render_pages

    rendered = render_pages(
        ctx,
        pages,
        folder_name="page_locate_pages",
        prefix="locate",
        timeout=120,
    )
    if not rendered:
        best = candidates[0]
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, RENDER_FAILED_GREP_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": "render failed; selected top grep candidate",
        }

    prompt = _build_verify_prompt(title=title, candidates=candidates)
    est = 800 * len(rendered) + 800
    stage = "page_locate"
    if not ctx.budget.try_reserve("visual", est, stage=stage):
        best = candidates[0]
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, BUDGET_EXHAUSTED_GREP_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": "page_locate visual budget exhausted; selected top grep candidate",
        }

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in rendered:
        with open(str(item["png_path"]), "rb") as image_file:
            img_b64 = base64.b64encode(image_file.read()).decode()
        content_parts.append({"type": "text", "text": f"\n--- Page {item['page']} ---"})
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )

    start = time.monotonic()
    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=model,
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
            usage_task="page_memory.page_locate",
        )
        ctx.budget.commit(
            "visual",
            actual=usage.get("total_tokens", est),
            est=est,
            stage=stage,
        )
        payload = json.loads(raw)
        selected_page = payload.get("selected_page")
        if selected_page is not None:
            selected_page = int(selected_page)
        if selected_page not in pages:
            selected_page = None
        return {
            "selected_page": selected_page,
            "candidate_pages": pages,
            "confidence": float(payload.get("confidence") or VLM_CONFIRMED_DEFAULT_CONFIDENCE),
            "source": "agent_vlm",
            "reason": str(payload.get("reason") or ""),
            "latency_ms": int((time.monotonic() - start) * 1000),
            "tokens_used": usage.get("total_tokens", 0),
        }
    except Exception as exc:
        ctx.budget.refund("visual", est=est, stage=stage)
        best = candidates[0]
        logger.warning("[page_locate.agent] VLM failed for title={!r}: {}", title, exc)
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, VLM_FAILED_GREP_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": f"VLM failed ({type(exc).__name__}); selected top grep candidate",
        }


def _build_verify_prompt(*, title: str, candidates: list[TitleMatch]) -> str:
    candidate_lines = "\n".join(
        f"- page {match.page}: source={match.source}, line={match.matched_line!r}"
        for match in candidates
    )
    return (
        "You are a page-location sub-agent for a PDF hierarchy parser.\n"
        "Choose which candidate page is the true START page of the section title, "
        "not a table-of-contents entry, page header, footer, or body-text mention.\n"
        f"Section title: {title!r}\n"
        f"Candidates:\n{candidate_lines}\n"
        "Return strict JSON: {\"selected_page\": number|null, "
        "\"confidence\": number, \"reason\": \"brief explanation\"}."
    )
