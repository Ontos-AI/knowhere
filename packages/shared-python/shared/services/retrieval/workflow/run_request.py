from __future__ import annotations

from dataclasses import dataclass

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

    def for_step(self, step: PlannedStep) -> WorkflowStepRequest:
        return WorkflowStepRequest(
            user_id=self.user_id,
            namespace=self.namespace,
            query=step.sub_query,
            top_k=step.top_k or self.top_k,
            exclude_document_ids=self.exclude_document_ids,
            exclude_sections=self.exclude_sections,
            data_type=step.data_type or self.data_type,
            signal_paths=self.signal_paths,
            filter_mode=self.filter_mode,
            channels=self.channels,
            channel_weights=self.channel_weights,
        )


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
