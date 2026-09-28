"""Collect and evaluate current RSS after repeated publication in one process.

Collection requires an explicitly listed, small, local test clone whose source
is separate from the September restore. This is a bounded telemetry retention
check, not a production memory or pooled-connection capacity claim.
"""

from __future__ import annotations

import argparse
import gc
import gzip
import json
import os
import secrets
import statistics
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.clone_db import (  # noqa: E402
    DEFAULT_SOURCE_CONTAINER,
    DEFAULT_SOURCE_VOLUME,
    POSTGRES_IMAGE,
)
from scripts.publication_benchmark.clone_state import (  # noqa: E402
    CloneRecord,
    clone_container_name,
    clone_data_volume_name,
    find_listed_clone,
)
from scripts.publication_benchmark.frozen_input import (  # noqa: E402
    FrozenInputExpectations,
    FrozenInputManifest,
    read_frozen_input,
)
from scripts.publication_benchmark.guards import (  # noqa: E402
    BenchmarkGuardError,
    assert_not_source_database,
    read_database_url_file,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.report import (  # noqa: E402
    assert_report_is_redacted,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    resolve_source_content_digest,
)

if TYPE_CHECKING:
    from scripts.publication_benchmark.publication_execution import (
        PublicationExecutionRequest,
        PublicationExecutionResult,
    )

SCHEMA_VERSION: str = "publication-memory-growth/1"
WARMUP_COUNT: int = 10
MEASURED_COUNT: int = 60
WINDOW_SIZE: int = 15
MAX_TEST_DATABASE_BYTES: int = 512 * 1024 * 1024
MAX_CONTINUE_DATABASE_BYTES: int = 384 * 1024 * 1024
MAX_INPUT_BYTES: int = 1024 * 1024
MAX_COMPRESSED_INPUT_BYTES: int = 1024 * 1024
MAX_INPUT_CHUNKS: int = 64
MATERIAL_GROWTH_BYTES: int = 32 * 1024 * 1024


class MemoryGrowthError(RuntimeError):
    """Raised when memory evidence cannot be collected safely."""


def validate_test_clone(
    *, layout: BenchmarkLayout, run_id: str, database_url_path: Path
) -> tuple[CloneRecord, str]:
    """Refuse unlisted, source-backed, remote, or oversized clone targets."""
    resolved_run_id = validate_run_id(run_id)
    if (
        database_url_path.resolve()
        != layout.database_url_path(resolved_run_id).resolve()
    ):
        raise BenchmarkGuardError("memory check requires the listed clone URL file")
    database_url = read_database_url_file(database_url_path, purpose="memory check")
    record = find_listed_clone(
        clones_root=layout.clones_root,
        run_id=resolved_run_id,
        database_url=database_url,
    )
    if (
        record.source.volume_name == DEFAULT_SOURCE_VOLUME
        or record.source.container_name == DEFAULT_SOURCE_CONTAINER
    ):
        raise BenchmarkGuardError("memory check requires a separate small test source")
    if (
        record.data_volume != clone_data_volume_name(resolved_run_id)
        or record.container_name != clone_container_name(resolved_run_id)
        or record.postgres_image != POSTGRES_IMAGE
        or not record.postgres_version.startswith("15.")
        or record.port == record.source.database_url_port
        or record.database_name == record.source.database_name
    ):
        raise BenchmarkGuardError("memory check clone target identity is unsafe")
    url = make_url(database_url)
    if (
        url.host != "127.0.0.1"
        or url.port != record.port
        or url.database != record.database_name
    ):
        raise BenchmarkGuardError(
            "memory check requires the local listed clone endpoint"
        )
    assert_not_source_database(
        database_url, source=record.source, purpose="memory check"
    )
    recorded_size = int(record.database_identity["size_bytes"])
    if recorded_size <= 0 or recorded_size > MAX_CONTINUE_DATABASE_BYTES:
        raise BenchmarkGuardError("memory check requires a small test database")
    return record, database_url


def verify_live_test_database(database_url: str, record: CloneRecord) -> int:
    """Check the recorded database identity and live size before publication."""
    try:
        engine = create_engine(database_url, future=True, poolclass=NullPool)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT (SELECT system_identifier FROM pg_control_system())::text, "
                        "current_database(), "
                        "(SELECT oid FROM pg_database WHERE datname = current_database())::text, "
                        "pg_database_size(current_database())::bigint"
                    )
                ).one()
        finally:
            engine.dispose()
    except SQLAlchemyError:
        raise MemoryGrowthError("could not verify the small test database") from None
    if (
        str(row[0]) != str(record.database_identity["system_identifier"])
        or str(row[1]) != record.database_name
        or str(row[2]) != str(record.database_identity["database_oid"])
        or int(row[3]) > MAX_TEST_DATABASE_BYTES
    ):
        raise BenchmarkGuardError("memory check live database identity or size changed")
    return int(row[3])


