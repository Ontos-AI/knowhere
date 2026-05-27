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
from app.services.document_agent.manifest import PageAnatomyMap, ToolContext
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

    def run(self) -> PageAnatomyMap:
        try:
            self.state = DocumentAgentState.RUNNING
            self._run_bootstrap()
            self._run_toc_pipeline()
            profile, initial_decision, planner_result = ProfilePlanner(self.ctx).propose()
            self.blackboard.document_profile = profile
            self.blackboard.global_signals["document_profile"] = profile.to_dict()
            self.trace.record_step(
                round_index=self.round_index,
                actor="planner",
                action_type="plan",
                result=planner_result,
                tool_name=None,
                tool_args={},
            )
            self.round_index += 1

            executor_result = ReActExecutor(
                self.ctx,
                registry=REGISTRY,
                max_rounds=int(self.ctx.settings.get("max_rounds", 30)),
                initial_decision=initial_decision,
            ).run()
            if executor_result.verdict.status != "success":
                raise RuntimeError(
                    f"profile aborted: {executor_result.verdict.rationale}"
                )
            anatomy = build_anatomy_map(self.ctx)
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
            return anatomy
        except Exception as exc:
            logger.error(f"[document_agent] profile failed: {exc}")
            self.state = DocumentAgentState.FAILED
            self.trace.write_trace_artifact(
                self.ctx.output_dir,
                final_status="failed",
                summary={"error": str(exc), "budget": self.ctx.budget.snapshot()},
            )
            self.trace.flush(final_status="failed", summary={"error": str(exc)})
            raise

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

    def _run_toc_pipeline(self) -> None:
        for tool_name in (
            "find.toc_anchor_pages",
            "extract.toc_with_boundaries",
            "match.h1_pages",
        ):
            result = REGISTRY.dispatch(tool_name, self.ctx, {})
            self.trace.record_step(
                round_index=self.round_index,
                actor=f"toc:{tool_name}",
                action_type="toc",
                result=result,
                tool_name=tool_name,
                tool_args={},
            )
            if result.status not in {"ok", "invalid"}:
                raise RuntimeError(result.error or f"{tool_name} failed")
            self.round_index += 1
