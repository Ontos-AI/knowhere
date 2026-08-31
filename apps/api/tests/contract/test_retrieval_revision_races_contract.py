"""Deterministic contracts for revision-generation coherence."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.execution.revision_pins import (
    RetrievalRevisionPins,
    is_revision_generation_stable,
)


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
