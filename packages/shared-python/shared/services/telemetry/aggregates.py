"""DB-backed self-hosted aggregate telemetry snapshots."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, cast

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .api_metrics import ApiRequestTelemetryMetrics, ApiRequestMetricsSnapshot
from .client import TelemetryClient
from .config import TelemetryRuntimeConfig, TelemetrySettings
from .events import TelemetryProperties, build_base_event_properties

AGGREGATE_WINDOW_SECONDS = 24 * 60 * 60


class DatabaseSessionFactory(Protocol):
    """Factory for app-owned async database sessions."""

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Return an async context manager yielding an AsyncSession."""
        raise NotImplementedError


class TelemetryAggregateSettings(TelemetrySettings, Protocol):
    TELEMETRY_AGGREGATE_INTERVAL_SECONDS: int


class SelfHostedAggregateTelemetryRunner:
    """Periodically emits anonymous, aggregate-only self-hosted telemetry."""

    def __init__(
        self,
        *,
        config: TelemetryRuntimeConfig,
        telemetry_client: TelemetryClient,
        db_session_factory: DatabaseSessionFactory,
        api_metrics: ApiRequestTelemetryMetrics,
        interval_seconds: int,
    ) -> None:
        self._config = config
        self._telemetry_client = telemetry_client
        self._db_session_factory = db_session_factory
        self._api_metrics = api_metrics
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def emit_once(self) -> None:
        """Collect and emit all aggregate snapshots once."""
        event_properties = await collect_self_hosted_aggregate_event_properties(
            config=self._config,
            db_session_factory=self._db_session_factory,
            api_metrics=self._api_metrics,
            window_seconds=AGGREGATE_WINDOW_SECONDS,
        )
        for event_name, properties in event_properties.items():
            self._telemetry_client.capture(event_name, properties)

    def start(self) -> None:
        """Start the periodic aggregate loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="self-hosted-aggregate-telemetry",
        )

    async def stop(self) -> None:
        """Stop the periodic aggregate loop."""
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.emit_once()
            except Exception as exc:
                logger.warning(f"anonymous aggregate telemetry failed: {exc}")


async def start_self_hosted_aggregate_telemetry(
    settings: TelemetryAggregateSettings,
    *,
    telemetry_client: TelemetryClient | None,
    config: TelemetryRuntimeConfig | None,
    db_session_factory: DatabaseSessionFactory,
    api_metrics: ApiRequestTelemetryMetrics,
) -> SelfHostedAggregateTelemetryRunner | None:
    """Start aggregate telemetry when the base self-hosted client is active."""
    if telemetry_client is None or config is None:
        return None
    interval_seconds = max(settings.TELEMETRY_AGGREGATE_INTERVAL_SECONDS, 60)
    runner = SelfHostedAggregateTelemetryRunner(
        config=config,
        telemetry_client=telemetry_client,
        db_session_factory=db_session_factory,
        api_metrics=api_metrics,
        interval_seconds=interval_seconds,
    )
    try:
        await runner.emit_once()
    except Exception as exc:
        logger.warning(f"anonymous aggregate telemetry failed: {exc}")
    runner.start()
    logger.info("anonymous self-hosted aggregate telemetry started")
    return runner


async def stop_self_hosted_aggregate_telemetry(
    runner: SelfHostedAggregateTelemetryRunner | None,
) -> None:
    """Stop aggregate telemetry if it was started."""
    if runner is None:
        return
    await runner.stop()


async def collect_self_hosted_aggregate_event_properties(
    *,
    config: TelemetryRuntimeConfig,
    db_session_factory: DatabaseSessionFactory,
    api_metrics: ApiRequestTelemetryMetrics,
    window_seconds: int = AGGREGATE_WINDOW_SECONDS,
) -> dict[str, TelemetryProperties]:
    """Collect aggregate event properties without including customer content."""
    async with db_session_factory() as session:
        return {
            "self_hosted_usage_aggregate": await _collect_usage_aggregate(
                session,
                config,
                window_seconds,
            ),
            "self_hosted_retrieval_aggregate": await _collect_retrieval_aggregate(
                session,
                config,
                window_seconds,
            ),
            "self_hosted_worker_aggregate": await _collect_worker_aggregate(
                session,
                config,
                window_seconds,
            ),
            "self_hosted_api_aggregate": _collect_api_aggregate(
                config,
                api_metrics.snapshot_and_reset(),
                window_seconds,
            ),
            "self_hosted_provider_aggregate": await _collect_provider_aggregate(
                session,
                config,
                window_seconds,
            ),
        }


async def _collect_usage_aggregate(
    session: AsyncSession,
    config: TelemetryRuntimeConfig,
    window_seconds: int,
) -> TelemetryProperties:
    properties = _base_aggregate_properties(config, window_seconds)
    properties.update(
        {
            "total_users": await _int_scalar(session, 'SELECT COUNT(*) FROM "user"'),
            "active_api_keys": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM api_keys WHERE is_active = true",
            ),
            "total_jobs": await _int_scalar(session, "SELECT COUNT(*) FROM jobs"),
            "jobs_created_24h": await _int_scalar(
                session,
                _windowed_count_sql("jobs", "created_at"),
                window_seconds,
            ),
            "active_jobs": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM jobs WHERE status IN ('waiting-file', 'pending', 'running', 'converting')",
            ),
            "completed_jobs_24h": await _int_scalar(
                session,
                _windowed_count_sql("jobs", "updated_at", "status = 'done'"),
                window_seconds,
            ),
            "failed_jobs_24h": await _int_scalar(
                session,
                _windowed_count_sql("jobs", "updated_at", "status = 'failed'"),
                window_seconds,
            ),
            "total_documents": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM documents",
            ),
            "active_documents": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM documents WHERE status = 'active'",
            ),
            "total_document_chunks": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM document_chunks",
            ),
            "total_job_chunks": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM job_chunks",
            ),
            "pages_processed_24h": await _int_scalar(
                session,
                f"""
                SELECT COALESCE(SUM(page_count), 0)
                FROM jobs
                WHERE updated_at >= {_window_start_expression()}
                  AND status = 'done'
                """,
                window_seconds,
            ),
            "credits_charged_24h": await _int_scalar(
                session,
                f"""
                SELECT COALESCE(SUM(credits_charged), 0)
                FROM jobs
                WHERE updated_at >= {_window_start_expression()}
                """,
                window_seconds,
            ),
        }
    )
    return properties


async def _collect_retrieval_aggregate(
    session: AsyncSession,
    config: TelemetryRuntimeConfig,
    window_seconds: int,
) -> TelemetryProperties:
    properties = _base_aggregate_properties(config, window_seconds)
    properties.update(
        {
            "retrieval_runs_24h": await _int_scalar(
                session,
                _windowed_count_sql("retrieval_runs", "created_at"),
                window_seconds,
            ),
            "retrieval_errors_24h": await _int_scalar(
                session,
                _windowed_count_sql(
                    "retrieval_runs",
                    "created_at",
                    "error IS NOT NULL AND error <> ''",
                ),
                window_seconds,
            ),
            "retrieval_cache_hits_24h": await _int_scalar(
                session,
                _windowed_count_sql(
                    "retrieval_runs",
                    "created_at",
                    "cache_hit = true",
                ),
                window_seconds,
            ),
            "retrieval_result_count_24h": await _int_scalar(
                session,
                f"""
                SELECT COALESCE(SUM(result_count), 0)
                FROM retrieval_runs
                WHERE created_at >= {_window_start_expression()}
                """,
                window_seconds,
            ),
            "retrieval_latency_avg_ms_24h": await _float_scalar(
                session,
                f"""
                SELECT COALESCE(AVG(latency_ms), 0)
                FROM retrieval_runs
                WHERE created_at >= {_window_start_expression()}
                """,
                window_seconds,
            ),
            "retrieval_latency_p95_ms_24h": await _float_scalar(
                session,
                f"""
                SELECT COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)
                FROM retrieval_runs
                WHERE created_at >= {_window_start_expression()}
                """,
                window_seconds,
            ),
            "retrieval_tokens_24h": await _int_scalar(
                session,
                f"""
                SELECT COALESCE(SUM(token_count), 0)
                FROM retrieval_runs
                WHERE created_at >= {_window_start_expression()}
                """,
                window_seconds,
            ),
            "retrieval_steps_24h": await _int_scalar(
                session,
                _windowed_count_sql("retrieval_steps", "created_at"),
                window_seconds,
            ),
            "retrieval_step_errors_24h": await _int_scalar(
                session,
                _windowed_count_sql(
                    "retrieval_steps",
                    "created_at",
                    "error IS NOT NULL AND error <> ''",
                ),
                window_seconds,
            ),
        }
    )
    return properties


async def _collect_worker_aggregate(
    session: AsyncSession,
    config: TelemetryRuntimeConfig,
    window_seconds: int,
) -> TelemetryProperties:
    properties = _base_aggregate_properties(config, window_seconds)
    properties.update(
        {
            "jobs_waiting_file": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM jobs WHERE status = 'waiting-file'",
            ),
            "jobs_pending": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM jobs WHERE status = 'pending'",
            ),
            "jobs_running": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM jobs WHERE status = 'running'",
            ),
            "jobs_converting": await _int_scalar(
                session,
                "SELECT COUNT(*) FROM jobs WHERE status = 'converting'",
            ),
            "jobs_done_24h": await _int_scalar(
                session,
                _windowed_count_sql("jobs", "updated_at", "status = 'done'"),
                window_seconds,
            ),
            "jobs_failed_24h": await _int_scalar(
                session,
                _windowed_count_sql("jobs", "updated_at", "status = 'failed'"),
                window_seconds,
            ),
            "job_duration_avg_seconds_24h": await _float_scalar(
                session,
                f"""
                SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (updated_at - created_at))), 0)
                FROM jobs
                WHERE updated_at >= {_window_start_expression()}
                  AND status IN ('done', 'failed')
                """,
                window_seconds,
            ),
        }
    )
    return properties


def _collect_api_aggregate(
    config: TelemetryRuntimeConfig,
    snapshot: ApiRequestMetricsSnapshot,
    window_seconds: int,
) -> TelemetryProperties:
    properties = _base_aggregate_properties(config, window_seconds)
    properties.update(
        {
            "api_requests_total": snapshot.request_count,
            "api_requests_2xx": snapshot.status_2xx_count,
            "api_requests_3xx": snapshot.status_3xx_count,
            "api_requests_4xx": snapshot.status_4xx_count,
            "api_requests_5xx": snapshot.status_5xx_count,
            "api_latency_avg_ms": snapshot.latency_avg_ms,
            "api_latency_p95_ms": snapshot.latency_p95_ms,
        }
    )
    return properties


async def _collect_provider_aggregate(
    session: AsyncSession,
    config: TelemetryRuntimeConfig,
    window_seconds: int,
) -> TelemetryProperties:
    properties = _base_aggregate_properties(config, window_seconds)
    parse_tokens = await _int_scalar(
        session,
        f"""
        SELECT COALESCE(SUM(total_tokens), 0)
        FROM parse_runs
        WHERE started_at >= {_window_start_expression()}
        """,
        window_seconds,
    )
    retrieval_tokens = await _int_scalar(
        session,
        f"""
        SELECT COALESCE(SUM(token_count), 0)
        FROM retrieval_steps
        WHERE created_at >= {_window_start_expression()}
        """,
        window_seconds,
    )
    properties.update(
        {
            "parse_agent_runs_24h": await _int_scalar(
                session,
                _windowed_count_sql("parse_runs", "started_at"),
                window_seconds,
            ),
            "parse_agent_errors_24h": await _int_scalar(
                session,
                _windowed_count_sql(
                    "parse_runs",
                    "started_at",
                    "final_status <> 'ready'",
                ),
                window_seconds,
            ),
            "parse_agent_tokens_24h": parse_tokens,
            "parse_agent_latency_avg_ms_24h": await _float_scalar(
                session,
                f"""
                SELECT COALESCE(AVG(total_latency_ms), 0)
                FROM parse_runs
                WHERE started_at >= {_window_start_expression()}
                """,
                window_seconds,
            ),
            "retrieval_provider_tokens_24h": retrieval_tokens,
            "retrieval_model_count_24h": await _int_scalar(
                session,
                f"""
                SELECT COUNT(DISTINCT model_name)
                FROM retrieval_steps
                WHERE created_at >= {_window_start_expression()}
                  AND model_name IS NOT NULL
                  AND model_name <> ''
                """,
                window_seconds,
            ),
            "provider_tokens_24h": parse_tokens + retrieval_tokens,
            "provider_error_count_24h": await _int_scalar(
                session,
                f"""
                SELECT
                  (SELECT COUNT(*) FROM parse_runs
                   WHERE started_at >= {_window_start_expression()}
                     AND final_status <> 'ready')
                  +
                  (SELECT COUNT(*) FROM retrieval_steps
                   WHERE created_at >= {_window_start_expression()}
                     AND error IS NOT NULL
                     AND error <> '')
                """,
                window_seconds,
            ),
            "webhook_deliveries_24h": await _int_scalar(
                session,
                _windowed_count_sql("webhook_logs", "created_at"),
                window_seconds,
            ),
            "webhook_delivery_failures_24h": await _int_scalar(
                session,
                _windowed_count_sql(
                    "webhook_logs",
                    "created_at",
                    "(response_status_code IS NULL OR response_status_code >= 400)",
                ),
                window_seconds,
            ),
        }
    )
    return properties


def _base_aggregate_properties(
    config: TelemetryRuntimeConfig,
    window_seconds: int,
) -> TelemetryProperties:
    return {
        **build_base_event_properties(config),
        "window_seconds": window_seconds,
    }


async def _int_scalar(
    session: AsyncSession,
    statement: str,
    window_seconds: int | None = None,
) -> int:
    value = await _scalar(session, statement, window_seconds)
    if value is None:
        return 0
    return int(cast(Any, value))


async def _float_scalar(
    session: AsyncSession,
    statement: str,
    window_seconds: int | None = None,
) -> float:
    value = await _scalar(session, statement, window_seconds)
    if value is None:
        return 0.0
    return float(cast(Any, value))


async def _scalar(
    session: AsyncSession,
    statement: str,
    window_seconds: int | None = None,
) -> object:
    params = {} if window_seconds is None else {"window_seconds": window_seconds}
    result = await session.execute(text(statement), params)
    return result.scalar_one_or_none()


def _windowed_count_sql(
    table_name: str,
    timestamp_column: str,
    predicate: str | None = None,
) -> str:
    clauses = [f"{timestamp_column} >= {_window_start_expression()}"]
    if predicate:
        clauses.append(predicate)
    return f"SELECT COUNT(*) FROM {table_name} WHERE {' AND '.join(clauses)}"


def _window_start_expression() -> str:
    return "timezone('utc', now()) - (:window_seconds * INTERVAL '1 second')"