def read_small_frozen_input(
    input_directory: Path,
) -> tuple[tuple[Mapping[str, object], ...], str]:
    """Read a bounded frozen input while preserving its digest validation."""
    manifest_path = input_directory / "manifest.json"
    payload_path = input_directory / "chunks.json.gz"
    if not manifest_path.is_file() or not payload_path.is_file():
        raise MemoryGrowthError("small frozen input manifest or payload is missing")
    if payload_path.stat().st_size > MAX_COMPRESSED_INPUT_BYTES:
        raise MemoryGrowthError("small frozen input compressed payload is too large")
    manifest = FrozenInputManifest.from_dict(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    if (
        manifest.payload_bytes > MAX_INPUT_BYTES
        or manifest.counts.chunks < 1
        or manifest.counts.chunks > MAX_INPUT_CHUNKS
        or manifest.counts.text_chunks < 1
        or manifest.counts.image_chunks < 1
        or manifest.counts.table_chunks < 1
    ):
        raise MemoryGrowthError("small frozen input exceeds the bounded test size")
    with gzip.open(payload_path, "rb") as payload_stream:
        if len(payload_stream.read(MAX_INPUT_BYTES + 1)) > MAX_INPUT_BYTES:
            raise MemoryGrowthError("small frozen input expands beyond the test limit")
    counts = manifest.counts
    expectations = FrozenInputExpectations(
        chunks=counts.chunks,
        text_chunks=counts.text_chunks,
        image_chunks=counts.image_chunks,
        table_chunks=counts.table_chunks,
        chunks_with_metadata=counts.chunks_with_metadata,
        chunks_with_artifact_path=counts.chunks_with_artifact_path,
        map_units=counts.map_units,
        token_rows=counts.token_rows,
    )
    payload, validated = read_frozen_input(input_directory, expectations=expectations)
    return tuple(payload["chunks"]), validated.digest


def current_rss_bytes() -> int:
    """Read current Linux process RSS, avoiding high-water RSS counters."""
    try:
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError) as error:
        raise MemoryGrowthError("current process RSS is unavailable") from error


def artifact_path(
    layout: BenchmarkLayout, *, run_id: str, owner: str, trace_enabled: bool
) -> Path:
    """Place each process arm in a separate redacted report file."""
    trace_name = "enabled" if trace_enabled else "disabled"
    return layout.report_directory(run_id) / f"memory-growth-{owner}-{trace_name}.json"


