"""Contract checks for the read-only Phase 5 retrieval probe and pair gate."""

from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.testing.contract_runtime import get_contract_database_url
from tests.contract.test_retrieval_classic_map_unit_contract import _publish_document

from tests.support.publication_benchmark_support import (
    REPOSITORY_ROOT,
    ensure_benchmark_import_path,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.retrieval_interference import (  # noqa: E402
    RetrievalProbeError,
    RetrievalProbeSample,
    RetrievalProbeSpec,
    execute_retrieval_probe,
    evaluate_interference_pair,
    evaluate_probe_samples,
    probe_samples_cover_publication,
)


def test_probe_gate_rejects_semantic_drift_and_timeout() -> None:
    stable = RetrievalProbeSample(0, 1, 20, "result-a", "revision-a", 3)
    changed = RetrievalProbeSample(1000, 1, 21, "result-b", "revision-a", 3)
    timed_out = RetrievalProbeSample(2000, 1, 20_000, None, None, 0, "timeout")

    assert evaluate_probe_samples([stable])["status"] == "pass"
    result = evaluate_probe_samples([stable, changed, timed_out])
    assert result["status"] == "fail"
    assert any("result digests differ" in failure for failure in result["failures"])
    assert any("timeout" in failure for failure in result["failures"])


def test_probe_coverage_uses_actual_request_window() -> None:
    first = RetrievalProbeSample(
        0, 0, 100, "result-a", "revision-a", 3,
        actual_start_offset_ms=0,
        actual_end_offset_ms=100,
    )
    delayed_tail = RetrievalProbeSample(
        2000, 200, 100, "result-a", "revision-a", 3,
        actual_start_offset_ms=2200,
        actual_end_offset_ms=2300,
    )
    assert probe_samples_cover_publication(
        [first, delayed_tail],
        probe_origin=100.0,
        publication_started_at=100.1,
        publication_ended_at=102.15,
    )
    assert not probe_samples_cover_publication(
        [first, delayed_tail],
        probe_origin=100.0,
        publication_started_at=100.1,
        publication_ended_at=102.35,
    )
    assert not probe_samples_cover_publication(
        [delayed_tail],
        probe_origin=100.0,
        publication_started_at=100.1,
        publication_ended_at=102.15,
    )


def test_pair_gate_requires_equivalent_load_and_p95_within_ten_percent() -> None:
    baseline = {
        "spec_digest": "spec-a",
        "rate_per_second": 1,
        "duration_seconds": 30,
        "template_digest": "template-a",
        "host_id": "host-a",
        "postgres_settings_digest": "settings-a",
        "clone_source_digest": "source-a",
        "owner": "sync",
        "strategy": "baseline",
        "concurrency": 4,
        "gate": {"status": "pass"},
        "p95_duration_ms": 100,
    }
    candidate = {**baseline, "strategy": "candidate", "p95_duration_ms": 110}
    assert evaluate_interference_pair(
        baseline_report=baseline, candidate_report=candidate
    )["status"] == "pass"

    candidate = {**candidate, "p95_duration_ms": 111, "rate_per_second": 3}
    failed = evaluate_interference_pair(
        baseline_report=baseline, candidate_report=candidate
    )
    assert failed["status"] == "fail"
    assert any("rate_per_second" in failure for failure in failed["failures"])
    assert any("p95" in failure for failure in failed["failures"])


def test_probe_spec_requires_protected_documents_and_query(tmp_path: Path) -> None:
    path = tmp_path / "probe.json"
    path.write_text(
        '{"user_id":"user","namespace":"namespace","query":"",'
        '"protected_document_ids":["document"]}',
        encoding="utf-8",
    )
    with pytest.raises(RetrievalProbeError, match="query"):
        RetrievalProbeSpec.from_path(path)


def test_retrieval_probe_command_boots() -> None:
    command = Path(REPOSITORY_ROOT) / "scripts/publication_benchmark/run_retrieval_probe.py"
    completed = subprocess.run(
        [sys.executable, str(command), "--help"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--expected-report" in completed.stdout


def test_retrieval_probe_command_configures_benchmark_environment_before_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = import_module("scripts.publication_benchmark.run_retrieval_probe")
    database_url = "postgresql+asyncpg://benchmark@localhost/benchmark"
    calls: list[str] = []
    layout = SimpleNamespace(
        clones_root=tmp_path,
        database_url_path=lambda _run_id: tmp_path / "database-url",
        ensure_report_directory=lambda _report_id: tmp_path,
    )
    clone = SimpleNamespace(
        source=SimpleNamespace(digest="source"),
        clone_id="clone",
        database_identity={"template_content_digest": "template"},
        settings_digest=lambda: "settings",
    )
    spec = RetrievalProbeSpec("user", "namespace", "query", ("document",))

    def configure_environment(*, database_url: str, run_id: str, strategy: str) -> None:
        assert database_url == "postgresql+asyncpg://benchmark@localhost/benchmark"
        assert run_id == "probe-test"
        assert strategy == "candidate"
        calls.append("environment")

    async def probe(**_kwargs: object) -> list[RetrievalProbeSample]:
        assert calls == ["environment"]
        return [RetrievalProbeSample(0, 0, 10, "result", "revision", 1)]

    monkeypatch.setattr(command.BenchmarkLayout, "from_path", lambda: layout)
    monkeypatch.setattr(command, "read_database_url_file", lambda *_args, **_kwargs: database_url)
    monkeypatch.setattr(command, "find_listed_clone", lambda **_kwargs: clone)
    monkeypatch.setattr(command, "assert_not_source_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "apply_publication_environment", configure_environment)
    monkeypatch.setattr(command.RetrievalProbeSpec, "from_path", lambda _path: spec)
    monkeypatch.setattr(command, "run_open_loop_probes", probe)
    monkeypatch.setattr(command, "host_identity", lambda: "host")
    monkeypatch.setattr(command.cli_support, "print_result", lambda _result: None)

    assert command.main([
        "--run-id", "probe-test", "--probe-spec", str(tmp_path / "probe.json"),
        "--owner", "sync", "--strategy", "candidate", "--concurrency", "1",
        "--duration", "1", "--report-id", "probe-report",
    ]) == 0
    assert calls == ["environment"]


async def test_probe_reads_pinned_document_while_other_document_is_published(
    monkeypatch: pytest.MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    discovery_module = import_module(
        "shared.services.retrieval.search.map_unit_discovery"
    )

    def reject_redis_readiness_write(**_kwargs: object) -> None:
        raise AssertionError("retrieval probe scheduled a Redis readiness write")

    monkeypatch.setattr(
        discovery_module, "_schedule_index_readiness", reject_redis_readiness_write
    )
    identifier = uuid4().hex[:8]
    namespace = f"interference-{identifier}"
    async with developer_api_client_factory():
        protected = await _publish_document(
            namespace=namespace,
            source_file_name="protected.pdf",
            chunks=[
                {
                    "chunk_id": f"protected-{identifier}",
                    "type": "text",
                    "content": "stable protected evidence marker",
                    "path": "protected.pdf/Root/Section/body",
                    "order": 1,
                    "metadata": {},
                },
                {
                    "chunk_id": f"filler-a-{identifier}",
                    "type": "text",
                    "content": "unrelated astronomy paragraph",
                    "path": "protected.pdf/Root/Other/first",
                    "order": 2,
                    "metadata": {},
                },
                {
                    "chunk_id": f"filler-b-{identifier}",
                    "type": "text",
                    "content": "separate ocean paragraph",
                    "path": "protected.pdf/Root/Other/second",
                    "order": 3,
                    "metadata": {},
                },
            ],
        )
        spec = RetrievalProbeSpec(
            user_id="local-dev-user",
            namespace=namespace,
            query="stable protected evidence marker",
            protected_document_ids=(protected["document_id"],),
        )
        engine = create_async_engine(get_contract_database_url())
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            before = await execute_retrieval_probe(session_factory=session_factory, spec=spec)
            await _publish_document(
                namespace=namespace,
                source_file_name="unrelated.pdf",
                chunks=[
                    {
                        "chunk_id": f"unrelated-{identifier}",
                        "type": "text",
                        "content": "stable protected evidence marker",
                        "path": "unrelated.pdf/Root/Section/body",
                        "order": 1,
                        "metadata": {},
                    }
                ],
            )
            after = await execute_retrieval_probe(session_factory=session_factory, spec=spec)
        finally:
            await engine.dispose()
    assert before == after
    assert before[2] > 0
