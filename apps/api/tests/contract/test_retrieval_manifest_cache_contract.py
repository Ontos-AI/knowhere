from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

from shared.services.retrieval.manifest_cache import (
    cache_manifest_payloads,
    get_cached_manifest_payloads,
)
from shared.services.retrieval.search.scoped_corpus import count_manifest_chunks


class _SessionWithInfo:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}


def test_manifest_payload_cache_is_scoped_to_revision_pin_set() -> None:
    session = _SessionWithInfo()
    revisions = {"doc-a": "result-a", "doc-b": "result-b"}
    payloads = {
        ("doc-a", "result-a"): {"chunks": [{"chunk_id": "chunk-a"}]},
        ("doc-b", "result-b"): {"chunks": [{"chunk_id": "chunk-b"}]},
    }

    cache_manifest_payloads(session, revisions=revisions, payloads=payloads)

    assert get_cached_manifest_payloads(session, revisions=revisions) == payloads
    assert get_cached_manifest_payloads(
        session,
        revisions={"doc-a": "different-result"},
    ) is None


class _UnavailableManifestSession:
    def __init__(self) -> None:
        self.rollback_count = 0

    async def execute(self, _statement: object) -> object:
        raise SQLAlchemyError("serving manifest table is unavailable")

    async def rollback(self) -> None:
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_manifest_count_falls_back_after_derived_table_error() -> None:
    session = _UnavailableManifestSession()

    result = await count_manifest_chunks(
        session,  # type: ignore[arg-type]
        revision_pins={"doc-a": "result-a"},
    )

    assert result is None
    assert session.rollback_count == 1
