from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import gevent  # noqa: E402
import pytest  # noqa: E402

from app.services.document_parser.support.gevent_map import gevent_map  # noqa: E402
from shared.services.ai import llm_overrides, token_tracking  # noqa: E402


def test_results_keep_input_order() -> None:
    def _slow_square(value: int) -> int:
        gevent.sleep(0.001 * (5 - value))
        return value * value

    assert gevent_map(_slow_square, [1, 2, 3, 4], max_concurrent=4) == [1, 4, 9, 16]


def test_empty_input_spawns_nothing() -> None:
    assert gevent_map(lambda _item: 1 / 0, [], max_concurrent=4) == []


def test_task_greenlet_tracker_and_overrides_reach_pooled_calls() -> None:
    observed: dict[str, object] = {}

    def _probe(_item: int) -> bool:
        token_tracking.record_tokens({"total_tokens": 1}, model="m", task="t")
        return llm_overrides.get_current_llm_overrides() is not None

    def _task() -> None:
        tracker = token_tracking.init_token_tracker()
        llm_overrides.init_llm_overrides(
            {"api_key": "k", "model": "byok-model", "base_url": "https://example.test"}
        )
        try:
            observed["overrides"] = gevent_map(_probe, [1, 2, 3], max_concurrent=2)
            observed["calls"] = tracker.get("calls")
        finally:
            token_tracking.cleanup_token_tracker()
            llm_overrides.cleanup_llm_overrides()

    # Celery's gevent pool runs each task in its own greenlet, like this one.
    gevent.spawn(_task).join()
    assert observed == {"overrides": [True, True, True], "calls": 3}


def test_call_errors_propagate() -> None:
    def _fail(item: int) -> int:
        if item == 2:
            raise ValueError("boom")
        return item

    with pytest.raises(ValueError, match="boom"):
        gevent_map(_fail, [1, 2, 3], max_concurrent=2)
