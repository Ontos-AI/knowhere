"""Lightweight trace recorder for agentic retrieval runs.

Records each run and its steps into retrieval_runs / retrieval_steps tables.
All writes are best-effort — failures are logged but never propagate to the
caller.  This ensures trace recording cannot break the retrieval pipeline.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.core.types import (
    AgentRunConfig,
    DecisionTraceStep,
    ToolResult,
)
from shared.services.retrieval.settings import DEFAULT_TOP_K


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode('utf-8')).hexdigest()[:16]


class TraceRecorder:
    """Records a single agentic retrieval run and its steps.

    Usage::

        trace = TraceRecorder(db, user_id=..., namespace=..., query=..., config=...)
        await trace.create_run()
        ...
        trace.record_step(action_type, tool_result)
        ...
        await trace.complete(ranked_rows, router_used)
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        config: AgentRunConfig,
        top_k: int = DEFAULT_TOP_K,
        data_type: int = 1,
        filters: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        workflow_step_id: str | None = None,
        workflow_plan: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self._run_id = f'aret_{uuid4().hex[:12]}'
        self._user_id = user_id
        self._namespace = namespace
        self._query = query
        self._config = config
        self._top_k = top_k
        self._data_type = data_type
        self._filters = filters or {}
        self._parent_run_id = parent_run_id
        self._workflow_step_id = workflow_step_id
        self._workflow_plan = workflow_plan
        self._steps: list[dict[str, Any]] = []
        self._start_time = time.monotonic()
        self._created = False

    @property
    def run_id(self) -> str:
        return self._run_id

    async def create_run(self) -> None:
        """Insert the retrieval_runs row.  Best-effort."""
        try:
            from shared.models.database.document import RetrievalRun

            run = RetrievalRun(
                run_id=self._run_id,
                user_id=self._user_id,
                namespace=self._namespace,
                query=self._query,
                query_hash=_query_hash(self._query),
                top_k=self._top_k,
                data_type=self._data_type,
                filters=self._filters,
                policy_name='llm_policy_v1',
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
            logger.debug(f'agentic trace: failed to create run {self._run_id}: {e}')
            try:
                await self._db.rollback()
            except Exception as rollback_error:
                logger.debug(
                    f'agentic trace: failed to roll back create failure '
                    f'{self._run_id}: {rollback_error}'
                )

    def record_step(
        self,
        action_type: str,
        result: ToolResult,
        *,
        decision_reason: str = '',
    ) -> None:
        """Buffer a step record.  Flushed on complete()."""
        self._steps.append({
            'step_index': len(self._steps),
            'action_type': action_type,
            'action_input': {'decision_reason': decision_reason} if decision_reason else {},
            'observation_status': result.status,
            'observation_payload_keys': list(result.payload.keys()) if result.payload else [],
            'latency_ms': result.latency_ms,
            'error': result.error,
            'tokens_used': result.tokens_used,
            'created_at': _now_utc(),
        })

    def record_decision_trace_step(self, step: DecisionTraceStep) -> None:
        """Buffer a DB trace row derived from the public decision trace step."""
        result_status = str(step.result.get("status") or "unknown")
        self._steps.append({
            "step_index": len(self._steps),
            "action_type": f"{step.phase}:{step.agent}:{step.decision.get('action', '')}",
            "action_input": {
                "public_step_index": step.step_index,
                "decision": step.decision,
                "scope": step.scope,
                "document_id": step.document_id,
                "parent_step_index": step.parent_step_index,
            },
            "observation_status": result_status,
            "observation_payload_keys": list(step.observation.keys()),
            "latency_ms": step.elapsed_ms or 0,
            "error": step.result.get("error"),
            "tokens_used": 0,
            "created_at": _now_utc(),
        })

    def record_budget_stop(self, reason: str) -> None:
        """Record that the agent loop stopped due to a budget guard."""
        self._steps.append({
            'step_index': len(self._steps),
            'action_type': f'budget_stop_{reason}',
            'action_input': {},
            'observation_status': 'budget_stop',
            'observation_payload_keys': [],
            'latency_ms': 0,
            'error': None,
            'tokens_used': 0,
            'created_at': _now_utc(),
        })

    async def complete(
        self,
        ranked_rows: list[dict[str, Any]],
        router_used: str,
        budget_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Flush all step records and update the run row.  Best-effort."""
        if not self._created:
            return

        total_latency = int((time.monotonic() - self._start_time) * 1000)

        try:
            from shared.models.database.document import RetrievalStep

            for step_data in self._steps:
                step = RetrievalStep(
                    step_id=f'arst_{uuid4().hex[:12]}',
                    run_id=self._run_id,
                    step_index=step_data['step_index'],
                    action_type=step_data['action_type'],
                    action_input=step_data.get('action_input'),
                    observation={
                        'status': step_data['observation_status'],
                        'payload_keys': step_data['observation_payload_keys'],
                        'tokens_used': step_data.get('tokens_used', 0),
                    },
                    latency_ms=step_data['latency_ms'],
                    token_count=step_data.get('tokens_used', 0),
                    error=step_data.get('error'),
                    created_at=step_data['created_at'],
                )
                self._db.add(step)

            # Update run row
            from sqlalchemy import update
            from shared.models.database.document import RetrievalRun

            # Build provenance: which docs contributed to final results
            doc_ids_in_result = list({r.get('document_id', '') for r in ranked_rows if r.get('document_id')})
            provenance = {
                'router': router_used,
                'step_count': len(self._steps),
                'final_doc_ids': doc_ids_in_result,
            }
            if budget_snapshot is not None:
                provenance['budget_snapshot'] = budget_snapshot
            if self._parent_run_id:
                provenance['parent_run_id'] = self._parent_run_id
            if self._workflow_step_id:
                provenance['workflow_step_id'] = self._workflow_step_id

            stmt = (
                update(RetrievalRun)
                .where(RetrievalRun.run_id == self._run_id)
                .values(
                    result_count=len(ranked_rows),
                    final_doc_ids=doc_ids_in_result,
                    result_provenance=provenance,
                    latency_ms=total_latency,
                    token_count=sum(step.get('tokens_used', 0) for step in self._steps),
                    completed_at=_now_utc(),
                )
            )
            await self._db.execute(stmt)
            await self._db.flush()

        except Exception as e:
            logger.debug(f'agentic trace: failed to complete run {self._run_id}: {e}')
            try:
                await self._db.rollback()
            except Exception as rollback_error:
                logger.debug(
                    f'agentic trace: failed to roll back completion failure '
                    f'{self._run_id}: {rollback_error}'
                )