def collect_memory_series(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    database_url_path: Path,
    input_directory: Path,
    owner: str,
    trace_enabled: bool,
    strategy: str,
    execute: Callable[[PublicationExecutionRequest], PublicationExecutionResult]
    | None = None,
    read_rss: Callable[[], int] = current_rss_bytes,
    verify_database: Callable[[str, CloneRecord], int] = verify_live_test_database,
) -> dict[str, object]:
    """Run repeated real publication in one process and save scalar evidence."""
    if owner not in ("sync", "async") or strategy not in ("baseline", "candidate"):
        raise MemoryGrowthError("unsupported owner or publication strategy")
    record, database_url = validate_test_clone(
        layout=layout, run_id=run_id, database_url_path=database_url_path
    )
    chunks, input_digest = read_small_frozen_input(input_directory)
    initial_database_bytes = verify_database(database_url, record)
    if initial_database_bytes > MAX_CONTINUE_DATABASE_BYTES:
        raise BenchmarkGuardError("memory check test database is too large to start")
    # A failed retry must not leave a prior passing arm available for admission.
    artifact_path(
        layout, run_id=run_id, owner=owner, trace_enabled=trace_enabled
    ).unlink(missing_ok=True)
    source_content_digest = resolve_source_content_digest(layout.repository_root)

    from scripts.publication_benchmark.publication_execution import (
        PublicationExecutionRequest,
        PublicationScope,
        apply_publication_environment,
        database_url_for_owner,
        executor_for,
    )

    apply_publication_environment(
        database_url=database_url_for_owner(database_url, owner="async"),
        run_id=run_id,
        strategy=strategy,
    )
    scope_suffix = secrets.token_hex(8)
    scope = PublicationScope(
        user_ref=f"memory-user-{scope_suffix}",
        namespace_ref=f"memory-namespace-{scope_suffix}",
        source_file_name=f"memory-source-{scope_suffix}.pdf",
        job_ref="",
        revision_ref="",
    )
    active_execute = execute or executor_for(owner).execute
    observations: list[dict[str, object]] = []
    series_started_at = cli_support.utc_now_iso()
    previous_chunks = 0
    previous_map_units = 0
    current_database_bytes = initial_database_bytes
    for iteration in range(WARMUP_COUNT + MEASURED_COUNT):
        if current_database_bytes > MAX_CONTINUE_DATABASE_BYTES:
            raise BenchmarkGuardError(
                "memory check test database reached its growth limit"
            )
        iteration_scope = replace(
            scope,
            source_file_name=f"memory-source-{scope_suffix}-{iteration:03d}.pdf",
            job_ref=f"memory-job-{scope_suffix}-{iteration:03d}",
            revision_ref=f"memory-result-{scope_suffix}-{iteration:03d}",
        )
        attempt_ref = f"memory-{run_id}-{owner}-{iteration}"
        request = PublicationExecutionRequest(
            database_url=database_url_for_owner(database_url, owner=owner),
            scope=iteration_scope,
            chunks=chunks,
            owner=owner,
            mode="warm",
            redis_namespace=f"publication-memory:{run_id}",
            trace_enabled=trace_enabled,
            publication_attempt_ref=attempt_ref,
        )
        try:
            result = active_execute(request)
        except Exception:
            raise MemoryGrowthError(
                f"publication iteration {iteration} failed; no admission evidence"
            ) from None
        outcome = result.outcome
        persisted_chunks = int(result.counts.get("persisted_document_chunks", 0))
        persisted_map_units = int(result.counts.get("persisted_map_units", 0))
        submitted_chunks = int(result.counts.get("submitted_chunks", 0))
        sql_statement_count = int(result.sql_observation.get("statement_count", 0))
        has_publication_stages = (
            "chunks_prepare" in result.stage_durations
            and "commit" in result.stage_durations
        )
        terminal_count = int(result.terminal_trace is not None)
        terminal_matches_attempt = (
            result.terminal_trace is not None
            and result.terminal_trace.get("attempt_ref") == attempt_ref
            and result.terminal_trace.get("outcome") == "success"
        )
        terminal_counts = (
            result.terminal_trace.get("counts")
            if result.terminal_trace is not None
            else None
        )
        terminal_chunk_count = (
            int(terminal_counts.get("chunks", 0))
            if isinstance(terminal_counts, Mapping)
            else 0
        )
        del result
        database_size_after = verify_database(database_url, record)
        current_database_bytes = database_size_after
        gc.collect()
        observations.append(
            {
                "iteration": iteration,
                "phase": "warmup" if iteration < WARMUP_COUNT else "measured",
                "outcome": outcome,
                "persisted_chunks": persisted_chunks,
                "persisted_map_units": persisted_map_units,
                "submitted_chunks": submitted_chunks,
                "sql_statement_count": sql_statement_count,
                "has_publication_stages": has_publication_stages,
                "terminal_count": terminal_count,
                "terminal_matches_attempt": terminal_matches_attempt,
                "terminal_chunk_count": terminal_chunk_count,
                "database_size_after_bytes": database_size_after,
                "increased_persisted_state": (
                    persisted_chunks > previous_chunks
                    and persisted_map_units > previous_map_units
                ),
                "rss_after_gc_bytes": read_rss(),
            }
        )
        previous_chunks = persisted_chunks
        previous_map_units = persisted_map_units
    if resolve_source_content_digest(layout.repository_root) != source_content_digest:
        raise MemoryGrowthError("benchmark source changed during memory series")
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "owner": owner,
        "trace_enabled": trace_enabled,
        "strategy": strategy,
        "code_commit": cli_support.resolve_code_commit(layout.repository_root),
        "dependency_lock_digest": cli_support.resolve_dependency_lock_digest(
            layout.repository_root
        ),
        "source_content_digest": source_content_digest,
        "input_digest": input_digest,
        "schema_revision": record.schema_revision,
        "postgres_profile": record.profile,
        "postgres_version": record.postgres_version,
        "postgres_settings_digest": record.settings_digest(),
        "clone_source_digest": record.source.digest,
        "process_id": os.getpid(),
        "series_started_at": series_started_at,
        "warmup_count": WARMUP_COUNT,
        "measured_count": MEASURED_COUNT,
        "input_chunk_count": len(chunks),
        "initial_database_bytes": initial_database_bytes,
        "observations": observations,
    }
    document["gate"] = evaluate_memory_series(document)
    assert_report_is_redacted(document)
    cli_support.write_json(
        artifact_path(layout, run_id=run_id, owner=owner, trace_enabled=trace_enabled),
        document,
    )
    return document


