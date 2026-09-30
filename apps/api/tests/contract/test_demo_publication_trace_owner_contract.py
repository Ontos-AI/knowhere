"""Contract for the demo adapter's publication transaction trace boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from loguru import logger
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

configure_import_environment()
ensure_import_paths()

from app.services.demo.source_catalog import DemoSourceCatalog, DemoSourceDefinition  # noqa: E402
from app.services.demo.canonical_bundle_result import CanonicalDemoBundle  # noqa: E402
from app.services.demo.source_materializer import DemoSourceMaterializer  # noqa: E402
from shared.models.database.demo_materialization import DemoMaterialization  # noqa: E402
from shared.models.database.job import Job  # noqa: E402
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace  # noqa: E402
from shared.services.retrieval.publication_models import PublishedDocumentState  # noqa: E402
from shared.services.retrieval.publication_service import RetrievalPublicationService  # noqa: E402
from shared.services.redis import RedisPublicationSemaphore, RedisService  # noqa: E402


class FakeCatalog:
    def source_directory(self, source: DemoSourceDefinition) -> Path:
        return Path("/tmp")

    def publication_chunks(
        self, source: DemoSourceDefinition
    ) -> list[dict[str, object]]:
        return [{"type": "text", "content": "contract content", "metadata": {}}]


class FakeConnection:
    def __init__(self) -> None:
        self.engine: object = object()
        self.info: dict[str, object] = {}


class FakeDatabase:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.bind: object = object()
        self.connection_value = FakeConnection()
        self.job: Job | None = None

    def add(self, value: object) -> None:
        if isinstance(value, Job):
            self.job = value

    async def connection(self) -> FakeConnection:
        self.actions.append("connection")
        return self.connection_value

    async def flush(self) -> None:
        self.actions.append("flush")

    async def commit(self) -> None:
        self.actions.append("commit")

    async def rollback(self) -> None:
        self.actions.append("rollback")

    async def run_sync(self, operation: Callable[[object], object]) -> object:
        return operation(self)


class FakeSemaphore:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def acquire(self) -> float:
        self.actions.append("acquire")
        return 0.0

    async def release(self) -> None:
        self.actions.append("release")


class FakePublicationService:
    def __init__(self, database: FakeDatabase, *, should_fail: bool = False) -> None:
        self.database = database
        self.should_fail = should_fail
        self.traces: list[PublicationTrace | None] = []

    def publish_document_state(
        self, database: object, **kwargs: object
    ) -> PublishedDocumentState:
        assert database is self.database
        trace = cast(PublicationTrace | None, kwargs["trace"])
        self.traces.append(trace)
        if self.should_fail:
            raise RuntimeError("publication failed")
        assert self.database.job is not None
        metadata = cast(dict[str, str], self.database.job.job_metadata)
        return PublishedDocumentState(
            user_id=str(self.database.job.user_id),
            namespace=metadata["namespace"],
            document_id=metadata["document_id"],
            manifest_payload={"version": "1"},
        )

    def publish_document_graph(self, database: object, **kwargs: object) -> None:
        assert database is self.database
        self.traces.append(cast(PublicationTrace | None, kwargs["trace"]))

    def update_namespace_snapshot(self, database: object, **kwargs: object) -> None:
        assert database is self.database
        self.traces.append(cast(PublicationTrace | None, kwargs["trace"]))


def make_source() -> DemoSourceDefinition:
    return DemoSourceDefinition(
        demo_source_id="demo-contract",
        canonical_document_id="demo-doc-contract",
        title="Contract source.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        asset_directory="contract",
        chunk_count=1,
        examples=(),
    )


def make_materializer(
    publication: FakePublicationService,
    materializer_type: type[DemoSourceMaterializer],
) -> DemoSourceMaterializer:
    materializer = object.__new__(materializer_type)
    materializer._catalog = cast(DemoSourceCatalog, FakeCatalog())
    materializer._publication_service = cast(RetrievalPublicationService, publication)
    materializer._redis_service = cast(RedisService, object())
    return materializer


@pytest.mark.asyncio
@pytest.mark.parametrize("should_fail", [False, True])
async def test_demo_owner_finishes_trace_at_publication_commit_or_rollback(
    monkeypatch: MonkeyPatch,
    should_fail: bool,
) -> None:
    import app.services.demo.source_materializer as materializer_module

    actions: list[str] = []
    database = FakeDatabase(actions)
    publication = FakePublicationService(database, should_fail=should_fail)
    materializer = make_materializer(
        publication,
        materializer_module.DemoSourceMaterializer,
    )
    source = make_source()
    claim = SimpleNamespace(
        document_id=None,
        status="materializing",
        claimed_at=object(),
        updated_at=None,
    )

    async def use_bundle(**kwargs: object) -> CanonicalDemoBundle:
        assert kwargs["demo_source_id"] == source.demo_source_id
        assert kwargs["source_directory"] == Path("/tmp")
        return CanonicalDemoBundle(
            zip_key="zip/123",
            zip_size=123,
            raw_prefix="results/demo-canonical/",
            content_version="v1",
            reused=False,
        )

    monkeypatch.setattr(materializer_module, "_upload_demo_result_bundle", use_bundle)
    monkeypatch.setattr(
        materializer_module,
        "install_publication_trace_sql_instrumentation",
        lambda engine: actions.append("install"),
    )
    monkeypatch.setattr(
        materializer_module,
        "settings",
        SimpleNamespace(
            KNOWHERE_PUBLICATION_TRACE_ENABLED=True,
            DEMO_CANONICAL_BUNDLE_ENABLED=True,
            DEMO_PUBLICATION_PREPARATION_CACHE_ENABLED=False,
        ),
    )
    terminal_payloads: list[dict[str, object]] = []

    def capture_terminal(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["extra"].get("event") == "publication.trace.terminal":
            actions.append("terminal")
            terminal_payloads.append(
                cast(dict[str, object], record["extra"]["publication_trace"])
            )

    sink_id = logger.add(capture_terminal, level="INFO")
    try:
        if should_fail:
            with pytest.raises(RuntimeError, match="publication failed"):
                await materializer._materialize_source(
                    cast(AsyncSession, database),
                    user_id="user_123",
                    namespace="namespace_123",
                    source=source,
                    claim=cast(DemoMaterialization, claim),
                    semaphore=cast(RedisPublicationSemaphore, FakeSemaphore(actions)),
                )
        else:
            await materializer._materialize_source(
                cast(AsyncSession, database),
                user_id="user_123",
                namespace="namespace_123",
                source=source,
                claim=cast(DemoMaterialization, claim),
                semaphore=cast(RedisPublicationSemaphore, FakeSemaphore(actions)),
            )
    finally:
        logger.remove(sink_id)

    assert len(terminal_payloads) == 1
    assert terminal_payloads[0]["owner"] == "async"
    assert terminal_payloads[0]["outcome"] == ("rollback" if should_fail else "success")
    assert terminal_payloads[0]["scope_fingerprint"] != "namespace_123"
    assert database.connection_value.info == {}
    assert actions.index("install") < actions.index("connection")
    if should_fail:
        assert actions[-3:] == ["rollback", "terminal", "release"]
        assert len(publication.traces) == 1
    else:
        assert actions[-4:] == ["flush", "commit", "terminal", "release"]
        assert claim.status == "ready"
        assert actions.index("commit") < actions.index("terminal")
        assert actions.count("commit") == 1
        assert len(publication.traces) == 3
    assert all(trace is not None for trace in publication.traces)
