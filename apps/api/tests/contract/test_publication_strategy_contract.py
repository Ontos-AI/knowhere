"""Contract tests for the temporary global Publication strategy setting."""

# ruff: noqa: E402

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from loguru import logger
from pydantic import ValidationError

from tests.support.publication_benchmark_support import (
    REPOSITORY_ROOT,
    ensure_benchmark_import_path,
)

ensure_benchmark_import_path()

from shared.core.config import settings  # noqa: E402
from shared.core.config.publication import (  # noqa: E402
    DEFAULT_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES,
    DEFAULT_PUBLICATION_STRATEGY,
    PUBLICATION_STRATEGIES,
    PUBLICATION_STRATEGY_ENV_VAR,
    PublicationConfig,
    PublicationStrategyError,
    normalize_publication_strategy,
)
from shared.services.retrieval.publication_strategy import (  # noqa: E402
    PublicationStrategyAnnouncer,
    announce_publication_strategy,
    resolve_publication_strategy,
)


def test_publication_strategy_contract_defaults_to_baseline() -> None:
    assert DEFAULT_PUBLICATION_STRATEGY == "baseline"
    assert PUBLICATION_STRATEGIES == ("baseline", "candidate")
    assert settings.KNOWHERE_PUBLICATION_STRATEGY in PUBLICATION_STRATEGIES
    assert (
        settings.KNOWHERE_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES
        == DEFAULT_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES
    )


def test_publication_snapshot_bound_accepts_zero_to_disable() -> None:
    config = PublicationConfig(
        KNOWHERE_PUBLICATION_STRATEGY="candidate",
        KNOWHERE_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES=0,
    )
    assert config.KNOWHERE_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES == 0


def test_publication_config_rejects_unsupported_strategy_values() -> None:
    config = PublicationConfig(KNOWHERE_PUBLICATION_STRATEGY="candidate")
    assert config.KNOWHERE_PUBLICATION_STRATEGY == "candidate"

    with pytest.raises(ValidationError) as error:
        PublicationConfig(KNOWHERE_PUBLICATION_STRATEGY="legacy")

    assert PUBLICATION_STRATEGY_ENV_VAR in str(error.value)
    with pytest.raises(PublicationStrategyError):
        normalize_publication_strategy("percent-50")


def test_strategy_resolver_accepts_no_override_argument() -> None:
    assert inspect.signature(resolve_publication_strategy).parameters == {}


def test_strategy_resolver_reads_the_live_global_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", "candidate")
    assert resolve_publication_strategy() == "candidate"

    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", "legacy")
    with pytest.raises(PublicationStrategyError):
        resolve_publication_strategy()


def test_strategy_announcer_emits_one_event_per_process() -> None:
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]),
        level="INFO",
    )
    try:
        announcer = PublicationStrategyAnnouncer()
        first = announcer.announce(service_name="knowhere-worker")
        second = announcer.announce(service_name="knowhere-worker")
    finally:
        logger.remove(sink_id)

    assert first == second == settings.KNOWHERE_PUBLICATION_STRATEGY
    assert len(messages) == 1
    assert "publication strategy" in messages[0]
    assert PUBLICATION_STRATEGY_ENV_VAR in messages[0]


def test_strategy_announcement_singleton_is_exposed() -> None:
    assert announce_publication_strategy is not None
    assert resolve_publication_strategy() in PUBLICATION_STRATEGIES


def test_worker_and_api_announce_strategy_at_startup() -> None:
    repository_root = Path(REPOSITORY_ROOT)
    worker_source = (
        repository_root / "apps/worker/app/core/worker_bootstrap.py"
    ).read_text(encoding="utf-8")
    api_source = (repository_root / "apps/api/main.py").read_text(encoding="utf-8")

    assert "announce_publication_strategy" in worker_source
    assert "announce_publication_strategy" in api_source