def evaluate_memory_series(document: Mapping[str, object]) -> dict[str, object]:
    """Classify material sustained RSS growth without claiming absence of leaks."""
    rows = document.get("observations")
    if document.get("schema_version") != SCHEMA_VERSION or not isinstance(rows, list):
        return {"status": "insufficient_evidence", "reason": "invalid series artifact"}
    measured = [
        row for row in rows if isinstance(row, dict) and row.get("phase") == "measured"
    ]
    initial_database_bytes = document.get("initial_database_bytes")
    if (
        len(rows) != WARMUP_COUNT + MEASURED_COUNT
        or len(measured) != MEASURED_COUNT
        or not isinstance(initial_database_bytes, int)
        or not 0 < initial_database_bytes <= MAX_CONTINUE_DATABASE_BYTES
        or any(
            not isinstance(row, dict)
            or row.get("iteration") != index
            or row.get("phase") != ("warmup" if index < WARMUP_COUNT else "measured")
            or row.get("outcome") != "committed"
            or not isinstance(row.get("rss_after_gc_bytes"), int)
            or int(row["rss_after_gc_bytes"]) <= 0
            or int(row.get("persisted_chunks") or 0) <= 0
            or int(row.get("persisted_map_units") or 0) <= 0
            or row.get("submitted_chunks") != document.get("input_chunk_count")
            or int(row.get("sql_statement_count") or 0) <= 0
            or row.get("increased_persisted_state") is not True
            or not isinstance(row.get("database_size_after_bytes"), int)
            or int(row["database_size_after_bytes"]) <= 0
            or int(row["database_size_after_bytes"]) > MAX_TEST_DATABASE_BYTES
            or row.get("terminal_count") != int(document.get("trace_enabled") is True)
            or row.get("terminal_matches_attempt")
            is not (document.get("trace_enabled") is True)
            or (
                document.get("trace_enabled") is True
                and (
                    row.get("has_publication_stages") is not True
                    or row.get("terminal_chunk_count")
                    != document.get("input_chunk_count")
                )
            )
            for index, row in enumerate(rows)
        )
    ):
        return {
            "status": "insufficient_evidence",
            "reason": "incomplete or non-committed measured publications",
        }
    chunk_counts = [
        int(row["persisted_chunks"]) for row in rows if isinstance(row, dict)
    ]
    map_unit_counts = [
        int(row["persisted_map_units"]) for row in rows if isinstance(row, dict)
    ]
    if any(
        current_chunks <= prior_chunks or current_units <= prior_units
        for prior_chunks, current_chunks, prior_units, current_units in zip(
            chunk_counts,
            chunk_counts[1:],
            map_unit_counts,
            map_unit_counts[1:],
        )
    ):
        return {
            "status": "insufficient_evidence",
            "reason": "publication did not add persisted state on every iteration",
        }
    values = [int(row["rss_after_gc_bytes"]) for row in measured]
    first_median = statistics.median(values[:WINDOW_SIZE])
    last_median = statistics.median(values[-WINDOW_SIZE:])
    threshold = max(MATERIAL_GROWTH_BYTES, int(first_median * 0.10))
    slopes = [
        (values[end] - values[start]) / (end - start)
        for start in range(len(values))
        for end in range(start + 1, len(values))
    ]
    projected_growth = statistics.median(slopes) * MEASURED_COUNT
    window_growth = last_median - first_median
    if window_growth > threshold and projected_growth > threshold:
        status = "fail"
    elif window_growth <= threshold and projected_growth <= threshold:
        status = "pass"
    else:
        status = "insufficient_evidence"
    return {
        "status": status,
        "reason": "bounded repeated-publication RSS trend; not a leak-free claim",
        "measured_count": MEASURED_COUNT,
        "first_window_median_bytes": first_median,
        "last_window_median_bytes": last_median,
        "window_growth_bytes": window_growth,
        "projected_growth_bytes": projected_growth,
        "material_growth_threshold_bytes": threshold,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the explicit small-clone memory check CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("collect", "evaluate"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner", choices=("sync", "async"), required=True)
    parser.add_argument("--trace", choices=("enabled", "disabled"), required=True)
    parser.add_argument(
        "--strategy", choices=("baseline", "candidate"), default="baseline"
    )
    parser.add_argument("--db-url-file", type=Path)
    parser.add_argument("--input", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Collect or evaluate a bounded memory-growth artifact."""
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    trace_enabled = arguments.trace == "enabled"
    try:
        if arguments.action == "collect":
            if arguments.db_url_file is None or arguments.input is None:
                raise BenchmarkGuardError("collect requires --db-url-file and --input")
            document = collect_memory_series(
                layout=layout,
                run_id=arguments.run_id,
                database_url_path=arguments.db_url_file,
                input_directory=arguments.input,
                owner=arguments.owner,
                trace_enabled=trace_enabled,
                strategy=arguments.strategy,
            )
            gate = document["gate"]
        else:
            path = artifact_path(
                layout,
                run_id=arguments.run_id,
                owner=arguments.owner,
                trace_enabled=trace_enabled,
            )
            document = json.loads(path.read_text(encoding="utf-8"))
            gate = evaluate_memory_series(document)
        if not isinstance(gate, Mapping):
            raise MemoryGrowthError("memory gate result is invalid")
        status = str(gate["status"])
    except (BenchmarkGuardError, MemoryGrowthError, OSError, ValueError) as error:
        cli_support.print_result(
            {"command": "check_memory_growth", "status": "refused", "error": str(error)}
        )
        return cli_support.EXIT_GUARD_REFUSED
    except Exception:
        cli_support.print_result(
            {
                "command": "check_memory_growth",
                "status": "failed",
                "error": "collection or evaluation failed; no admission evidence",
            }
        )
        return cli_support.EXIT_GATE_FAILED
    cli_support.print_result(
        {"command": "check_memory_growth", "status": status, "gate": gate}
    )
    return cli_support.EXIT_OK if status == "pass" else cli_support.EXIT_GATE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
