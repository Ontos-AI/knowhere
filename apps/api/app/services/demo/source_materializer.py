"""Demo Source Materialization workflow."""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import blake2b
from pathlib import Path
from uuid import uuid4

import logfire

from app.services.demo.source_catalog import DemoSourceCatalog, DemoSourceDefinition
from sqlalchemy.exc import IntegrityError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from shared.core.exceptions.domain_exceptions import ConflictException, ValidationException
from shared.models.database.demo_materialization import DemoMaterialization
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
from shared.services.jobs.lifecycle.publication_trace_sql import (
    attach_publication_trace,
    clear_publication_trace,
    install_publication_trace_sql_instrumentation,
)
from shared.services.retrieval.cache_service import invalidate_retrieval_cache_namespaces
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.redis import RedisPublicationSemaphore, RedisServiceFactory
from shared.core.config import settings
from shared.services.storage.result_storage import get_result_storage


@dataclass(frozen=True)
class MaterializedDemoSource:
    """User-owned copy of one canonical demo source."""

    demo_source_id: str
    document_id: str
    status: str
    title: str
    mime_type: str
    size_bytes: int
    chunk_count: int


class DemoSourceMaterializer:
    """Copies canonical Demo Sources into user-owned retrieval state."""

    def __init__(
        self,
        *,
        catalog: DemoSourceCatalog,
        publication_service: RetrievalPublicationService | None = None,
    ) -> None:
        self._catalog = catalog
        self._publication_service = publication_service or RetrievalPublicationService()
        self._redis_service = RedisServiceFactory.get_service()

    async def materialize_sources(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        demo_source_ids: list[str],
    ) -> list[MaterializedDemoSource]:
        selected_demo_source_ids = _deduplicate_source_ids(demo_source_ids)
        if not selected_demo_source_ids:
            raise ValidationException(
                user_message="At least one demo source must be selected.",
                violations=[
                    {
                        "field": "demo_source_ids",
                        "description": "Select one or more demo source IDs.",
                    }
                ],
            )

        selected_sources = [
            self._catalog.require_source(demo_source_id)
            for demo_source_id in selected_demo_source_ids
        ]
        try:
            claims = await self._claim_sources(
                db,
                user_id=user_id,
                namespace=namespace,
                sources=selected_sources,
            )
        except IntegrityError as error:
            await db.rollback()
            raise ConflictException(
                user_message="This demo source is currently being materialized.",
                reason="ABORTED",
                resource="Demo materialization",
            ) from error
        await db.commit()
        claim_ids = tuple(claim.id for claim in claims.values())
        results: list[MaterializedDemoSource] = []
        for source in selected_sources:
            try:
                semaphore = RedisPublicationSemaphore(
                    self._redis_service,
                    concurrency=settings.MATERIALIZATION_DB_PUBLICATION_CONCURRENCY,
                    lease_seconds=settings.MATERIALIZATION_DB_PUBLICATION_LEASE_SECONDS,
                    acquire_timeout_seconds=settings.MATERIALIZATION_DB_PUBLICATION_ACQUIRE_TIMEOUT_SECONDS,
                )
                result = await self._materialize_source(
                    db,
                    user_id=user_id,
                    namespace=namespace,
                    source=source,
                    claim=claims[source.demo_source_id],
                    semaphore=semaphore,
                )
                results.append(result)
            except Exception:
                await db.rollback()
                await self._release_claims(
                    db,
                    claim_ids=claim_ids,
                )
                raise
        await invalidate_retrieval_cache_namespaces(
            user_id=user_id,
            namespaces=[namespace],
        )
        return results

    async def _materialize_source(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        source: DemoSourceDefinition,
        claim: DemoMaterialization,
        semaphore: RedisPublicationSemaphore,
    ) -> MaterializedDemoSource:
        document_id = f"doc_{uuid4().hex[:12]}"
        job_id = f"job_demo_{uuid4().hex[:12]}"
        job_result_id = str(uuid4())
        timestamp = _utc_now()
        if settings.KNOWHERE_PUBLICATION_TRACE_ENABLED:
            bind = db.bind
            if bind is None:
                raise RuntimeError("Demo publication database session has no bind")
            install_publication_trace_sql_instrumentation(
                bind.engine if isinstance(bind, AsyncConnection) else bind
            )
        stage_started_at = time.perf_counter()
        result_bundle = _upload_demo_result_bundle(
            job_id=job_id,
            source_directory=self._catalog.source_directory(source),
        )
        logfire.info(
            "Demo materialization source bundle upload completed",
            demo_source_id=source.demo_source_id,
            duration_seconds=time.perf_counter() - stage_started_at,
        )

        db.add(
            Job(
                job_id=job_id,
                user_id=user_id,
                job_type="demo_materialization",
                status="done",
                source_type="demo",
                webhook_enabled=False,
                job_metadata={
                    "document_id": document_id,
                    "namespace": namespace,
                    "source_type": "demo",
                    "source_file_name": source.title,
                    "demo_source_id": source.demo_source_id,
                },
                version=0,
                created_at=timestamp,
                updated_at=timestamp,
                credits_charged=0,
                billing_status="skipped",
            )
        )
        db.add(
            JobResult(
                id=job_result_id,
                job_id=job_id,
                delivery_mode="inline",
                document_metadata={
                    "source_file_name": source.title,
                    "demo_source_id": source.demo_source_id,
                },
                inline_payload={"source": "canonical_demo"},
                result_s3_key=result_bundle["zip_key"],
                result_size=result_bundle["zip_size"],
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        chunks = self._catalog.publication_chunks(source)
        stage_started_at = time.perf_counter()
        wait_seconds = await semaphore.acquire()
        logfire.info(
            "Demo materialization publication semaphore acquired",
            demo_source_id=source.demo_source_id,
            wait_seconds=wait_seconds,
        )
        trace: PublicationTrace | None = (
            PublicationTrace.start(
                attempt_ref=f"attempt_{uuid4().hex}",
                owner="async",
                job_id=job_id,
                job_result_id=job_result_id,
                document_id=document_id,
                scope_fingerprint=_publication_scope_fingerprint(
                    user_id=user_id,
                    namespace=namespace,
                ),
                job_type="demo_materialization",
                parse_track="chunk",
            )
            if settings.KNOWHERE_PUBLICATION_TRACE_ENABLED
            else None
        )
        trace_connection: AsyncConnection | None = None
        try:
            if trace is not None:
                for chunk_type in ("text", "image", "table", "page"):
                    trace.record_count(
                        f"input_{chunk_type}_chunks",
                        sum(
                            1
                            for chunk in chunks
                            if str(chunk.get("type") or chunk.get("chunk_type") or "text")
                            == chunk_type
                        ),
                    )
                checkout_started_at = time.perf_counter()
                trace_connection = await db.connection()
                trace.record_stage(
                    "pool_checkout_wait",
                    (time.perf_counter() - checkout_started_at) * 1000,
                )
                attach_publication_trace(trace_connection, trace)

            base_rows_started_at = time.perf_counter()
            with trace.stage("result_binding") if trace is not None else nullcontext():
                await db.flush()
            logfire.info(
                "Demo materialization base rows completed",
                demo_source_id=source.demo_source_id,
                duration_seconds=time.perf_counter() - base_rows_started_at,
            )
            published_state = await db.run_sync(
                lambda sync_db: self._publication_service.publish_document_state(
                    sync_db,
                    job_id=job_id,
                    job_result_id=job_result_id,
                    chunks=[dict(chunk) for chunk in chunks],
                    update_namespace_snapshot=False,
                    trace=trace,
                )
            )
            await db.run_sync(
                lambda sync_db: self._publication_service.publish_document_graph(
                    sync_db,
                    job_id=job_id,
                    job_result_id=job_result_id,
                    trace=trace,
                )
            )
            await db.flush()
            logfire.info(
                "Demo materialization sections chunks and map index completed",
                demo_source_id=source.demo_source_id,
                duration_seconds=time.perf_counter() - stage_started_at,
                chunk_count=len(chunks),
            )

            if published_state is None or published_state.document_id != document_id:
                raise RuntimeError("Demo publication did not create its requested document")
            if published_state.manifest_payload is None:
                raise RuntimeError("Demo publication did not create a serving manifest")
            manifest_payload = published_state.manifest_payload
            stage_started_at = time.perf_counter()
            await db.run_sync(
                lambda sync_db: self._publication_service.update_namespace_snapshot(
                    sync_db,
                    scope=DocumentPublicationScope(
                        user_id=user_id,
                        namespace=namespace,
                        document_id=document_id,
                        job_result_id=job_result_id,
                        source_file_name=source.title,
                    ),
                    manifest_payload=manifest_payload,
                    previous_namespace=published_state.previous_namespace,
                    trace=trace,
                )
            )
            logfire.info(
                "Demo materialization namespace snapshot completed",
                demo_source_id=source.demo_source_id,
                duration_seconds=time.perf_counter() - stage_started_at,
            )
            claim.document_id = document_id
            claim.status = "ready"
            claim.claimed_at = None
            claim.updated_at = _utc_now()
            commit_started_at = time.perf_counter()
            with trace.stage("commit") if trace is not None else nullcontext():
                await db.commit()
            logfire.info(
                "Demo materialization publication commit completed",
                demo_source_id=source.demo_source_id,
                duration_seconds=time.perf_counter() - commit_started_at,
            )
        except Exception:
            if trace is not None:
                try:
                    with trace.stage("rollback"):
                        await db.rollback()
                finally:
                    trace.finish(outcome="rollback")
            raise
        else:
            if trace is not None:
                trace.finish(outcome="success")
        finally:
            if trace_connection is not None:
                clear_publication_trace(trace_connection)
            await semaphore.release()

        return _materialized_source_payload(
            source=source,
            document_id=document_id,
            status="created",
        )

    async def _claim_sources(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        sources: list[DemoSourceDefinition],
    ) -> dict[str, DemoMaterialization]:
        now = _utc_now()
        claims: dict[str, DemoMaterialization] = {}
        for source in sorted(sources, key=lambda item: item.demo_source_id):
            lock_acquired = await db.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        _materialization_lock_id(
                            user_id=user_id,
                            namespace=namespace,
                            demo_source_id=source.demo_source_id,
                        )
                    )
                )
            )
            if not lock_acquired:
                raise ConflictException(
                    user_message="This demo source is currently being materialized.",
                    reason="ABORTED",
                    resource="Demo materialization",
                    resource_id=source.demo_source_id,
                )
            result = await db.execute(
                select(DemoMaterialization)
                .where(DemoMaterialization.user_id == user_id)
                .where(DemoMaterialization.namespace == namespace)
                .where(DemoMaterialization.demo_source_id == source.demo_source_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                if not _is_stale_claim(existing, now=now):
                    raise _materialization_conflict(existing, source)
                claim = existing
                claim.status = "materializing"
                claim.document_id = None
                claim.claimed_at = now
                claim.updated_at = now
            else:
                claim = DemoMaterialization(
                    user_id=user_id,
                    namespace=namespace,
                    demo_source_id=source.demo_source_id,
                    status="materializing",
                    document_id=None,
                    claimed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(claim)
            claims[source.demo_source_id] = claim
            await db.flush()
        return claims

    async def _release_claims(
        self,
        db: AsyncSession,
        *,
        claim_ids: Iterable[str],
    ) -> None:
        claim_ids = tuple(claim_ids)
        if not claim_ids:
            return
        await db.execute(
            delete(DemoMaterialization).where(
                DemoMaterialization.status == "materializing",
                DemoMaterialization.id.in_(claim_ids),
            )
        )
        await db.commit()


def _deduplicate_source_ids(demo_source_ids: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for demo_source_id in demo_source_ids:
        normalized = str(demo_source_id).strip()
        if not normalized or normalized in seen:
            continue
        selected.append(normalized)
        seen.add(normalized)
    return selected


def _is_stale_claim(materialization: DemoMaterialization, *, now: datetime) -> bool:
    return (
        materialization.status == "materializing"
        and materialization.claimed_at is not None
        and materialization.claimed_at < now - timedelta(minutes=10)
    )


def _materialization_lock_id(
    *,
    user_id: str,
    namespace: str,
    demo_source_id: str,
) -> int:
    lock_key = f"{user_id}\0{namespace}\0{demo_source_id}"
    digest = blake2b(lock_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _publication_scope_fingerprint(*, user_id: str, namespace: str) -> str:
    scope = f"{user_id}\0{namespace}".encode("utf-8")
    return f"scope-{blake2b(scope, digest_size=16).hexdigest()}"


def _materialization_conflict(
    materialization: DemoMaterialization,
    source: DemoSourceDefinition,
) -> ConflictException:
    reason = "ALREADY_EXISTS" if materialization.status == "ready" else "ABORTED"
    return ConflictException(
        user_message=(
            "This demo source has already been materialized."
            if reason == "ALREADY_EXISTS"
            else "This demo source is currently being materialized."
        ),
        reason=reason,
        resource="Demo materialization",
        resource_id=source.demo_source_id,
    )


def _materialized_source_payload(
    *,
    source: DemoSourceDefinition,
    document_id: str,
    status: str,
) -> MaterializedDemoSource:
    return MaterializedDemoSource(
        demo_source_id=source.demo_source_id,
        document_id=document_id,
        status=status,
        title=source.title,
        mime_type=source.mime_type,
        size_bytes=source.size_bytes,
        chunk_count=source.chunk_count,
    )


def _upload_demo_result_bundle(
    *,
    job_id: str,
    source_directory: Path,
) -> dict[str, int | str]:
    storage = get_result_storage()
    with tempfile.TemporaryDirectory(prefix="knowhere-demo-result-") as temp_directory:
        zip_base_path = Path(temp_directory) / job_id
        zip_file_path = Path(
            shutil.make_archive(
                str(zip_base_path),
                "zip",
                root_dir=source_directory,
            )
        )
        zip_size = zip_file_path.stat().st_size
        bundle = storage.upload(
            job_id=job_id,
            result_dir=str(source_directory),
            zip_file_path=str(zip_file_path),
        )

    return {
        "zip_key": bundle.zip_key,
        "zip_size": zip_size,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
