"""Publication execution adapters for benchmark samples.

A sample replays the frozen parsed chunks through the real shared publication
path, from just before the chunks enter shared Publication until the owning
transaction commits. Two transaction owners are exercised: the worker
synchronous SQLAlchemy/psycopg2 path and the API ``AsyncSession.run_sync``
path used by featured-project materialization.

Post-commit effects are recorded as intents only. Phase 0 runs never enqueue a
webhook or touch a production Redis namespace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol, Sequence, cast
from contextlib import nullcontext

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from scripts.publication_benchmark.state_snapshot import capture_state_snapshot

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace

PUBLICATION_ATTEMPT_SCHEMA_VERSION: str = "publication-attempt/1"
OWNER_DRIVERS: dict[str, str] = {
    "sync": "postgresql+psycopg2",
    "async": "postgresql+asyncpg",
}


class PublicationExecutionError(RuntimeError):
    """Raised when a benchmark publication sample cannot be executed."""


def database_url_for_owner(database_url: str, *, owner: str) -> str:
    """Return the clone URL with the driver the transaction owner needs."""
    driver = OWNER_DRIVERS.get(owner)
    if driver is None:
        raise PublicationExecutionError(
            f"unsupported publication owner {owner!r}; "
            f"expected one of {tuple(OWNER_DRIVERS)}"
        )
    return (
        make_url(database_url)
        .set(drivername=driver)
        .render_as_string(hide_password=False)
    )


def build_publication_environment(
    *,
    database_url: str,
    run_id: str,
    strategy: str,
) -> dict[str, str]:
    """Build the benchmark-only process environment for one sample.

    Publication replays parsed chunks, so it needs the clone database and
    nothing else. Storage, LLM, and telemetry values are inert local
    placeholders: no production credential or endpoint is ever required or
    copied into a benchmark process.
    """
    scratch_root = f"/tmp/knowhere-publication-benchmark/{run_id}"
    return {
        "DATABASE_URL": database_url,
        "DB_SSL_MODE": "disable",
        "TMP_PATH": scratch_root,
        "S3_TYPE": "filesystem",
        "S3_BUCKET_NAME": "knowhere-publication-benchmark",
        "S3_ACCESS_KEY_ID": "benchmark-access-key",
        "S3_SECRET_ACCESS_KEY": "benchmark-secret-key",
        "S3_TEMP_PATH": scratch_root,
        "OBJECT_STORAGE_LOCAL_ROOT": scratch_root,
        "DS_KEY": "benchmark-placeholder-key",
        "DS_URL": "https://benchmark.invalid/v1",
        "TELEMETRY_ENABLED": "false",
        "LOGFIRE_TOKEN": "",
        "KNOWHERE_PUBLICATION_STRATEGY": strategy,
    }


def apply_publication_environment(
    *,
    database_url: str,
    run_id: str,
    strategy: str,
) -> dict[str, str]:
    """Apply the benchmark environment to this process and return it."""
    import os

    environment = build_publication_environment(
        database_url=database_url,
        run_id=run_id,
        strategy=strategy,
    )
    for key, value in environment.items():
        os.environ[key] = value
    return environment


def opaque_ref(value: object) -> str:
    """Return an opaque reference for one benchmark identifier."""
    return "op-" + sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PublicationScope:
    """Synthetic benchmark scope seeded inside one writable clone."""

    user_ref: str
    namespace_ref: str
    source_file_name: str
    job_ref: str
    revision_ref: str
    document_id_ref: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "user_ref": self.user_ref,
            "namespace_ref": self.namespace_ref,
            "source_file_name": self.source_file_name,
            "job_ref": self.job_ref,
            "revision_ref": self.revision_ref,
            "document_id_ref": self.document_id_ref,
        }


@dataclass(frozen=True)
class PublicationExecutionRequest:
    """One publication sample to execute against a writable clone."""

    database_url: str
    scope: PublicationScope
    chunks: Sequence[Mapping[str, Any]]
    owner: str
    mode: str
    redis_namespace: str
    trace_enabled: bool = False
    publication_attempt_ref: str | None = None


@dataclass(frozen=True)
class PublicationExecutionResult:
    """Observed outcome of one publication sample."""

    outcome: str
    duration_ms: float
    state_before: Mapping[str, Any]
    state_after: Mapping[str, Any]
    counts: Mapping[str, int]
    effects: Mapping[str, Any]
    stage_durations: Mapping[str, float]
    background_activity: Mapping[str, Any] = field(default_factory=dict)
    sql_observation: Mapping[str, int | bool] = field(default_factory=dict)
    failure_reason: str | None = None
    terminal_trace: Mapping[str, object] | None = None


class BenchmarkSqlObservation:
    """Count publication SQL identically with trace enabled or disabled.

    One benchmark sample owns one engine and runs one publication at a time.
    The listener is installed before the engine creates any Connection; the
    active window excludes fixture seeding and state snapshots. It performs
    no SQL and retains neither statements nor parameter values.
    """

    def __init__(self, engine: Engine | AsyncEngine) -> None:
        self.statement_count: int = 0
        self.write_statement_count: int = 0
        self.write_row_count: int = 0
        self.write_row_count_complete: bool = True
        self._active: bool = False
        sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        event.listen(sync_engine, "before_cursor_execute", self._before_cursor_execute)
        event.listen(sync_engine, "after_cursor_execute", self._after_cursor_execute)
        event.listen(sync_engine, "handle_error", self._handle_error)

    def start(self) -> None:
        if self._active:
            raise RuntimeError("benchmark SQL observation is already active")
        self._active = True

    def stop(self) -> None:
        self._active = False

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "statement_count": self.statement_count,
            "write_statement_count": self.write_statement_count,
            "write_row_count": self.write_row_count,
            "write_row_count_complete": self.write_row_count_complete,
        }

    def record_manual_write(self, *, write_row_count: int | None) -> None:
        """Account for a write performed through a driver COPY adapter."""
        if not self._active:
            return
        self.write_statement_count += 1
        if isinstance(write_row_count, int) and write_row_count >= 0:
            self.write_row_count += write_row_count
        else:
            self.write_row_count_complete = False

    def _before_cursor_execute(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, executemany
        if not self._active:
            return
        self.statement_count += 1
        if _is_write_statement(context, statement):
            self.write_statement_count += 1

    def _after_cursor_execute(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection
        if not self._active or not _is_write_statement(context, statement):
            return
        rowcount = getattr(cursor, "rowcount", -1)
        if isinstance(rowcount, int) and rowcount >= 0:
            self.write_row_count += rowcount
            return
        inferred_row_count = _infer_write_row_count(
            context=context,
            parameters=parameters,
            executemany=executemany,
        )
        if inferred_row_count is not None:
            self.write_row_count += inferred_row_count
        # asyncpg reports ``-1`` for successful INSERT ... RETURNING batches.
        # The statement completed successfully, so this is not evidence of a
        # failed write. COPY adapters provide their exact row counts through
        # ``record_manual_write``; failed statements are marked incomplete by
        # ``_handle_error``.

    def _handle_error(self, exception_context: object) -> None:
        if not self._active:
            return
        context = getattr(exception_context, "execution_context", None)
        statement = getattr(exception_context, "statement", None)
        if isinstance(statement, str) and _is_write_statement(context, statement):
            self.write_row_count_complete = False


def _is_write_statement(context: object, statement: str) -> bool:
    if any(
        bool(getattr(context, attribute, False))
        for attribute in ("isinsert", "isupdate", "isdelete")
    ):
        return True
    leading_keyword = (
        statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
    )
    return leading_keyword in {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "TRUNCATE",
        "COPY",
    }


def _infer_write_row_count(
    *,
    context: object,
    parameters: object,
    executemany: bool,
) -> int | None:
    """Recover successful INSERT batch size when the driver reports ``-1``.

    asyncpg exposes ``-1`` for SQLAlchemy's INSERT ... RETURNING batches. The
    compiled execution context still carries one parameter mapping per row;
    using that count keeps SQL observation complete without storing SQL or
    bind values. A single INSERT has one affected row when its context marks it
    as an insert. Unknown UPDATE/DELETE counts remain intentionally unknown.
    """
    compiled_parameters = getattr(context, "compiled_parameters", None)
    if isinstance(compiled_parameters, (list, tuple)) and compiled_parameters:
        if executemany or len(compiled_parameters) > 1:
            return len(compiled_parameters)
    if executemany and isinstance(parameters, (list, tuple)):
        return len(parameters)
    return None


class PublicationExecutor(Protocol):
    """Transaction owner that runs one benchmark publication sample."""

    def execute(
        self,
        request: PublicationExecutionRequest,
    ) -> PublicationExecutionResult: ...


def seed_publication_fixture(db: Session, *, scope: PublicationScope) -> None:
    """Seed the clone rows publication needs without touching real users."""
    import json

    db.execute(
        text(
            'INSERT INTO "user" (id, name, email) '
            "VALUES (:user_id, :name, :email) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "user_id": scope.user_ref,
            "name": "Publication benchmark user",
            "email": f"{scope.user_ref}@benchmark.invalid",
        },
    )
    job_metadata = json.dumps(
        {
            "namespace": scope.namespace_ref,
            "parse_track": "chunk",
            "source_file_name": scope.source_file_name,
            "source_type": "file",
            "document_metadata": {},
            **(
                {"document_id": scope.document_id_ref}
                if scope.document_id_ref
                else {}
            ),
        },
        sort_keys=True,
    )
    db.execute(
        text(
            "INSERT INTO jobs (job_id, user_id, job_type, status, source_type, "
            "webhook_enabled, job_metadata, version, created_at, updated_at, "
            "credits_charged, billing_status) VALUES (:job_id, :user_id, "
            "'document_ingestion', 'running', 'file', false, "
            "CAST(:job_metadata AS JSON), 0, NOW(), NOW(), 0, 'skipped') "
            "ON CONFLICT (job_id) DO UPDATE SET status = 'running', "
            "job_metadata = CAST(:job_metadata AS JSON), updated_at = NOW()"
        ),
        {
            "job_id": scope.job_ref,
            "user_id": scope.user_ref,
            "job_metadata": job_metadata,
        },
    )
    db.execute(
        text(
            "INSERT INTO job_results (id, job_id, delivery_mode, "
            "document_metadata, created_at, updated_at) VALUES (:revision_id, "
            ":job_id, 'inline', CAST('{}' AS JSON), NOW(), NOW()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"revision_id": scope.revision_ref, "job_id": scope.job_ref},
    )


def capture_background_activity(db: Session) -> dict[str, Any]:
    """Record database maintenance activity that could bias a sample."""
    activity = db.execute(
        text(
            "SELECT count(*) FILTER (WHERE query LIKE 'autovacuum%') "
            "AS autovacuum_workers, "
            "count(*) FILTER (WHERE state = 'active') AS active_sessions "
            "FROM pg_stat_activity WHERE datname = current_database()"
        )
    ).mappings()
    counters = db.execute(
        text(
            "SELECT deadlocks, xact_commit FROM pg_stat_database "
            "WHERE datname = current_database()"
        )
    ).mappings()
    activity_row = dict(next(iter(activity), {}))
    counter_row = dict(next(iter(counters), {}))
    return {
        "autovacuum_workers": int(activity_row.get("autovacuum_workers") or 0),
        "active_sessions": int(activity_row.get("active_sessions") or 0),
        "deadlocks": int(counter_row.get("deadlocks") or 0),
        "committed_transactions": int(counter_row.get("xact_commit") or 0),
    }


def capture_benchmark_state(
    db: Session, scope: PublicationScope
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read benchmark state from one consistent database version."""
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    return (
        capture_state_snapshot(
            db, user_id=scope.user_ref, namespace=scope.namespace_ref
        ),
        capture_background_activity(db),
    )


