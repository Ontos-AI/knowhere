from __future__ import annotations

from dataclasses import dataclass

from shared.services.retrieval.route_types import RetrievalRouteContext
from shared.services.retrieval.settings import INTERNAL_RECALL_K_MULTIPLIER
from shared.services.retrieval.workflow.types import PlannedStep


@dataclass(frozen=True)
class WorkflowRunRequest:
    user_id: str
    namespace: str
    query: str
    top_k: int
    exclude_document_ids: list[str]
    exclude_sections: list[dict[str, str]]
    data_type: int = 1
    signal_paths: list[str] | None = None
    filter_mode: str = "delete"
    channels: list[str] | None = None
    channel_weights: dict[str, float] | None = None
    internal_recall_k: int | None = None
    rerank: bool = False
    threshold: float = 0.0

    @classmethod
    def from_route_context(
        cls,
        context: RetrievalRouteContext,
    ) -> WorkflowRunRequest:
        return cls(
            user_id=context.user_id,
            namespace=context.namespace,
            query=context.query,
            top_k=context.top_k,
            exclude_document_ids=context.exclude_document_ids,
            exclude_sections=context.exclude_sections,
            data_type=context.data_type,
            signal_paths=context.signal_paths,
            filter_mode=context.filter_mode,
            channels=context.channels,
            channel_weights=context.channel_weights,
            internal_recall_k=context.internal_recall_k,
            rerank=context.rerank,
            threshold=context.threshold,
        )

    def for_step(self, step: PlannedStep) -> WorkflowStepRequest:
        step_top_k = step.top_k or self.top_k
        return WorkflowStepRequest(
            user_id=self.user_id,
            namespace=self.namespace,
            query=step.sub_query,
            top_k=step_top_k,
            exclude_document_ids=self.exclude_document_ids,
            exclude_sections=self.exclude_sections,
            data_type=step.data_type or self.data_type,
            signal_paths=self.signal_paths,
            filter_mode=self.filter_mode,
            channels=self.channels,
            channel_weights=self.channel_weights,
            internal_recall_k=self.resolve_effective_recall_k(step_top_k),
            rerank=self.rerank,
            threshold=self.threshold,
        )

    def resolve_effective_recall_k(self, top_k: int) -> int:
        if self.internal_recall_k is not None:
            return self.internal_recall_k
        return top_k * INTERNAL_RECALL_K_MULTIPLIER


@dataclass(frozen=True)
class WorkflowStepRequest:
    user_id: str
    namespace: str
    query: str
    top_k: int
    exclude_document_ids: list[str]
    exclude_sections: list[dict[str, str]]
    data_type: int
    signal_paths: list[str] | None
    filter_mode: str
    channels: list[str] | None
    channel_weights: dict[str, float] | None
    internal_recall_k: int | None
    rerank: bool
    threshold: float
