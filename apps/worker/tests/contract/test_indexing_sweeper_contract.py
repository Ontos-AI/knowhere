"""Indexing recovery remains active when parser age expiration is disabled."""

from contextlib import contextmanager
from collections.abc import Iterator
from unittest.mock import Mock

import pytest


def test_stale_tick_settles_indexing_even_when_parser_expiration_is_disabled(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.tasks import stale_job_sweeper
    from shared.core.config import settings
    from shared.services.retrieval.lexical.settlement import IndexingSettlement

    @contextmanager
    def acquire(*arguments: object, **options: object) -> Iterator[bool]:
        yield True

    sweep: Mock = Mock(return_value={"expired": 2, "deferred": 1})
    monkeypatch.setattr(stale_job_sweeper, "periodic_task_lock", acquire)
    monkeypatch.setattr(settings, "JOB_WAITING_EXPIRE_SECONDS", 0)
    monkeypatch.setattr(settings, "JOB_PROCESSING_EXPIRE_SECONDS", 0)
    monkeypatch.setattr(IndexingSettlement, "sweep", sweep)
    result = stale_job_sweeper.expire_stale_jobs.run()
    sweep.assert_called_once()
    assert result["indexing"] == {"expired": 2, "deferred": 1}