def _cache_invalidation_effects(
    publication_outcome: object,
    *,
    redis_namespace: str,
) -> dict[str, Any]:
    raw_keys: list[str] = []
    invalidation = getattr(publication_outcome, "cache_invalidation", None)
    if invalidation is not None:
        user_id = str(getattr(invalidation, "user_id", ""))
        for namespace in getattr(invalidation, "namespaces", ()) or ():
            raw_keys.append(f"retrieval:version:{user_id}:{namespace}")
    return {
        "applied": False,
        "cache_invalidation_key_refs": [
            opaque_ref(key) for key in sorted(set(raw_keys))
        ],
        "webhook_enqueue_count": 0,
        "redis_namespace_ref": opaque_ref(redis_namespace),
        "notes": (
            "Phase 0 records post-commit effect intents only; no benchmark run "
            "enqueues webhooks or writes a production Redis namespace"
        ),
    }


def _submitted_chunk_counts(
    chunks: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {"submitted_chunks": len(chunks)}
    for chunk in chunks:
        chunk_type = str(chunk.get("type") or chunk.get("chunk_type") or "text")
        key = f"submitted_{chunk_type}_chunks"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _start_publication_trace(
    request: PublicationExecutionRequest,
) -> PublicationTrace | None:
    """Start an optional trace at the benchmark transaction owner boundary."""
    if not request.trace_enabled:
        return None
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace

    if request.publication_attempt_ref is None:
        raise PublicationExecutionError(
            "trace-enabled publication requires a publication attempt reference"
        )
    trace = PublicationTrace.start(
        attempt_ref=request.publication_attempt_ref,
        owner=cast(Literal["sync", "async"], request.owner),
        job_id=request.scope.job_ref,
        job_result_id=request.scope.revision_ref,
        job_type="document_ingestion",
        parse_track="chunk",
    )
    trace.bind_scope(
        user_id=request.scope.user_ref,
        namespace=request.scope.namespace_ref,
    )
    for chunk_type in ("text", "image", "table", "page"):
        trace.record_count(
            f"input_{chunk_type}_chunks",
            sum(
                1
                for chunk in request.chunks
                if str(chunk.get("type") or chunk.get("chunk_type") or "text")
                == chunk_type
            ),
        )
    return trace


def _trace_stage_durations(
    terminal_trace: Mapping[str, object] | None,
) -> dict[str, float]:
    """Project safe stage durations into the benchmark sample record."""
    if terminal_trace is None:
        return {}
    stages = terminal_trace.get("stages")
    if not isinstance(stages, Mapping):
        return {}
    return {
        str(name): float(values["duration_ms"])
        for name, values in stages.items()
        if isinstance(values, Mapping) and "duration_ms" in values
    }


class SyncPublicationExecutor:
    """Worker transaction owner: sync SQLAlchemy/psycopg2 publication."""

    def execute(
        self,
        request: PublicationExecutionRequest,
    ) -> PublicationExecutionResult:
        from shared.services.jobs.lifecycle.publication import (
            SyncJobPublicationFinalizer,
        )
        from shared.services.jobs.lifecycle.publication_trace_sql import (
            attach_publication_trace,
            attach_publication_manual_sql_observer,
            clear_publication_manual_sql_observer,
            clear_publication_trace,
            install_publication_trace_sql_instrumentation,
        )

        scope = request.scope
        engine = create_engine(request.database_url, future=True, poolclass=NullPool)
        sql_observation = BenchmarkSqlObservation(engine)
        if request.trace_enabled:
            install_publication_trace_sql_instrumentation(engine)
        try:
            with Session(engine) as session:
                # Capacity batches validate convergence once after all writers
                # finish. Per-writer semantic snapshots would both race the
                # other writers and add a full-corpus read to every sample.
                if request.mode == "capacity":
                    state_before = {}
                else:
                    state_before, _background_before_snapshot = capture_benchmark_state(
                        session, scope
                    )
            with Session(engine) as session:
                seed_publication_fixture(session, scope=scope)
                session.commit()

            with Session(engine) as session:
                background_before = capture_background_activity(session)

            finalizer = SyncJobPublicationFinalizer()
            failure_reason: str | None = None
            outcome = "failed"
            publication_outcome: object | None = None
            terminal_trace: Mapping[str, object] | None = None
            started_at = perf_counter()
            with Session(engine) as session:
                trace: PublicationTrace | None = None
                trace_connection = None
                try:
                    trace = _start_publication_trace(request)
                    checkout_started_at = perf_counter()
                    trace_connection = session.connection()
                    if trace is not None:
                        trace.record_stage(
                            "pool_checkout_wait",
                            (perf_counter() - checkout_started_at) * 1000.0,
                        )
                    attach_publication_manual_sql_observer(
                        trace_connection, sql_observation
                    )
                    if trace is not None:
                        attach_publication_trace(trace_connection, trace)
                    sql_observation.start()
                    publication_outcome = finalizer.publish_result(
                        session,
                        job_id=scope.job_ref,
                        job_result_id=scope.revision_ref,
                        chunks=[dict(chunk) for chunk in request.chunks],
                        section_summaries=None,
                        trace=trace,
                    )
                    with trace.stage("commit") if trace is not None else nullcontext():
                        session.commit()
                    outcome = "committed"
                except Exception as error:  # noqa: BLE001 - recorded as the sample
                    with (
                        trace.stage("rollback") if trace is not None else nullcontext()
                    ):
                        session.rollback()
                    failure_reason = f"{type(error).__name__}: {error}"
                finally:
                    sql_observation.stop()
                    if trace is not None:
                        terminal_trace = trace.finish(
                            outcome="success" if outcome == "committed" else "rollback"
                        )
                    if trace_connection is not None:
                        clear_publication_manual_sql_observer(trace_connection)
                        clear_publication_trace(trace_connection)
            duration_ms = (perf_counter() - started_at) * 1000.0

            with Session(engine) as session:
                if request.mode == "capacity":
                    state_after = {}
                    background_after = capture_background_activity(session)
                else:
                    state_after, background_after = capture_benchmark_state(
                        session, scope
                    )
        finally:
            engine.dispose()

        if outcome != "committed":
            raise PublicationExecutionError(
                f"sync publication sample failed: {failure_reason}"
            )
        return PublicationExecutionResult(
            outcome=outcome,
            duration_ms=duration_ms,
            state_before=state_before,
            state_after=state_after,
            counts={
                **_submitted_chunk_counts(request.chunks),
                "persisted_document_chunks": int(
                    state_after.get("relation_counts", {}).get("document_chunks", 0)
                ),
                "persisted_map_units": int(
                    state_after.get("relation_counts", {}).get("document_map_units", 0)
                ),
                "persisted_token_rows": int(
                    state_after.get("relation_counts", {})
                    .get("document_map_unit_tokens", 0)
                ),
            },
            effects=_cache_invalidation_effects(
                publication_outcome,
                redis_namespace=request.redis_namespace,
            ),
            stage_durations=_trace_stage_durations(terminal_trace),
            background_activity={
                "before": background_before,
                "after": background_after,
            },
            sql_observation=sql_observation.snapshot(),
            terminal_trace=terminal_trace,
        )


class AsyncPublicationExecutor:
    """API transaction owner: AsyncSession ``run_sync`` publication."""

    def execute(
        self,
        request: PublicationExecutionRequest,
    ) -> PublicationExecutionResult:
        import asyncio

        return asyncio.run(self._execute(request))

    async def _execute(
        self,
        request: PublicationExecutionRequest,
    ) -> PublicationExecutionResult:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from shared.services.retrieval.publication_models import (
            DocumentPublicationScope,
            PublishedDocumentState,
        )
        from shared.services.retrieval.publication_service import (
            RetrievalPublicationService,
        )
        from shared.services.jobs.lifecycle.publication_trace_sql import (
            attach_publication_trace,
            attach_publication_manual_sql_observer,
            clear_publication_manual_sql_observer,
            clear_publication_trace,
            install_publication_trace_sql_instrumentation,
        )

        scope = request.scope
        engine = create_async_engine(request.database_url, poolclass=NullPool)
        sql_observation = BenchmarkSqlObservation(engine)
        if request.trace_enabled:
            install_publication_trace_sql_instrumentation(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        publication_service = RetrievalPublicationService()
        terminal_trace: Mapping[str, object] | None = None
        published_state: PublishedDocumentState | None = None
        try:
            async with session_factory() as session:
                # Capacity batches validate convergence once after all writers
                # finish. Per-writer semantic snapshots would both race the
                # other writers and add a full-corpus read to every sample.
                if request.mode == "capacity":
                    state_before = {}
                else:
                    state_before, _background_before_snapshot = await session.run_sync(
                        lambda sync_session: capture_benchmark_state(
                            sync_session, scope
                        )
                    )
                await session.rollback()
                await session.run_sync(
                    lambda sync_session: seed_publication_fixture(
                        sync_session,
                        scope=scope,
                    )
                )
                await session.commit()

                background_before = await session.run_sync(
                    lambda sync_session: capture_background_activity(sync_session)
                )
                await session.rollback()
                trace: PublicationTrace | None = None
                trace_connection = None
                outcome = "failed"
                started_at = perf_counter()
                try:
                    trace = _start_publication_trace(request)
                    checkout_started_at = perf_counter()
                    trace_connection = await session.connection()
                    if trace is not None:
                        trace.record_stage(
                            "pool_checkout_wait",
                            (perf_counter() - checkout_started_at) * 1000.0,
                        )
                    attach_publication_manual_sql_observer(
                        trace_connection, sql_observation
                    )
                    if trace is not None:
                        attach_publication_trace(trace_connection, trace)
                    sql_observation.start()
                    published_state = await session.run_sync(
                        lambda sync_session: publication_service.publish_document_state(
                            sync_session,
                            job_id=scope.job_ref,
                            job_result_id=scope.revision_ref,
                            chunks=[dict(chunk) for chunk in request.chunks],
                            update_namespace_snapshot=False,
                            trace=trace,
                        )
                    )
                    if (
                        trace is not None
                        and published_state is not None
                        and published_state.document_id is not None
                    ):
                        trace.bind_document_id(published_state.document_id)
                    if published_state is None or published_state.skipped_all_duplicate:
                        # Duplicate-only publication creates no document revision.
                        pass
                    elif published_state.manifest_payload is None:
                        raise PublicationExecutionError(
                            "async publication did not produce a serving manifest"
                        )
                    else:
                        await session.run_sync(
                            lambda sync_session: publication_service.publish_document_graph(
                                sync_session,
                                job_id=scope.job_ref,
                                job_result_id=scope.revision_ref,
                                trace=trace,
                            )
                        )
                        await session.run_sync(
                            lambda sync_session: publication_service.update_namespace_snapshot(
                                sync_session,
                                scope=DocumentPublicationScope(
                                    user_id=scope.user_ref,
                                    namespace=scope.namespace_ref,
                                    document_id=str(published_state.document_id),
                                    job_result_id=scope.revision_ref,
                                    source_file_name=scope.source_file_name,
                                ),
                                manifest_payload=cast(
                                    dict[str, Any], published_state.manifest_payload
                                ),
                                previous_namespace=published_state.previous_namespace,
                                trace=trace,
                            )
                        )
                    with trace.stage("commit") if trace is not None else nullcontext():
                        await session.commit()
                    outcome = "committed"
                    failure_reason = None
                except Exception as error:  # noqa: BLE001 - recorded as the sample
                    with (
                        trace.stage("rollback") if trace is not None else nullcontext()
                    ):
                        await session.rollback()
                    outcome = "failed"
                    failure_reason = f"{type(error).__name__}: {error}"
                finally:
                    sql_observation.stop()
                    if trace is not None:
                        terminal_trace = trace.finish(
                            outcome="success" if outcome == "committed" else "rollback"
                        )
                    if trace_connection is not None:
                        clear_publication_manual_sql_observer(trace_connection)
                        clear_publication_trace(trace_connection)
                duration_ms = (perf_counter() - started_at) * 1000.0

                if request.mode == "capacity":
                    state_after = {}
                    background_after = await session.run_sync(
                        lambda sync_session: capture_background_activity(sync_session)
                    )
                else:
                    state_after, background_after = await session.run_sync(
                        lambda sync_session: capture_benchmark_state(sync_session, scope)
                    )
        finally:
            await engine.dispose()

        if outcome != "committed":
            raise PublicationExecutionError(
                f"async publication sample failed: {failure_reason}"
            )
        return PublicationExecutionResult(
            outcome=outcome,
            duration_ms=duration_ms,
            state_before=state_before,
            state_after=state_after,
            counts={
                **_submitted_chunk_counts(request.chunks),
                "persisted_document_chunks": int(
                    state_after.get("relation_counts", {}).get("document_chunks", 0)
                ),
                "persisted_map_units": int(
                    state_after.get("relation_counts", {}).get("document_map_units", 0)
                ),
                "persisted_token_rows": int(
                    state_after.get("relation_counts", {})
                    .get("document_map_unit_tokens", 0)
                ),
            },
            effects={
                "applied": False,
                "cache_invalidation_key_refs": [],
                "webhook_enqueue_count": 0,
                "redis_namespace_ref": opaque_ref(request.redis_namespace),
                "notes": (
                    "Phase 0 records post-commit effect intents only; the async "
                    "owner stores its cache-version bump in the materialization "
                    "claim finalization transaction"
                ),
            },
            stage_durations=_trace_stage_durations(terminal_trace),
            background_activity={
                "before": background_before,
                "after": background_after,
            },
            sql_observation=sql_observation.snapshot(),
            terminal_trace=terminal_trace,
        )


def executor_for(owner: str) -> PublicationExecutor:
    """Return the publication executor for one transaction owner."""
    if owner == "sync":
        return SyncPublicationExecutor()
    if owner == "async":
        return AsyncPublicationExecutor()
    raise PublicationExecutionError(
        f"unsupported publication owner {owner!r}; expected 'sync' or 'async'"
    )
