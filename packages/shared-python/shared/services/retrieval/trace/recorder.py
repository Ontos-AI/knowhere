"""Retrieval run / step recorder (moved from agentic/core/trace).

Writes retrieval_runs / retrieval_steps. Best-effort — failures never
propagate to the retrieval caller.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.nav_config import MAPNAV_MODEL
from shared.services.retrieval.settings import DEFAULT_TOP_K
from shared.services.retrieval.trace.types import DecisionTraceStep


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


class TraceRecorder:
    """Records a single retrieval run and its steps."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        chunk_types: set[str] | None = None,
        filters: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        workflow_step_id: str | None = None,
        workflow_plan: dict[str, Any] | None = None,
        policy_name: str = "llm_policy_v1",
        config: Any = None,
    ) -> None:
        del config  # legacy AgentRunConfig; ignored on map-nav path
        self._db = db
        self._run_id = f"aret_{uuid4().hex[:12]}"
        self._user_id = user_id
        self._namespace = namespace
        self._query = query
        self._top_k = top_k
        self._chunk_types = chunk_types
        self._filters = filters or {}
        self._parent_run_id = parent_run_id
        self._workflow_step_id = workflow_step_id
        self._workflow_plan = workflow_plan
        self._policy_name = policy_name
        self._steps: list[dict[str, Any]] = []
        self._start_time = time.monotonic()
        self._created = False

    @property
    def run_id(self) -> str:
        return self._run_id

    async def create_run(self) -> None:
        """Insert the retrieval_runs row. Best-effort."""
        try:
            from shared.models.database.document import RetrievalRun

            run = RetrievalRun(
                run_id=self._run_id,
                user_id=self._user_id,
                namespace=self._namespace,
                query=self._query,
                query_hash=_query_hash(self._query),
                top_k=self._top_k,
                chunk_types=sorted(self._chunk_types) if self._chunk_types else None,
                filters=self._filters,
                policy_name=self._policy_name,
                agentic_enabled=True,
                cache_hit=False,
                result_count=0,
                parent_run_id=self._parent_run_id,
                workflow_step_id=self._workflow_step_id,
                workflow_plan=self._workflow_plan,
                latency_ms=0,
                created_at=_now_utc(),
            )
            self._db.add(run)
            await self._db.flush()
            self._created = True
        except Exception as e:
            logger.debug(f"retrieval trace: failed to create run {self._run_id}: {e}")
            try:
                await self._db.rollback()
            except Exception as rollback_error:
                logger.debug(
                    f"retrieval trace: failed to roll back create failure "
                    f"{self._run_id}: {rollback_error}"
                )

    def record_decision_trace_step(self, step: DecisionTraceStep) -> None:
        """Buffer a DB trace row derived from the public decision trace step."""
        result_status = str(step.result.get("status") or "unknown")
        budget = step.budget or {}
        tokens = int(budget.get("tokens_used_delta") or 0)
        model = (
            step.result.get("model")
            or (step.decision or {}).get("model")
            or None
        )
        selected_paths = (
            step.result.get("selected_paths")
            or step.result.get("collect_section_ids")
            or None
        )
        if isinstance(selected_paths, list):
            selected_paths = [str(x) for x in selected_paths if str(x).strip()]
        else:
            selected_paths = None
        selected_docs = step.result.get("selected_doc_ids")
        if isinstance(selected_docs, list):
            selected_docs = [str(x) for x in selected_docs if str(x).strip()]
        else:
            selected_docs = None
        self._steps.append(
            {
                "step_index": len(self._steps),
                "action_type": (
                    f"{step.phase}:{step.agent}:{step.decision.get('action', '')}"
                ),
                "action_input": {
                    "public_step_index": step.step_index,
                    "decision": step.decision,
                    "scope": step.scope,
                    "document_id": step.document_id,
                    "parent_step_index": step.parent_step_index,
                },
                "observation_status": result_status,
                "observation_payload_keys": list(step.observation.keys()),
                "latency_ms": int(step.elapsed_ms or 0),
                "error": step.result.get("error"),
                "tokens_used": tokens,
                "selected_paths": selected_paths,
                "selected_doc_ids": selected_docs,
                "model_name": str(model).strip() if model else None,
                "created_at": _now_utc(),
            }
        )

    def record_budget_stop(self, reason: str) -> None:
        """Record that the agent loop stopped due to a budget guard."""
        self._steps.append(
            {
                "step_index": len(self._steps),
                "action_type": f"budget_stop_{reason}",
                "action_input": {},
                "observation_status": "budget_stop",
                "observation_payload_keys": [],
                "latency_ms": 0,
                "error": None,
                "tokens_used": 0,
                "selected_paths": None,
                "selected_doc_ids": None,
                "model_name": None,
                "created_at": _now_utc(),
            }
        )

    async def complete(
        self,
        ranked_rows: list[dict[str, Any]],
        router_used: str,
        budget_snapshot: dict[str, Any] | None = None,
        *,
        token_count: int | None = None,
        model_name: str | None = None,
        selected_paths: list[str] | None = None,
        selected_doc_ids: list[str] | None = None,
    ) -> None:
        """Flush step records and update the run row. Best-effort."""
        if not self._created:
            return

        total_latency = int((time.monotonic() - self._start_time) * 1000)
        summed_tokens = sum(int(step.get("tokens_used", 0) or 0) for step in self._steps)
        run_token_count = (
            int(token_count) if token_count is not None else summed_tokens
        )

        try:
            from shared.models.database.document import RetrievalStep

            for step_data in self._steps:
                step = RetrievalStep(
                    step_id=f"arst_{uuid4().hex[:12]}",
                    run_id=self._run_id,
                    step_index=step_data["step_index"],
                    action_type=step_data["action_type"],
                    action_input=step_data.get("action_input"),
                    observation={
                        "status": step_data["observation_status"],
                        "payload_keys": step_data["observation_payload_keys"],
                        "tokens_used": step_data.get("tokens_used", 0),
                    },
                    selected_paths=step_data.get("selected_paths"),
                    selected_doc_ids=step_data.get("selected_doc_ids"),
                    latency_ms=step_data["latency_ms"],
                    token_count=step_data.get("tokens_used", 0),
                    model_name=step_data.get("model_name") or model_name,
                    error=step_data.get("error"),
                    created_at=step_data["created_at"],
                )
                self._db.add(step)

            from sqlalchemy import update

            from shared.models.database.document import RetrievalRun

            doc_ids_in_result = list(
                {
                    r.get("document_id", "")
                    for r in ranked_rows
                    if r.get("document_id")
                }
            )
            if selected_doc_ids:
                doc_ids_in_result = list(selected_doc_ids)
            provenance = {
                "router": router_used,
                "step_count": len(self._steps),
                "final_doc_ids": doc_ids_in_result,
                "model_name": model_name or MAPNAV_MODEL,
            }
            if selected_paths is not None:
                provenance["selected_paths"] = selected_paths
            if budget_snapshot is not None:
                provenance["budget_snapshot"] = budget_snapshot
            if self._parent_run_id:
                provenance["parent_run_id"] = self._parent_run_id
            if self._workflow_step_id:
                provenance["workflow_step_id"] = self._workflow_step_id

            stmt = (
                update(RetrievalRun)
                .where(RetrievalRun.run_id == self._run_id)
                .values(
                    result_count=len(ranked_rows),
                    final_doc_ids=doc_ids_in_result,
                    result_provenance=provenance,
                    latency_ms=total_latency,
                    token_count=run_token_count,
                    completed_at=_now_utc(),
                )
            )
            await self._db.execute(stmt)
            await self._db.flush()

        except Exception as e:
            logger.debug(f"retrieval trace: failed to complete run {self._run_id}: {e}")
            try:
                await self._db.rollback()
            except Exception as rollback_error:
                logger.debug(
                    f"retrieval trace: failed to roll back completion failure "
                    f"{self._run_id}: {rollback_error}"
                )
