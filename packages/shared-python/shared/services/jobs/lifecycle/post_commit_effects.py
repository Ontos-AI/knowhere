from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.services.jobs.lifecycle.publication import SyncJobPublicationFinalizer
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox


@dataclass(frozen=True)
class PostCommitEffectPlan:
    retrieval_cache_invalidations: tuple[dict[str, Any], ...] = field(
        default_factory=tuple
    )
    webhook_event_ids: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def none(cls) -> PostCommitEffectPlan:
        return cls()

    @classmethod
    def from_success(
        cls,
        *,
        cache_invalidation: dict[str, Any] | None,
        webhook_event_id: str | None,
    ) -> PostCommitEffectPlan:
        return cls(
            retrieval_cache_invalidations=(
                (cache_invalidation,) if cache_invalidation else ()
            ),
            webhook_event_ids=((webhook_event_id,) if webhook_event_id else ()),
        )

    @classmethod
    def from_failure(
        cls,
        *,
        webhook_event_id: str | None,
    ) -> PostCommitEffectPlan:
        return cls(webhook_event_ids=((webhook_event_id,) if webhook_event_id else ()))


class SyncJobPostCommitEffectRunner:
    def __init__(
        self,
        *,
        publication_finalizer: SyncJobPublicationFinalizer | None = None,
        webhook_outbox: SyncJobWebhookOutbox | None = None,
    ) -> None:
        self._publication_finalizer = (
            publication_finalizer or SyncJobPublicationFinalizer()
        )
        self._webhook_outbox = webhook_outbox or SyncJobWebhookOutbox()

    def run(self, plan: PostCommitEffectPlan) -> None:
        for cache_invalidation in plan.retrieval_cache_invalidations:
            self._publication_finalizer.invalidate_cache_after_commit(
                cache_invalidation
            )
        for webhook_event_id in plan.webhook_event_ids:
            self._webhook_outbox.enqueue_event_id_after_commit(webhook_event_id)
