from __future__ import annotations

from typing import Any, cast


def test_post_commit_effect_runner_uses_primitive_effect_plan(
    worker_contract_environment: None,
) -> None:
    from shared.services.job_post_commit_effects_sync import (
        PostCommitEffectPlan,
        SyncJobPostCommitEffectRunner,
    )

    cache_invalidations: list[dict[str, Any]] = []
    webhook_event_ids: list[str] = []

    class FakePublicationFinalizer:
        def invalidate_cache_after_commit(
            self,
            cache_invalidation: dict[str, Any] | None,
        ) -> None:
            assert cache_invalidation is not None
            cache_invalidations.append(cache_invalidation)

    class FakeWebhookOutbox:
        def enqueue_event_id_after_commit(
            self,
            webhook_event_id: str | None,
        ) -> None:
            assert webhook_event_id is not None
            webhook_event_ids.append(webhook_event_id)

    runner = SyncJobPostCommitEffectRunner(
        publication_finalizer=cast(Any, FakePublicationFinalizer()),
        webhook_outbox=cast(Any, FakeWebhookOutbox()),
    )
    plan = PostCommitEffectPlan.from_success(
        cache_invalidation={
            "job_id": "job_post_commit",
            "user_id": "user-1",
            "namespaces": ["default", "finance"],
        },
        webhook_event_id="event-1",
    )

    runner.run(plan)

    assert cache_invalidations == [
        {
            "job_id": "job_post_commit",
            "user_id": "user-1",
            "namespaces": ["default", "finance"],
        }
    ]
    assert webhook_event_ids == ["event-1"]
