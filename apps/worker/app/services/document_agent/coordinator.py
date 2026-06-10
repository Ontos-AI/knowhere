"""ReAct-style coordinator for the document profile agent."""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from app.services.document_agent.bootstrap import (
    aggregate_doc_stats,
    classify_page_kinds,
    probe_page_features,
)
from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.executor import ReActExecutor
from app.services.document_agent.manifest import (
    DocumentProfile,
    PageAnatomyMap,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.persist import build_anatomy_map, persist_anatomy_map
from app.services.document_agent.planner import ProfilePlanner
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.state import AgentBlackboard, DocumentAgentState
from app.services.document_agent import tools as _registered_tools  # noqa: F401
from app.services.document_agent.trace import ParseRunRecorder


class ProfileCoordinator:
    def __init__(
        self,
        *,
        pdf_path: str,
        job_id: str,
        output_dir: str | None = None,
        db: Any | None = None,
        model: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.state = DocumentAgentState.INIT
        self.blackboard = AgentBlackboard()
        self.budget = BudgetTracker(
            plan_budget=int(os.environ.get("PARSE_AGENT_PLAN_BUDGET", "50000")),
            visual_budget=int(os.environ.get("PARSE_AGENT_VISUAL_BUDGET", "80000")),
        )
        effective_settings = settings or {}
        if model:
            effective_settings["model"] = model
        self.ctx = ToolContext(
            pdf_path=pdf_path,
            job_id=job_id,
            blackboard=self.blackboard,
            budget=self.budget,
            trace=None,
            output_dir=output_dir,
            settings=effective_settings,
        )
        self.trace = ParseRunRecorder(job_id=job_id, db=db)
        self.ctx.trace = self.trace
        self.round_index = 0
        self._planner_cache: tuple[DocumentProfile, Any, ToolResult] | None = None

    def run(self) -> PageAnatomyMap:
        try:
            return self._run_structural()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def run_coarse(self) -> DocumentProfile:
        try:
            return self._run_coarse()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def run_structural(self) -> PageAnatomyMap:
        try:
            return self._run_structural()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def run_toc(self) -> TocResult:
        try:
            return self._run_toc()
        except Exception as exc:
            logger.warning(
                "[document_agent] TOC profiling failed, degrading to empty TOC: {}",
                exc,
            )
            self.blackboard.toc_result = TocResult(
                method="none",
                notes=f"degraded: {type(exc).__name__}: {exc}",
                failure_kind="degraded",
            )
            self.blackboard.toc_hierarchies = None
            return self.blackboard.toc_result

    def run_lightweight_anatomy(self) -> PageAnatomyMap:
        try:
            return self._run_lightweight_anatomy()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _run_coarse(self) -> DocumentProfile:
        self.state = DocumentAgentState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        if self._should_run_toc_before_coarse():
            self._ensure_toc_profile(strict=False)
        profile, _initial_decision, _planner_result = self._propose_profile(
            actor="planner:coarse"
        )
        return profile

    def _run_structural(self) -> PageAnatomyMap:
        self.state = DocumentAgentState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        self._ensure_toc_profile(strict=True)
        profile, initial_decision, _planner_result = self._propose_profile(
            actor="planner"
        )
        self._run_h1_boundary_pipeline()
        executor_result = ReActExecutor(
            self.ctx,
            registry=REGISTRY,
            max_rounds=int(self.ctx.settings.get("max_rounds", 30)),
            initial_decision=initial_decision,
        ).run()
        if executor_result.verdict.status != "success":
            raise RuntimeError(f"profile aborted: {executor_result.verdict.rationale}")
        anatomy = build_anatomy_map(self.ctx)
        self._persist_ready_anatomy(anatomy)
        return anatomy

    def _run_toc(self) -> TocResult:
        self.state = DocumentAgentState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        self._ensure_toc_profile(strict=False)
        if self.blackboard.toc_result is None:
            self.blackboard.toc_result = TocResult(
                method="none",
                notes="TOC extraction completed without a result",
            )
        return self.blackboard.toc_result

    def _run_lightweight_anatomy(self) -> PageAnatomyMap:
        self.state = DocumentAgentState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        if self.blackboard.toc_result is None:
            self.blackboard.toc_result = TocResult(
                method="none",
                notes="TOC profiling disabled or not attempted",
            )
        self._run_h1_boundary_pipeline()
        result = REGISTRY.dispatch("propose.shard_plan", self.ctx, {})
        self.trace.record_step(
            round_index=self.round_index,
            actor="anatomy:propose.shard_plan",
            action_type="anatomy",
            result=result,
            tool_name="propose.shard_plan",
            tool_args={},
        )
        if result.status not in {"ok", "invalid"}:
            raise RuntimeError(result.error or "propose.shard_plan failed")
        self.round_index += 1
        anatomy = build_anatomy_map(self.ctx)
        self._persist_ready_anatomy(anatomy)
        return anatomy

    def _persist_ready_anatomy(self, anatomy: PageAnatomyMap) -> None:
        persist_result = persist_anatomy_map(self.ctx, {})
        self.trace.record_step(
            round_index=self.round_index,
            actor="persist",
            action_type="persist",
            result=persist_result,
            tool_name="persist.anatomy_map",
            tool_args={},
        )
        self.state = DocumentAgentState.READY
        self.trace.write_trace_artifact(
            self.ctx.output_dir,
            final_status="ready",
            summary=anatomy.trace_summary | self.trace.summary(),
        )
        self.trace.flush(
            final_status="ready",
            summary=anatomy.trace_summary | self.trace.summary(),
        )

    def _record_failure(self, exc: Exception) -> None:
        logger.error(f"[document_agent] profile failed: {exc}")
        self.state = DocumentAgentState.FAILED
        self.trace.write_trace_artifact(
            self.ctx.output_dir,
            final_status="failed",
            summary={"error": str(exc), "budget": self.ctx.budget.snapshot()},
        )
        self.trace.flush(final_status="failed", summary={"error": str(exc)})

    def _run_bootstrap(self) -> None:
        for tool_name, handler in (
            ("probe.page_features", probe_page_features),
            ("classify.page_kinds", classify_page_kinds),
            ("aggregate.doc_stats", aggregate_doc_stats),
        ):
            result = handler(self.ctx, {})
            self.trace.record_step(
                round_index=self.round_index,
                actor=f"bootstrap:{tool_name}",
                action_type="bootstrap",
                result=result,
                tool_name=tool_name,
                tool_args={},
            )
            if result.status != "ok":
                raise RuntimeError(result.error or f"{tool_name} failed")
            self.round_index += 1

    def _toc_result_requires_strict_retry(self) -> bool:
        toc_result = self.blackboard.toc_result
        return bool(
            toc_result
            and toc_result.method == "none"
            and toc_result.failure_kind in {"confirm_failed", "degraded"}
        )

    def _should_run_toc_before_coarse(self) -> bool:
        if self.ctx.settings.get("toc_before_coarse"):
            return True
        try:
            page_limit = int(self.ctx.settings.get("toc_before_coarse_page_limit", 0))
        except (TypeError, ValueError):
            page_limit = 0
        return page_limit > 0 and self.blackboard.page_count > page_limit

    def _ensure_toc_profile(self, *, strict: bool) -> None:
        should_run = self.blackboard.toc_result is None
        if strict and self._toc_result_requires_strict_retry():
            self.blackboard.toc_result = None
            self.blackboard.toc_hierarchies = None
            should_run = True

        if not should_run:
            return

        self._planner_cache = None
        try:
            self._run_toc_extraction_pipeline()
        except Exception as exc:
            logger.warning(
                "[document_agent] TOC profiling failed, "
                "degrading to empty TOC: {}",
                exc,
            )
            self.blackboard.toc_result = TocResult(
                method="none",
                notes=f"degraded: {type(exc).__name__}: {exc}",
                failure_kind="degraded",
            )
            self.blackboard.toc_hierarchies = None
            return

        if self.blackboard.toc_result is None:
            self.blackboard.toc_result = TocResult(
                method="none",
                notes="TOC extraction completed without a result",
            )

    def _propose_profile(self, *, actor: str) -> tuple[DocumentProfile, Any, ToolResult]:
        if self._planner_cache is not None:
            return self._planner_cache

        profile, initial_decision, planner_result = ProfilePlanner(self.ctx).propose()
        self.blackboard.document_profile = profile
        self.blackboard.global_signals["document_profile"] = profile.to_dict()
        self.trace.record_step(
            round_index=self.round_index,
            actor=actor,
            action_type="plan",
            result=planner_result,
            tool_name=None,
            tool_args={},
        )
        self.round_index += 1
        self._planner_cache = (profile, initial_decision, planner_result)
        return self._planner_cache

    def _dispatch_profile_tool(self, *, tool_name: str, actor: str) -> ToolResult:
        result = REGISTRY.dispatch(tool_name, self.ctx, {})
        self.trace.record_step(
            round_index=self.round_index,
            actor=actor,
            action_type="toc",
            result=result,
            tool_name=tool_name,
            tool_args={},
        )
        if result.status not in {"ok", "invalid"}:
            raise RuntimeError(result.error or f"{tool_name} failed")
        self.round_index += 1
        return result

    def _run_toc_extraction_pipeline(self) -> None:
        for tool_name in ("find.toc_anchor_pages", "extract.toc_with_boundaries"):
            self._dispatch_profile_tool(
                tool_name=tool_name,
                actor=f"toc:{tool_name}",
            )

    def _run_h1_boundary_pipeline(self) -> None:
        self._dispatch_profile_tool(
            tool_name="match.h1_pages",
            actor="toc:match.h1_pages",
        )
