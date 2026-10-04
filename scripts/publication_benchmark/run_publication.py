"""Run one publication benchmark sample against a listed writable clone.

    uv run python scripts/publication_benchmark/run_publication.py \
      --owner sync \
      --db-url-file .benchmarks/publication/clones/<run-id>/database-url \
      --input .benchmarks/publication/inputs/spacex-s1-production \
      --strategy candidate \
      --mode cold
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.frozen_input import (  # noqa: E402
    SPACEX_S1_PRODUCTION_EXPECTATIONS,
    FrozenInputError,
    FrozenInputExpectations,
    get_prohibited_payload_values,
    read_frozen_input,
)
from scripts.publication_benchmark.guards import (  # noqa: E402
    BenchmarkGuardError,
    assert_frozen_input_digest,
    assert_not_source_database,
    assert_strategy_matches_environment,
    read_database_url_file,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.report import (  # noqa: E402
    ReportContractError,
    assert_report_is_redacted,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PUBLICATION_MODES,
    PUBLICATION_OWNERS,
    PublicationRunRecord,
    RunRecordError,
    host_identity,
    resolve_source_content_digest,
)



def default_run_id() -> str:
    """Return a fresh benchmark run identifier."""
    stamp = cli_support.utc_now_iso().replace(":", "").replace("-", "")[:15]
    return validate_run_id(f"{stamp.lower()}-{uuid4().hex[:6]}")


def run_id_for_database_url_file(
    *, layout: BenchmarkLayout, database_url_file: Path, run_id: str | None
) -> str:
    """Resolve the clone ID from the documented database URL path."""
    resolved_run_id = validate_run_id(run_id or database_url_file.parent.name)
    expected_path = layout.database_url_path(resolved_run_id).resolve()
    if database_url_file.resolve() != expected_path:
        raise BenchmarkGuardError(
            "run_publication: --db-url-file must be the listed clone's "
            "database-url file"
        )
    return resolved_run_id


def run_publication_point(
    *,
    layout: BenchmarkLayout,
    owner: str,
    database_url: str,
    input_directory: Path,
    strategy: str,
    mode: str,
    run_id: str | None = None,
    namespace_ref: str | None = None,
    source_file_ref: str | None = None,
    replacement_document_id: str | None = None,
    executor: Any | None = None,
    trace_enabled: bool = False,
    expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
) -> dict[str, Any]:
    """Execute one publication sample and record its artifacts."""
    source_content_digest = resolve_source_content_digest(layout.repository_root)
    from scripts.publication_benchmark.clone_state import (
        FRESH_CLONE_STATES,
        find_listed_clone,
        write_clone_record,
    )
    from scripts.publication_benchmark.publication_execution import (
        PublicationExecutionError,
        PublicationExecutionRequest,
        PublicationScope,
        apply_publication_environment,
        database_url_for_owner,
        executor_for,
    )

    resolved_run_id = validate_run_id(run_id or default_run_id())
    if owner not in PUBLICATION_OWNERS:
        raise BenchmarkGuardError(
            f"unsupported owner {owner!r}; expected one of {PUBLICATION_OWNERS}"
        )
    if mode not in PUBLICATION_MODES:
        raise BenchmarkGuardError(
            f"unsupported mode {mode!r}; expected one of {PUBLICATION_MODES}"
        )

    payload, manifest = read_frozen_input(
        input_directory,
        expectations=expectations,
    )
    assert_frozen_input_digest(manifest)
    clone_record = find_listed_clone(
        clones_root=layout.clones_root,
        run_id=resolved_run_id,
        database_url=database_url,
    )
    assert_not_source_database(
        database_url,
        source=clone_record.source,
        purpose="run_publication",
    )
    execution_database_url = database_url_for_owner(database_url, owner=owner)
    # ``DATABASE_URL`` is the async canonical form in every deployment; the
    # synchronous worker pool derives its psycopg2 URL from it. Publishing the
    # psycopg2 form here would break shared modules that build an async engine.
    apply_publication_environment(
        database_url=database_url_for_owner(database_url, owner="async"),
        run_id=resolved_run_id,
        strategy=strategy,
    )
    if mode == "cold" and clone_record.state not in FRESH_CLONE_STATES:
        raise BenchmarkGuardError(
            f"run_publication: cold samples require a freshly created clone; "
            f"clone {resolved_run_id!r} is in state {clone_record.state!r}"
        )

    assert_strategy_matches_environment(strategy, "candidate", purpose="run_publication")

    scope_suffix = hashlib.sha256(resolved_run_id.encode("utf-8")).hexdigest()[:16]
    scope = PublicationScope(
        user_ref=f"bench-user-{resolved_run_id}",
        namespace_ref=namespace_ref or f"bench-namespace-{resolved_run_id}",
        source_file_name=source_file_ref or f"benchmark-source-{resolved_run_id}.pdf",
        job_ref=(
            f"bench-rjob-{scope_suffix[:12]}-{uuid4().hex[:4]}"
            if replacement_document_id
            else f"bench-job-{scope_suffix}"
        ),
        revision_ref=(
            f"bench-rres-{scope_suffix[:12]}-{uuid4().hex[:4]}"
            if replacement_document_id
            else f"bench-result-{scope_suffix}"
        ),
        document_id_ref=replacement_document_id,
    )
    sample_id = f"{resolved_run_id}-{owner}-{mode}-{strategy}-{uuid4().hex[:8]}"
    publication_attempt_ref = (
        "attempt-"
        + hashlib.sha256(
            f"{sample_id}|{clone_record.clone_id}".encode("utf-8")
        ).hexdigest()[:16]
    )
    request = PublicationExecutionRequest(
        database_url=execution_database_url,
        scope=scope,
        chunks=tuple(payload["chunks"]),
        owner=owner,
        mode=mode,
        redis_namespace=f"publication-benchmark:{resolved_run_id}",
        trace_enabled=trace_enabled,
        publication_attempt_ref=publication_attempt_ref,
    )
    active_executor = executor or executor_for(owner)
    # Consume the fresh state before any database work. An interrupted or failed
    # attempt cannot leave a dirty clone eligible for another cold sample.
    write_clone_record(
        layout.clone_record_path(resolved_run_id),
        clone_record.with_state("sampled"),
    )
    started_at = cli_support.utc_now_iso()
    execution = active_executor.execute(request)
    ended_at = cli_support.utc_now_iso()
    if resolve_source_content_digest(layout.repository_root) != source_content_digest:
        raise RunRecordError("benchmark source changed during publication")

    record = PublicationRunRecord(
        run_id=resolved_run_id,
        sample_id=sample_id,
        strategy=strategy,
        owner=owner,
        mode=mode,
        code_commit=cli_support.resolve_code_commit(layout.repository_root),
        source_content_digest=source_content_digest,
        dependency_lock_digest=cli_support.resolve_dependency_lock_digest(
            layout.repository_root
        ),
        postgres_profile=clone_record.profile,
        postgres_version=clone_record.postgres_version,
        postgres_settings_digest=clone_record.settings_digest(),
        redis_namespace=request.redis_namespace,
        input_digest=manifest.digest,
        clone_id=clone_record.clone_id,
        clone_source_digest=clone_record.source.digest,
        host_id=host_identity(),
        started_at=started_at,
        ended_at=ended_at,
        outcome=execution.outcome,
        publication_duration_ms=execution.duration_ms,
        publication_attempt_ref=publication_attempt_ref,
        template_content_digest=(
            str(clone_record.database_identity["template_content_digest"])
            if clone_record.database_identity.get("template_content_digest")
            else None
        ),
    )

    prohibited_values = get_prohibited_payload_values(payload) + (
        scope.user_ref,
        scope.namespace_ref,
        scope.source_file_name,
        database_url,
    )
    terminal_trace: dict[str, object] | None = None
    if trace_enabled:
        if execution.terminal_trace is None:
            raise PublicationExecutionError(
                "trace-enabled publication produced no terminal trace"
            )
        terminal_trace = dict(execution.terminal_trace)
        if terminal_trace.get("attempt_ref") != publication_attempt_ref:
            raise PublicationExecutionError(
                "terminal trace attempt reference does not match its run record"
            )
        from scripts.publication_benchmark.report import find_prohibited_values

        if find_prohibited_values(
            terminal_trace,
            prohibited_values=prohibited_values,
        ):
            raise ReportContractError(
                "terminal trace contains prohibited benchmark source values"
            )
    clone_directory = layout.ensure_clone_directory(resolved_run_id)
    state_before = dict(execution.state_before)
    state_after = dict(execution.state_after)
    assert_report_is_redacted(state_before, prohibited_values=prohibited_values)
    assert_report_is_redacted(state_after, prohibited_values=prohibited_values)
    sample_state_before_path = (
        clone_directory / "samples" / sample_id / "state-before.json"
    )
    sample_state_after_path = (
        clone_directory / "samples" / sample_id / "state-after.json"
    )
    cli_support.write_json(sample_state_before_path, state_before)
    cli_support.write_json(sample_state_after_path, state_after)
    cli_support.write_json(
        layout.state_before_path(resolved_run_id),
        state_before,
    )
    cli_support.write_json(
        layout.state_after_path(resolved_run_id),
        state_after,
    )
    cli_support.append_jsonl(
        layout.publication_record_path(resolved_run_id),
        {
            **record.to_dict(),
            "counts": dict(execution.counts),
            "sql_observation": dict(execution.sql_observation),
            "effects": dict(execution.effects),
            "stage_durations": dict(execution.stage_durations),
            "background_activity": dict(execution.background_activity),
            "payload_schema_version": manifest.payload_schema_version,
            "corpus_fingerprint": manifest.corpus_fingerprint,
            "trace_enabled": trace_enabled,
            "state_before_file": str(sample_state_before_path),
            "state_after_file": str(sample_state_after_path),
        },
    )
    if terminal_trace is not None:
        cli_support.append_jsonl(
            clone_directory / "publication-traces.jsonl",
            terminal_trace,
        )
        terminal_events = cli_support.read_jsonl(
            clone_directory / "publication-traces.jsonl"
        )
        cli_support.write_json(
            clone_directory / "trace-accounting.json",
            {
                "terminal_event_attempt_refs": [
                    str(event["attempt_ref"]) for event in terminal_events
                ]
            },
        )
    return {
        "command": "run_publication",
        "status": "ok",
        "clone_directory": str(clone_directory),
        "record": record.to_dict(),
        "counts": dict(execution.counts),
        "effects": dict(execution.effects),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the run_publication command line parser."""
    parser = argparse.ArgumentParser(
        description="Run one publication benchmark sample",
    )
    parser.add_argument("--owner", required=True, choices=PUBLICATION_OWNERS)
    parser.add_argument("--db-url-file", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--strategy",
        required=True,
        choices=("candidate",),
    )
    parser.add_argument("--mode", required=True, choices=PUBLICATION_MODES)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--namespace-ref",
        default=None,
        help="Existing clone namespace to publish into (default: fresh scope)",
    )
    parser.add_argument("--source-file-ref", default=None)
    parser.add_argument(
        "--replacement-document-id",
        default=None,
        help="Explicitly publish a new revision for this existing document ID",
    )
    parser.add_argument(
        "--trace",
        choices=("disabled", "enabled"),
        default="disabled",
        help="Enable measurement-only PublicationTrace for this sample",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the run_publication command."""
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    try:
        database_url = read_database_url_file(
            arguments.db_url_file,
            purpose="run_publication",
        )
        result = run_publication_point(
            layout=layout,
            owner=arguments.owner,
            database_url=database_url,
            input_directory=arguments.input,
            strategy=arguments.strategy,
            mode=arguments.mode,
            run_id=run_id_for_database_url_file(
                layout=layout,
                database_url_file=arguments.db_url_file,
                run_id=arguments.run_id,
            ),
            namespace_ref=arguments.namespace_ref,
            source_file_ref=arguments.source_file_ref,
            replacement_document_id=arguments.replacement_document_id,
            trace_enabled=arguments.trace == "enabled",
        )
    except (
        BenchmarkGuardError,
        FrozenInputError,
        ReportContractError,
        RunRecordError,
    ) as error:
        cli_support.print_result(
            {
                "command": "run_publication",
                "status": "failed",
                "error": str(error),
            }
        )
        return cli_support.EXIT_GUARD_REFUSED
    cli_support.print_result(result)
    return cli_support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
