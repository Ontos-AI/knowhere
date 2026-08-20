from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.exceptions.domain_exceptions import (  # noqa: E402
    LLMServiceException,
    UnavailableException,
)
import shared.services.ai.openai_compatible_client_sync as client_mod  # noqa: E402
from shared.services.ai.page_memory_vlm_limiter import (  # noqa: E402
    PageMemoryVlmLease,
    PageMemoryVlmLimiter,
)
import shared.services.ai.summary.engine as summary_engine  # noqa: E402


class _FakeRedis:
    def __init__(self, *, initial_count: int = 0, fail_eval: bool = False) -> None:
        self.count = initial_count
        self.fail_eval = fail_eval

    def eval(self, script: str, keys: list[str], args: list[Any]) -> list[int] | int:
        if self.fail_eval:
            raise RuntimeError("redis unavailable")
        if "INCR" in script:
            max_inflight = int(args[0])
            if self.count >= max_inflight:
                return [0, self.count]
            self.count += 1
            return [1, self.count]
        self.count = max(self.count - 1, 0)
        return self.count

    def get(self, key: str, default: Any = None) -> int:
        return self.count


class _DelayedCapacityRedis(_FakeRedis):
    def __init__(self) -> None:
        super().__init__(initial_count=1)
        self.acquire_attempts = 0

    def eval(self, script: str, keys: list[str], args: list[Any]) -> list[int] | int:
        if "INCR" not in script:
            return super().eval(script, keys, args)
        self.acquire_attempts += 1
        if self.acquire_attempts == 1:
            return [0, 1]
        self.count = 1
        return [1, 1]


def _build_limiter(redis: _FakeRedis) -> PageMemoryVlmLimiter:
    limiter = PageMemoryVlmLimiter(
        redis,
        max_inflight=1,
        lease_ttl_seconds=60,
        wait_timeout_seconds=1,
    )
    limiter._jittered_backoff = lambda current_inflight: 0.001  # type: ignore[method-assign]
    return limiter


def test_page_memory_vlm_limiter_acquire_release_updates_inflight_count() -> None:
    redis = _FakeRedis()
    limiter = _build_limiter(redis)

    lease = limiter.acquire(usage_task="page_memory.tag")
    assert lease.current_inflight == 1
    assert limiter.get_inflight_count() == 1

    limiter.release(lease)
    assert limiter.get_inflight_count() == 0


def test_page_memory_vlm_limiter_waits_then_succeeds() -> None:
    redis = _DelayedCapacityRedis()
    limiter = _build_limiter(redis)

    lease = limiter.acquire(usage_task="page_memory.title_detection")

    assert lease.current_inflight == 1
    assert redis.acquire_attempts == 2


def test_page_memory_vlm_limiter_timeout_raises_unavailable() -> None:
    redis = _FakeRedis(initial_count=1)
    limiter = PageMemoryVlmLimiter(
        redis,
        max_inflight=1,
        lease_ttl_seconds=60,
        wait_timeout_seconds=1,
    )
    limiter._jittered_backoff = lambda current_inflight: 1.1  # type: ignore[method-assign]

    with pytest.raises(UnavailableException):
        limiter.acquire(usage_task="page_memory.node_ocr")


def test_page_memory_vlm_limiter_redis_failure_raises_unavailable() -> None:
    limiter = _build_limiter(_FakeRedis(fail_eval=True))

    with pytest.raises(UnavailableException):
        limiter.acquire(usage_task="page_memory.node_summary")


def test_page_memory_provider_exception_still_releases_lease(monkeypatch) -> None:
    class _FakeLimiter:
        def __init__(self) -> None:
            self.release_count = 0

        def acquire(self, *, usage_task: str) -> PageMemoryVlmLease:
            return PageMemoryVlmLease(
                usage_task=usage_task,
                acquired_at=0.0,
                current_inflight=1,
            )

        def release(self, lease: PageMemoryVlmLease) -> None:
            self.release_count += 1

    class _FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("provider failed")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeSdkClient:
        chat = _FakeChat()
        base_url = "http://provider.example"

    fake_limiter = _FakeLimiter()
    monkeypatch.setattr(
        client_mod,
        "get_page_memory_vlm_limiter",
        lambda: fake_limiter,
    )
    client = client_mod.OpenAICompatibleClientSync(
        api_key="test",
        api_url="http://provider.example/v1",
        default_model="deepseek-test",
    )
    client._client = _FakeSdkClient()  # type: ignore[assignment]

    with pytest.raises(LLMServiceException):
        client.chat_completion_with_usage(
            messages="hello",
            usage_task="page_memory.tag",
        )

    assert fake_limiter.release_count == 1


def test_page_memory_summary_unavailable_exception_propagates(
    monkeypatch,
    tmp_path,
) -> None:
    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs: Any) -> Any:
            raise UnavailableException(
                internal_message="capacity busy",
                retry_after=5,
            )

    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"png")

    monkeypatch.setattr(
        summary_engine._client_mod,
        "get_openai_client",
        lambda model=None, api_key=None, api_url=None, **_kwargs: _FakeClient(),
    )

    with pytest.raises(UnavailableException):
        summary_engine.summarize(
            mode="page",
            image_paths=[str(image_path)],
            model="fake-vlm",
            usage_task="page_memory.node_summary",
        )


def test_page_memory_transcription_unavailable_exception_propagates(
    monkeypatch,
    tmp_path,
) -> None:
    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs: Any) -> Any:
            raise UnavailableException(
                internal_message="capacity busy",
                retry_after=5,
            )

    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"png")

    monkeypatch.setattr(
        summary_engine._client_mod,
        "get_openai_client",
        lambda model=None, api_key=None, api_url=None, **_kwargs: _FakeClient(),
    )

    with pytest.raises(UnavailableException):
        summary_engine.transcribe(
            image_paths=[str(image_path)],
            model="fake-vlm",
            usage_task="page_memory.node_ocr",
        )
