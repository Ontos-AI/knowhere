"""Deterministic contracts for revision and channel-session coherence."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.execution.revision_pins import (
    RetrievalRevisionPins,
    is_revision_generation_stable,
)
from shared.services.retrieval.search import discovery


@pytest.mark.asyncio
async def test_classic_channels_share_pins_but_use_distinct_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pins = RetrievalRevisionPins(
        revisions={"doc-1": "revision-1"},
        generation=7,
    )
    sessions: list[object] = []
    observed: list[tuple[object, object]] = []

    @asynccontextmanager
    async def fake_context() -> AsyncGenerator[object, None]:
        session = object()
        sessions.append(session)
        yield session

    async def fake_channel(
        db: AsyncSession,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        observed.append((db, kwargs["revision_pins"]))
        return []

    monkeypatch.setattr("shared.core.database.get_db_context", fake_context)
    monkeypatch.setattr(discovery, "path_channel", fake_channel)
    monkeypatch.setattr(discovery, "content_channel", fake_channel)
    monkeypatch.setattr(discovery, "term_channel", fake_channel)

    result = await discovery.bottom_discovery(
        cast(AsyncSession, object()),
        user_id="user-1",
        namespace="namespace-1",
        query="coherent query",
        top_k=3,
        exclude_document_ids=[],
        exclude_sections=[],
        revision_pins=pins,
    )

    assert result.status == "discovery_done"
    assert len(sessions) == 3
    assert len({id(session) for session in sessions}) == 3
    assert len(observed) == 3
    assert {id(session) for session, _pins in observed} == {
        id(session) for session in sessions
    }
    assert all(observed_pins is pins for _session, observed_pins in observed)


class _GenerationResult:
    def __init__(self, value: int | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> int | None:
        return self._value


class _GenerationSession:
    def __init__(self, values: list[int | None]) -> None:
        self._values = iter(values)

    async def execute(self, _statement: object) -> _GenerationResult:
        return _GenerationResult(next(self._values))


@pytest.mark.asyncio
async def test_generation_change_is_detected_before_scoring() -> None:
    pins = RetrievalRevisionPins(revisions={"doc-1": "revision-1"}, generation=7)
    stable = await is_revision_generation_stable(
        cast(AsyncSession, _GenerationSession([7])),
        user_id="user-1",
        namespace="namespace-1",
        pins=pins,
    )
    changed = await is_revision_generation_stable(
        cast(AsyncSession, _GenerationSession([8])),
        user_id="user-1",
        namespace="namespace-1",
        pins=pins,
    )

    assert stable is True
    assert changed is False
