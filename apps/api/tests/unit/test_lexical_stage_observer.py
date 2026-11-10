"""Opt-in measurements correlate isolated requests without becoming a gate."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import time
from threading import Event, Thread
from typing import cast

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from shared.services.retrieval.lexical.stage_observer import StageObserver


@pytest.fixture
def observer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from shared.core.config import settings
    from shared.services.retrieval.lexical.transition import LexicalTransition
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "APP_ENV", "staging")
    monkeypatch.setattr(LexicalTransition, "getSourceHash", staticmethod(lambda: "a" * 64))
    monkeypatch.setattr(StageObserver, "_configuration", None)
    path: Path = tmp_path / "observer.json"
    journal: Path = tmp_path / "stages.private.jsonl"
    path.write_text(json.dumps({"schema_version": "opensearch-stage-observer-v1", "campaign_id": "campaign",
        "source_hash": "a" * 64, "journal_path": str(journal), "maximum_journal_bytes": 65536}))
    path.chmod(0o600)
    monkeypatch.setenv("KNOWHERE_WORKLOAD_OBSERVER_CONFIG", str(path))
    StageObserver.install()
    return journal


def test_observer_records_actual_durations_and_counts_without_inventing_missing_stages(observer: Path) -> None:
    with StageObserver.bindRequest("campaign:read-one"):
        StageObserver.add("lexical_executions")
        with StageObserver.measure("lexical_ms"):
            time.sleep(0.002)
        StageObserver.recordBulk(123)
    rows: list[dict[str, object]] = [json.loads(line) for line in observer.read_text().splitlines()]
    assert rows[0]["kind"] == "observer_install"
    row = rows[1]
    assert row["campaign_id"] == "campaign" and row["source_hash"] == "a" * 64 and row["id"] == "read-one"
    assert float(str(row["lexical_ms"])) >= 2 and row["lexical_executions"] == 1
    assert row["native_bulk_bytes"] == [123] and "cold_first_ms" not in row and "capacity_wait_ms" not in row
    assert stat.S_IMODE(observer.stat().st_mode) == 0o600


def test_observer_isolates_async_requests_and_rejects_foreign_or_oversized_correlations(observer: Path) -> None:
    async def run(identity: str, count: int) -> None:
        with StageObserver.bindRequest(identity):
            await asyncio.sleep(0)
            StageObserver.add("lexical_executions", count)

    async def campaign() -> None:
        await asyncio.gather(run("campaign:one", 1), run("campaign:two", 2),
            run("foreign:one", 99), run("campaign:" + "x" * 129, 99))
    asyncio.run(campaign())
    rows: list[dict[str, object]] = [json.loads(line) for line in observer.read_text().splitlines()[1:]]
    assert {str(row["id"]): row["lexical_executions"] for row in rows} == {"one": 1, "two": 2}


def test_observer_emit_failure_does_not_change_business_success(observer: Path) -> None:
    observer.unlink()
    observer.mkdir()
    with StageObserver.bindRequest("campaign:write-one", isRetry=True):
        actual: str = "business-completed"
    assert actual == "business-completed"


@pytest.fixture
def localOrigin() -> Iterator[tuple[str, Event, Event]]:
    entered: Event = Event()
    release: Event = Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version: str = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path == "/hold":
                entered.set()
                release.wait(timeout=3)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread: Thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:" + str(server.server_port), entered, release
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=3)


def _lastRecord(journal: Path) -> dict[str, object]:
    return json.loads(journal.read_text().splitlines()[-1])


def test_async_native_http_pool_observes_real_connection_wait(observer: Path, localOrigin: tuple[str, Event, Event]) -> None:
    origin, entered, release = localOrigin

    async def campaign() -> None:
        async with httpx.AsyncClient(base_url=origin, limits=httpx.Limits(max_connections=1), trust_env=False) as client:
            with StageObserver.bindRequest("campaign:async-transport"), StageObserver.bindNativeRequest():
                first = asyncio.create_task(client.get("/hold"))
                assert await asyncio.to_thread(entered.wait, 2)
                second = asyncio.create_task(client.get("/next"))
                await asyncio.sleep(0.06)
                assert not second.done()
                release.set()
                assert all(response.content == b"ok" for response in await asyncio.gather(first, second))
    asyncio.run(campaign())
    assert float(str(_lastRecord(observer)["native_http_pool_wait_ms"])) >= 40


def test_sync_native_http_pool_observes_real_wait_and_ignores_other_http(observer: Path, localOrigin: tuple[str, Event, Event]) -> None:
    origin, entered, release = localOrigin
    with httpx.Client(base_url=origin, limits=httpx.Limits(max_connections=1), trust_env=False) as client:
        with StageObserver.bindRequest("campaign:unrelated-http"):
            assert client.get("/plain").content == b"ok"
        assert "native_http_pool_wait_ms" not in _lastRecord(observer)
        with StageObserver.bindRequest("campaign:sync-transport"), StageObserver.bindNativeRequest():
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(copy_context().run, client.get, "/hold")
                assert entered.wait(timeout=2)
                second = pool.submit(copy_context().run, client.get, "/next")
                time.sleep(0.06)
                assert not second.done()
                release.set()
                assert first.result(timeout=2).content == second.result(timeout=2).content == b"ok"
    assert float(str(_lastRecord(observer)["native_http_pool_wait_ms"])) >= 40


@pytest.mark.parametrize("isCapacityWait", [False, True])
def test_scope_pending_poll_is_not_mislabeled_as_capacity_wait(
    observer: Path, monkeypatch: pytest.MonkeyPatch, isCapacityWait: bool,
) -> None:
    from shared.services.retrieval.lexical import asynccoordination, coordination
    from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    monkeypatch.setattr(asynccoordination, "StageObserver", StageObserver)

    class Database:
        sessions = sessionmaker()
        allocations: int = 0

        async def run(self, operation: object) -> object:
            if getattr(getattr(operation, "func"), "__name__") != "_allocate":
                return None
            self.allocations += 1
            if self.allocations > 1:
                return "index-identity", 7
            return getattr(coordination, "_CAPACITY_WAIT", None) if isCapacityWait else None

    class Native:
        indexName: str = "index"

        async def getLifecycleSnapshot(self, **_options: object) -> None:
            return None

    async def campaign() -> None:
        with StageObserver.bindRequest("campaign:capacity-classification"):
            result = await asynccoordination.AsyncLexicalCoordination(cast(AsyncLexicalDatabase, Database())).ensureIndex(
                scope=LexicalScope(ownerId="owner", namespace="scope"), native=cast(AsyncOpenSearchLexicalIndex, Native()))
            assert result == ("index-identity", 7)
    asyncio.run(campaign())
    row = _lastRecord(observer)
    assert ("capacity_wait_ms" in row) is isCapacityWait
