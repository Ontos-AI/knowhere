"""Verify concurrent publication and protected retrieval on real PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.capacity_execution import (  # noqa: E402
    CapacityBatch,
    run_capacity_batch,
)
from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationExecutionRequest,
    PublicationScope,
    build_publication_environment,
    database_url_for_owner,
    executor_for,
)
from scripts.publication_benchmark.retrieval_interference import (  # noqa: E402
    RetrievalProbeSpec,
)


@pytest.mark.parametrize("owner", ("sync", "async"))
@pytest.mark.parametrize("strategy", ("candidate",))
def test_capacity_batch_converges_while_protected_retrieval_stays_stable(
    owner: str,
    strategy: str,
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
    tmp_path: Path,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    environment_keys = build_publication_environment(
        database_url="postgresql://benchmark.invalid/test",
        run_id="capacity-contract",
        strategy=strategy,
    )
    for key, benchmark_value in environment_keys.items():
        monkeypatch.setenv(key, os.environ.get(key, benchmark_value))
    asyncio.run(prepare_contract_storage())
    database_url = (
        make_url(get_contract_database_url())
        .set(drivername="postgresql+psycopg2")
        .render_as_string(hide_password=False)
    )
    user_id = f"capacity-user-{owner}-{strategy}"
    namespace = f"capacity-namespace-{owner}-{strategy}"
    protected = PublicationScope(
        user_ref=user_id,
        namespace_ref=namespace,
        source_file_name="protected.pdf",
        job_ref=f"cap-protected-job-{owner}-{strategy}",
        revision_ref=f"cap-protected-result-{owner}-{strategy}",
    )
    protected_chunks = [
        {
            "chunk_id": "protected-evidence",
            "type": "text",
            "content": "protected retrieval evidence capacity probe",
            "path": "protected.pdf/Root/body",
            "order": 0,
            "metadata": {},
        },
        {
            "chunk_id": "protected-filler-a",
            "type": "text",
            "content": "unrelated astronomy paragraph",
            "path": "protected.pdf/Root/Other/first",
            "order": 1,
            "metadata": {},
        },
        {
            "chunk_id": "protected-filler-b",
            "type": "text",
            "content": "separate ocean paragraph",
            "path": "protected.pdf/Root/Other/second",
            "order": 2,
            "metadata": {},
        },
    ]
    seed = executor_for(owner).execute(
        PublicationExecutionRequest(
            database_url=database_url_for_owner(database_url, owner=owner),
            scope=protected,
            chunks=protected_chunks,
            owner=owner,
            mode="cold",
            redis_namespace="publication-benchmark:capacity-contract",
        )
    )
    assert seed.outcome == "committed"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            document_id = str(
                connection.execute(
                    text("SELECT document_id FROM job_results WHERE id = :revision_id"),
                    {"revision_id": protected.revision_ref},
                ).scalar_one()
            )
    finally:
        engine.dispose()
    batch = CapacityBatch(
        database_url=database_url,
        run_id=f"capacity-{owner}-{strategy}-{tmp_path.name}",
        owner=owner,
        strategy=strategy,
        concurrency=2,
        chunks=[
            {
                "chunk_id": f"batch-{index}",
                "type": "text",
                "content": f"new publication evidence {index}",
                "path": f"batch.pdf/Root/Section{index}/body",
                "order": index,
                "metadata": {},
            }
            for index in range(3)
        ],
        probe_spec=RetrievalProbeSpec(
            user_id=user_id,
            namespace=namespace,
            query="protected retrieval evidence",
            protected_document_ids=(document_id,),
        ),
        probe_rate=1,
        probe_duration_seconds=3,
    )
    result = run_capacity_batch(batch)
    assert result["errors"] == 0, result
    assert result["converged"], result
    assert result["probe_gate"]["status"] == "pass", result
    assert result["probe_covered_publication"], result
    assert result["max_sql_seconds"] > 0, result
    assert result["resource"]["wal_bytes"] > 0, result
    assert sum(result["resource"]["index_size_delta_by_index"].values()) == result[
        "resource"
    ]["index_size_delta_bytes"], result
