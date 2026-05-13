"""Types for query-decomposition retrieval workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


StepKind = Literal["retrieve", "synthesize"]
OutputRole = Literal["final_part", "intermediate", "consumed_by_synthesis"]
FinalStrategy = Literal["concat_final_parts", "last_synthesize", "template"]
StepStatus = Literal["done", "skipped", "error", "budget_stop", "not_found"]


@dataclass
class PlannedStep:
    """A single node in the query workflow DAG."""

    id: str
    sub_query: str
    step_kind: StepKind = "retrieve"
    depends_on: list[str] = field(default_factory=list)
    output_role: OutputRole = "final_part"
    top_k: int | None = None
    data_type: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "sub_query": self.sub_query,
            "step_kind": self.step_kind,
            "depends_on": list(self.depends_on),
            "output_role": self.output_role,
        }
        if self.top_k is not None:
            data["top_k"] = self.top_k
        if self.data_type is not None:
            data["data_type"] = self.data_type
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass
class QueryPlan:
    """Planner output consumed by ``WorkflowOrchestrator``."""

    original_query: str
    steps: list[PlannedStep]
    final_strategy: FinalStrategy = "concat_final_parts"
    reasoning_summary: str = ""
    final_template: str | None = None
    planner_status: str = "planned"
    planner_error: str | None = None

    @staticmethod
    def single_step(query: str, *, reason: str = "single_step") -> "QueryPlan":
        return QueryPlan(
            original_query=query,
            steps=[
                PlannedStep(
                    id="s1",
                    sub_query=query,
                    step_kind="retrieve",
                    depends_on=[],
                    output_role="final_part",
                )
            ],
            final_strategy="concat_final_parts",
            reasoning_summary=reason,
        )

    @staticmethod
    def from_dict(data: dict[str, Any], *, original_query: str | None = None) -> "QueryPlan":
        steps = [
            PlannedStep(
                id=str(item.get("id") or f"s{idx}"),
                sub_query=str(item.get("sub_query") or original_query or ""),
                step_kind=item.get("step_kind", "retrieve"),
                depends_on=[str(dep) for dep in item.get("depends_on") or []],
                output_role=item.get("output_role", "final_part"),
                top_k=int(item["top_k"]) if item.get("top_k") is not None else None,
                data_type=int(item["data_type"]) if item.get("data_type") is not None else None,
                metadata=dict(item.get("metadata") or {}),
            )
            for idx, item in enumerate(data.get("steps") or [], start=1)
            if isinstance(item, dict)
        ]
        plan = QueryPlan(
            original_query=str(data.get("original_query") or original_query or ""),
            steps=steps,
            final_strategy=data.get("final_strategy", "concat_final_parts"),
            reasoning_summary=str(data.get("reasoning_summary") or ""),
            final_template=data.get("final_template"),
            planner_status=str(data.get("planner_status") or "cached"),
            planner_error=data.get("planner_error"),
        )
        plan.validate()
        return plan

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "original_query": self.original_query,
            "reasoning_summary": self.reasoning_summary,
            "steps": [step.to_dict() for step in self.steps],
            "final_strategy": self.final_strategy,
            "planner_status": self.planner_status,
        }
        if self.final_template:
            data["final_template"] = self.final_template
        if self.planner_error:
            data["planner_error"] = self.planner_error
        return data

    def step_by_id(self) -> dict[str, PlannedStep]:
        return {step.id: step for step in self.steps}

    def validate(self) -> None:
        if not self.steps:
            raise ValueError("query plan must contain at least one step")
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("query plan step ids must be unique")
        id_set = set(ids)
        for step in self.steps:
            if not step.id.strip():
                raise ValueError("query plan step id cannot be empty")
            if not step.sub_query.strip():
                raise ValueError(f"query plan step {step.id} sub_query cannot be empty")
            if step.step_kind not in ("retrieve", "synthesize"):
                raise ValueError(f"unsupported step_kind: {step.step_kind}")
            if step.output_role not in ("final_part", "intermediate", "consumed_by_synthesis"):
                raise ValueError(f"unsupported output_role: {step.output_role}")
            missing = [dep for dep in step.depends_on if dep not in id_set]
            if missing:
                raise ValueError(f"step {step.id} depends on unknown steps: {missing}")
            if step.step_kind == "synthesize" and not step.depends_on:
                raise ValueError(f"synthesize step {step.id} must depend on prior steps")
        self.topological_batches()

    def topological_batches(self) -> list[list[PlannedStep]]:
        """Return executable batches; steps in a batch have no mutual dependency."""
        remaining = {step.id: step for step in self.steps}
        completed: set[str] = set()
        batches: list[list[PlannedStep]] = []
        while remaining:
            ready = [
                step
                for step in self.steps
                if step.id in remaining and set(step.depends_on).issubset(completed)
            ]
            if not ready:
                raise ValueError("query plan contains a dependency cycle")
            batches.append(ready)
            for step in ready:
                remaining.pop(step.id, None)
                completed.add(step.id)
        return batches


@dataclass
class StepResult:
    """Execution result for one planned step."""

    step_id: str
    sub_query: str
    step_kind: StepKind
    depends_on: list[str]
    output_role: OutputRole
    status: StepStatus = "done"
    answer_text: str = ""
    evidence_text: str | None = None
    referenced_chunks: list[dict[str, Any]] = field(default_factory=list)
    budget_snapshot: dict[str, Any] | None = None
    child_run_id: str | None = None
    router_used: str = ""
    stop_reason: str = ""
    failure_reason: str = ""
    error: str | None = None

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "sub_query": self.sub_query,
            "step_kind": self.step_kind,
            "depends_on": list(self.depends_on),
            "output_role": self.output_role,
            "status": self.status,
            "answer_text": self.answer_text,
            "evidence_text": self.evidence_text,
            "referenced_chunks": list(self.referenced_chunks),
            "budget_snapshot": self.budget_snapshot,
            "child_run_id": self.child_run_id,
            "router_used": self.router_used,
            "stop_reason": self.stop_reason,
            "failure_reason": self.failure_reason,
            "error": self.error,
        }


@dataclass
class WorkflowResult:
    """Top-level response from query workflow execution."""

    namespace: str
    query: str
    router_used: str
    answer_text: str
    plan: QueryPlan | None = None
    steps: list[StepResult] = field(default_factory=list)
    referenced_chunks: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    final_strategy_used: FinalStrategy | None = None
    wallet_snapshot: dict[str, Any] | None = None
    planner_snapshot: dict[str, Any] | None = None
    parent_run_id: str | None = None

    def to_api_response(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "query": self.query,
            "router_used": self.router_used,
            "answer_text": self.answer_text,
            "referenced_chunks": self.referenced_chunks,
            "results": self.results,
            "plan": self.plan.to_dict() if self.plan else None,
            "steps": [step.to_api_dict() for step in self.steps] if self.steps else None,
            "final_strategy_used": self.final_strategy_used,
            "wallet_snapshot": self.wallet_snapshot,
            "planner_snapshot": self.planner_snapshot,
            "parent_run_id": self.parent_run_id,
        }
