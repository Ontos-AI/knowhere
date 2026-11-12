from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import gevent  # noqa: E402
from gevent.pool import Pool  # noqa: E402

from shared.services.ai import llm_overrides, token_tracking  # noqa: E402
from shared.utils.greenlet_lineage import greenlet_lineage  # noqa: E402


def test_lineage_reaches_the_spawning_task_through_nested_pools() -> None:
    observed: dict[str, bool] = {}

    def _task() -> None:
        task_greenlet = gevent.getcurrent()

        def _grandchild() -> bool:
            return any(greenlet is task_greenlet for greenlet in greenlet_lineage())

        def _child() -> bool:
            return Pool(1).spawn(_grandchild).get()

        observed["reached"] = Pool(2).spawn(_child).get()

    gevent.spawn(_task).join()
    assert observed == {"reached": True}


def test_pooled_calls_inside_a_task_greenlet_see_task_state() -> None:
    observed: dict[str, object] = {}

    def _pooled(_item: int) -> bool:
        token_tracking.record_tokens({"total_tokens": 1}, model="m", task="t")
        return llm_overrides.get_current_llm_overrides() is not None

    def _task() -> None:
        tracker = token_tracking.init_token_tracker()
        llm_overrides.init_llm_overrides(
            {"api_key": "k", "model": "byok-model", "base_url": "https://example.test"}
        )
        try:
            pool = Pool(2)
            greenlets = [pool.spawn(_pooled, item) for item in range(3)]
            gevent.joinall(greenlets, raise_error=True)
            observed["overrides"] = [greenlet.value for greenlet in greenlets]
            observed["calls"] = tracker.get("calls")
        finally:
            token_tracking.cleanup_token_tracker()
            llm_overrides.cleanup_llm_overrides()

    # Celery's gevent pool runs each task in its own greenlet, like this one.
    gevent.spawn(_task).join()
    assert observed == {"overrides": [True, True, True], "calls": 3}
