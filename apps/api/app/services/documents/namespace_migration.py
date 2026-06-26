"""Move a user's document library rows from one namespace to another."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Select, and_, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from shared.models.database.demo_materialization import DemoMaterialization
from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentSection,
    GraphEdge,
    GraphNode,
    RetrievalHitStat,
    RetrievalRun,
)
from shared.models.database.job import Job
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.redis.redis_sync_service import SyncRedisServiceFactory

NAMESPACE_MODELS: Final[Sequence[type]] = (
    Document,
    DocumentSection,
    DocumentChunk,
    GraphNode,
    GraphEdge,
    RetrievalHitStat,
    RetrievalRun,
    DemoMaterialization,
)
JOB_STATUSES_TO_MIGRATE: Final[tuple[str, ...]] = (
    "waiting-file",
    "pending",
    "running",
    "converting",
    "done",
    "failed",
)
CacheInvalidator = Callable[[str, str, str], None]


@dataclass(frozen=True)
class NamespaceMigrationSummary:
    user_id: str
    source_namespace: str
    target_namespace: str
    dry_run: bool
    row_counts: Mapping[str, int]
    job_count: int
    conflict_counts: Mapping[str, int]


class NamespaceMigrationConflictError(RuntimeError):
    """Raised when namespace migration would violate a target namespace key."""

    def __init__(self, summary: NamespaceMigrationSummary) -> None:
        self.summary = summary
        conflict_summary = ", ".join(
            f"{key}={count}"
            for key, count in sorted(summary.conflict_counts.items())
            if count > 0
        )
        super().__init__(
            "Namespace migration has target conflicts: "
            f"{conflict_summary or 'none'}"
        )


def count_model_rows(
    session: Session,
    *,
    model: type,
    user_id: str,
    source_namespace: str,
) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(model)
            .where(model.user_id == user_id)
            .where(model.namespace == source_namespace)
        ).scalar_one()
    )


def update_model_namespace(
    session: Session,
    *,
    model: type,
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> None:
    session.execute(
        update(model)
        .where(model.user_id == user_id)
        .where(model.namespace == source_namespace)
        .values(namespace=target_namespace)
    )


def count_retrieval_hit_stat_document_conflicts(
    session: Session,
    *,
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> int:
    source_stat = aliased(RetrievalHitStat)
    target_stat = aliased(RetrievalHitStat)
    return int(
        session.execute(
            select(func.count())
            .select_from(source_stat)
            .join(
                target_stat,
                and_(
                    target_stat.user_id == source_stat.user_id,
                    target_stat.namespace == target_namespace,
                    target_stat.hit_kind == source_stat.hit_kind,
                    target_stat.document_id == source_stat.document_id,
                    target_stat.chunk_id.is_(None),
                ),
            )
            .where(source_stat.user_id == user_id)
            .where(source_stat.namespace == source_namespace)
            .where(source_stat.chunk_id.is_(None))
        ).scalar_one()
    )


def count_retrieval_hit_stat_chunk_conflicts(
    session: Session,
    *,
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> int:
    source_stat = aliased(RetrievalHitStat)
    target_stat = aliased(RetrievalHitStat)
    return int(
        session.execute(
            select(func.count())
            .select_from(source_stat)
            .join(
                target_stat,
                and_(
                    target_stat.user_id == source_stat.user_id,
                    target_stat.namespace == target_namespace,
                    target_stat.hit_kind == source_stat.hit_kind,
                    target_stat.document_id == source_stat.document_id,
                    target_stat.chunk_id == source_stat.chunk_id,
                ),
            )
            .where(source_stat.user_id == user_id)
            .where(source_stat.namespace == source_namespace)
            .where(source_stat.chunk_id.is_not(None))
        ).scalar_one()
    )


def count_demo_materialization_conflicts(
    session: Session,
    *,
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> int:
    source_materialization = aliased(DemoMaterialization)
    target_materialization = aliased(DemoMaterialization)
    return int(
        session.execute(
            select(func.count())
            .select_from(source_materialization)
            .join(
                target_materialization,
                and_(
                    target_materialization.user_id == source_materialization.user_id,
                    target_materialization.namespace == target_namespace,
                    target_materialization.demo_source_id
                    == source_materialization.demo_source_id,
                ),
            )
            .where(source_materialization.user_id == user_id)
            .where(source_materialization.namespace == source_namespace)
        ).scalar_one()
    )


def count_namespace_conflicts(
    session: Session,
    *,
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> Mapping[str, int]:
    if source_namespace == target_namespace:
        return {}

    return {
        "retrieval_hit_stats.document_key": (
            count_retrieval_hit_stat_document_conflicts(
                session,
                user_id=user_id,
                source_namespace=source_namespace,
                target_namespace=target_namespace,
            )
        ),
        "retrieval_hit_stats.chunk_key": (
            count_retrieval_hit_stat_chunk_conflicts(
                session,
                user_id=user_id,
                source_namespace=source_namespace,
                target_namespace=target_namespace,
            )
        ),
        "demo_materializations.scope_source": (
            count_demo_materialization_conflicts(
                session,
                user_id=user_id,
                source_namespace=source_namespace,
                target_namespace=target_namespace,
            )
        ),
    }


def has_conflicts(conflict_counts: Mapping[str, int]) -> bool:
    return any(count > 0 for count in conflict_counts.values())


def iter_job_ids_to_migrate(
    session: Session,
    *,
    user_id: str,
    source_namespace: str,
) -> Sequence[str]:
    query: Select[tuple[str]] = (
        select(Job.job_id)
        .where(Job.user_id == user_id)
        .where(Job.job_metadata["namespace"].as_string() == source_namespace)
        .where(Job.status.in_(JOB_STATUSES_TO_MIGRATE))
    )
    return list(session.execute(query).scalars().all())


def update_job_namespace(
    session: Session,
    *,
    job_id: str,
    target_namespace: str,
) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return

    metadata = dict(job.job_metadata or {})
    original_request = metadata.get("original_request")
    metadata["namespace"] = target_namespace
    if isinstance(original_request, dict):
        metadata["original_request"] = {
            **original_request,
            "namespace": target_namespace,
        }
    job.job_metadata = metadata


def invalidate_retrieval_cache(
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> None:
    redis_service = SyncRedisServiceFactory.get_service()
    for namespace in {source_namespace, target_namespace}:
        normalized_namespace = normalize_retrieval_namespace(namespace)
        redis_service.incr(f"retrieval:version:{user_id}:{normalized_namespace}")


def migrate_namespace(
    session: Session,
    *,
    user_id: str,
    source_namespace: str,
    target_namespace: str,
    dry_run: bool,
    cache_invalidator: CacheInvalidator = invalidate_retrieval_cache,
) -> NamespaceMigrationSummary:
    row_counts = {
        model.__tablename__: count_model_rows(
            session,
            model=model,
            user_id=user_id,
            source_namespace=source_namespace,
        )
        for model in NAMESPACE_MODELS
    }
    job_ids = iter_job_ids_to_migrate(
        session,
        user_id=user_id,
        source_namespace=source_namespace,
    )
    conflict_counts = count_namespace_conflicts(
        session,
        user_id=user_id,
        source_namespace=source_namespace,
        target_namespace=target_namespace,
    )
    summary = NamespaceMigrationSummary(
        user_id=user_id,
        source_namespace=source_namespace,
        target_namespace=target_namespace,
        dry_run=dry_run,
        row_counts=row_counts,
        job_count=len(job_ids),
        conflict_counts=conflict_counts,
    )

    if not dry_run:
        if has_conflicts(conflict_counts):
            raise NamespaceMigrationConflictError(summary)
        for model in NAMESPACE_MODELS:
            update_model_namespace(
                session,
                model=model,
                user_id=user_id,
                source_namespace=source_namespace,
                target_namespace=target_namespace,
            )
        for job_id in job_ids:
            update_job_namespace(
                session,
                job_id=job_id,
                target_namespace=target_namespace,
            )
        cache_invalidator(user_id, source_namespace, target_namespace)

    return summary
