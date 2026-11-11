"""Publish one refined progressive scope and finalize the revision once all are refined.

Shared by the page-memory page-range track and the spreadsheet sheet-group track.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar, cast
from uuid import uuid4

import pandas as pd
from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, noload

from app.services.connect_builder.summary_builder import (
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
)
from app.services.document_ingestion.artifact_refs import (
    collect_referenced_artifact_refs,
)
from app.services.document_ingestion.processing_context import (
    merge_persisted_job_token_usage,
)
from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentScopeEnrichment,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.progressive_ingestion import (
    EnhancementStage,
    StoredSnapshot,
)
from shared.services.ai.token_tracking import get_current_token_tracker
from shared.services.jobs.lifecycle.scope_enrichments import claim_scope_enrichment
from shared.services.jobs.progressive_ingestion import (
    read_progressive_state,
    write_progressive_state,
)
from shared.services.jobs.progressive_stages import (
    claim_progressive_stage,
    finish_progressive_stage,
    required_stages_before_finalization,
)
from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.storage.result_storage import ResultStorage
from shared.services.storage.zip_result_service import ZipResultService
from shared.utils.utc_now import utc_now_naive


TargetT = TypeVar("TargetT")


class RevisionTarget(Protocol):
    """Published progressive revision a scope task refines."""

    @property
    def document_id(self) -> str: ...

    @property
    def job_id(self) -> str: ...

    @property
    def user_id(self) -> str: ...

    @property
    def namespace(self) -> str: ...

    @property
    def source_file_name(self) -> str: ...

    @property
    def raw_prefix(self) -> str | None: ...

    @property
    def job_metadata(self) -> dict[str, Any]: ...

    @property
    def claim_generation(self) -> str | None: ...

    @property
    def serving_raw_prefix(self) -> str | None: ...


@dataclass(frozen=True)
class ClaimedScopeRevision:
    """Rows a scope claim validated, readable only inside the claim session."""

    document: Document
    job: Job
    job_metadata: dict[str, Any]
    source_file_name: str
    claim_generation: str
    scope: DocumentScopeEnrichment


def claim_scope_revision(
    *,
    job_result_id: str,
    scope_id: str,
    build_target: Callable[[ClaimedScopeRevision], TargetT],
) -> tuple[str, TargetT | None]:
    """Claim one scope and build its task target while the claimed rows are loaded.

    Returns ``skipped`` when the scope is not claimable, and ``aborted`` (scope
    failed) when the revision is no longer the document's active current one.
    """
    now = utc_now_naive()
    claim_generation = str(uuid4())
    with get_sync_db_context() as db:
        claimed_doc_id = claim_scope_enrichment(
            db,
            job_result_id=job_result_id,
            scope_id=scope_id,
            claim_generation=claim_generation,
            now=now,
        )
        if claimed_doc_id is None:
            return "skipped", None

        document = db.execute(
            select(Document).where(Document.document_id == str(claimed_doc_id))
        ).scalar_one_or_none()
        job_result = db.execute(
            select(JobResult).where(JobResult.id == job_result_id)
        ).scalar_one_or_none()
        job = (
            db.execute(
                select(Job).where(Job.job_id == job_result.job_id)
            ).scalar_one_or_none()
            if job_result is not None
            else None
        )
        job_metadata = dict(job.job_metadata or {}) if job is not None else {}
        source_file_name = (
            (
                document.source_file_name
                or JobMetadataHelper.get_source_file_name(job_metadata)
            )
            if document is not None
            else None
        )
        if (
            document is None
            or document.status != "active"
            or document.current_job_result_id != job_result_id
            or job_result is None
            or job is None
            or not source_file_name
        ):
            db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == job_result_id)
                .where(DocumentScopeEnrichment.scope_id == scope_id)
                .values(state="failed", updated_at=now)
            )
            return "aborted", None

        scope = db.execute(
            select(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == job_result_id)
            .where(DocumentScopeEnrichment.scope_id == scope_id)
        ).scalar_one()
        return "claimed", build_target(
            ClaimedScopeRevision(
                document=document,
                job=job,
                job_metadata=job_metadata,
                source_file_name=source_file_name,
                claim_generation=claim_generation,
                scope=scope,
            )
        )


def flush_scope_token_usage(job_id: str) -> None:
    tracker = get_current_token_tracker()
    if tracker is None:
        return
    try:
        merge_persisted_job_token_usage(job_id=job_id, delta=dict(tracker))
    except Exception as exc:
        logger.warning(
            "[progressive] failed to merge enrichment token usage job_id={}: {}",
            job_id,
            exc,
        )


@dataclass(frozen=True)
class PackageRefs:
    """Track-owned artifacts of the final package beyond those chunks reference.

    ``baseline_refs`` still live under the serving (baseline snapshot) prefix.
    """

    extra_refs: frozenset[str]
    baseline_refs: frozenset[str]


REFINED_ENRICHMENT_STATE = "refined"


ScopePublisher = Callable[..., Any]


@dataclass(frozen=True)
class ScopeRefinementOutput:
    scope_id: str
    dataframe: pd.DataFrame
    chunks: list[dict[str, Any]]
    section_summaries: dict[str, str]
    artifact_refs: set[str]


def build_publication_scope(
    *,
    target: RevisionTarget,
    job_result_id: str,
) -> DocumentPublicationScope:
    return DocumentPublicationScope(
        user_id=target.user_id,
        namespace=target.namespace,
        document_id=target.document_id,
        job_result_id=job_result_id,
        source_file_name=target.source_file_name,
    )


def upload_scope_artifacts(
    *,
    storage: ResultStorage,
    job_id: str,
    raw_prefix: str | None,
    workspace_dir: str,
    artifact_refs: set[str],
    artifact_mapping: dict[str, str] | None = None,
    add_dir: str | None = None,
) -> None:
    if not artifact_refs:
        return
    if add_dir is None:
        add_dirs = [
            os.path.join(workspace_dir, entry)
            for entry in os.listdir(workspace_dir)
            if os.path.isdir(os.path.join(workspace_dir, entry))
        ]
        add_dir = next(
            (
                child
                for child in add_dirs
                if any(
                    os.path.isfile(os.path.join(child, relative_path))
                    for relative_path in artifact_refs
                )
            ),
            add_dirs[0] if add_dirs else None,
        )
    if not add_dir:
        return
    for relative_path in sorted(artifact_refs):
        local_path = os.path.join(add_dir, relative_path)
        if not os.path.isfile(local_path):
            continue
        storage.upload_raw_file(
            job_id=job_id,
            relative_path=(artifact_mapping or {}).get(relative_path, relative_path),
            raw_prefix=raw_prefix,
            local_file_path=local_path,
        )


def publish_and_mark_refined(
    *,
    job_result_id: str,
    scope_id: str,
    target: RevisionTarget,
    output: ScopeRefinementOutput,
    scope_publisher: ScopePublisher,
) -> bool | None:
    now = utc_now_naive()
    publication_scope = build_publication_scope(
        target=target,
        job_result_id=job_result_id,
    )

    def publish(db: Session, scope: DocumentPublicationScope) -> bool | None:
        document = db.execute(
            select(Document)
            .where(Document.document_id == target.document_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            document is None
            or document.status != "active"
            or document.current_job_result_id != job_result_id
        ):
            db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == job_result_id)
                .where(DocumentScopeEnrichment.scope_id == scope_id)
                .where(
                    DocumentScopeEnrichment.claim_generation == target.claim_generation
                )
                .values(state="failed", updated_at=now)
            )
            return None

        db.execute(
            select(JobResult)
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one()
        claimed_scope = db.execute(
            select(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == job_result_id)
            .where(DocumentScopeEnrichment.scope_id == scope_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            claimed_scope is None
            or claimed_scope.state != "running"
            or claimed_scope.claim_generation != target.claim_generation
        ):
            return None

        scope_publisher(
            db,
            scope=scope,
            scope_id=scope_id,
            chunks=output.chunks,
            section_summaries=output.section_summaries,
        )
        db.execute(
            update(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == job_result_id)
            .where(DocumentScopeEnrichment.scope_id == scope_id)
            .values(
                state=REFINED_ENRICHMENT_STATE,
                refined_at=now,
                updated_at=now,
            )
        )
        remaining = int(
            db.execute(
                select(func.count())
                .select_from(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == job_result_id)
                .where(DocumentScopeEnrichment.state != REFINED_ENRICHMENT_STATE)
            ).scalar_one()
        )
        return remaining == 0

    return ProgressivePublication.publishForServingBackend(
        scope=publication_scope, update=publish, openDatabase=get_sync_db_context,
    )


def finalize_all_scopes_refined(
    *,
    job_result_id: str,
    target: RevisionTarget,
    storage: ResultStorage,
    workspace_dir: str,
    package_refs: PackageRefs,
) -> None:
    """Regenerate the final package once every scope of the revision is refined."""
    from shared.services.jobs.progressive_completion import commit_final_completion
    from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox

    with get_sync_db_context() as db:
        result = db.get(JobResult, job_result_id)
        state = (
            read_progressive_state(result.document_metadata)
            if result is not None
            else None
        )
        remaining = db.scalar(
            select(func.count())
            .select_from(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == job_result_id)
            .where(DocumentScopeEnrichment.state != "refined")
        )
        if remaining:
            return
        if state is not None:
            stages = state.stages
            required_stages = required_stages_before_finalization(
                JobMetadataHelper.get_parse_track(target.job_metadata)
            )
            if any(
                stages.get(stage_name, {}).get("status") != "complete"
                for stage_name in required_stages
            ):
                return
    token = (
        claim_progressive_stage(job_result_id=job_result_id, stage_name="finalization")
        if state is not None
        else None
    )
    if state is not None and token is None:
        return
    try:
        snapshot, zip_size = regenerate_and_upload_full_package(
            job_result_id=job_result_id,
            target=target,
            storage=storage,
            workspace_dir=workspace_dir,
            snapshot_id=f"snapshot_{uuid4().hex}",
            package_refs=package_refs,
        )
        if snapshot is None:
            raise ValueError("Final document package contains no chunks")
        with get_sync_db_context() as db:
            document = db.execute(
                select(Document)
                .where(Document.document_id == target.document_id)
                .with_for_update()
            ).scalar_one_or_none()
            result = db.execute(
                select(JobResult)
                .options(noload("*"))
                .where(JobResult.id == job_result_id)
                .with_for_update(of=JobResult)
            ).scalar_one_or_none()
            if (
                document is None
                or result is None
                or document.status != "active"
                or document.current_job_result_id != job_result_id
            ):
                return
            if state is not None:
                current = read_progressive_state(result.document_metadata)
                if current is None:
                    return
                stages: dict[str, EnhancementStage] = dict(current.stages)
                stage: EnhancementStage = {**stages.get("finalization", {})}
                if (
                    stage.get("claim_generation") != token
                    or stage.get("status") != "running"
                    or current.enhancement.status == "failed"
                ):
                    return
                stage["status"] = "complete"
                stages["finalization"] = stage
                current.stages = stages
                result.document_metadata = write_progressive_state(
                    result.document_metadata, current
                )
                event = commit_final_completion(
                    db, job_id=target.job_id, job_result=result, snapshot=snapshot
                )
            else:
                result.result_s3_key = snapshot.archive_key
                result.inline_payload = {"checksum": snapshot.checksum}
                result.document_metadata = {
                    **dict(result.document_metadata or {}),
                    "enrichment_status": "complete",
                }
                event = None
            result.result_size = zip_size
            job = db.execute(
                select(Job).where(Job.job_id == target.job_id).with_for_update()
            ).scalar_one()
            job.job_metadata = {
                **dict(job.job_metadata or {}),
                "enrichment_status": "complete",
            }
        SyncJobWebhookOutbox().enqueue_after_commit(event)
    except Exception as exc:
        if token is not None:
            finish_progressive_stage(
                job_result_id=job_result_id,
                stage_name="finalization",
                claim_generation=token,
                error="Final package upload failed",
            )
        logger.warning(
            "[progressive] final package incomplete for {}: {}", job_result_id, exc
        )


def regenerate_and_upload_full_package(
    *,
    job_result_id: str,
    target: RevisionTarget,
    storage: ResultStorage,
    workspace_dir: str,
    snapshot_id: str,
    package_refs: PackageRefs,
) -> tuple[StoredSnapshot | None, int]:
    chunks = load_published_package_chunks(job_result_id=job_result_id, target=target)
    if not chunks:
        return None, 0

    package_dir = os.path.join(workspace_dir, "_full_package", target.source_file_name)
    os.makedirs(package_dir, exist_ok=True)
    required_refs: set[str] = collect_referenced_artifact_refs(chunks) | set(
        package_refs.extra_refs
    )
    baseline_refs: set[str] = set(package_refs.baseline_refs)
    for relative_path in sorted(required_refs):
        target_path = Path(package_dir) / relative_path
        if target_path.is_file():
            continue
        suffix = Path(relative_path).suffix or ".tmp"
        temp_path = storage.download_raw_to_temp(
            job_id=target.job_id,
            relative_path=relative_path,
            raw_prefix=(
                target.serving_raw_prefix
                if relative_path in baseline_refs and target.serving_raw_prefix
                else target.raw_prefix
            ),
            suffix=suffix,
            temp_dir=package_dir,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        Path(temp_path).replace(target_path)

    ensure_doc_nav_json(
        package_dir,
        chunks,
        source_file_name=target.source_file_name,
        overwrite=True,
    )
    enrich_doc_nav_summaries(
        os.path.dirname(package_dir),
        source_file=target.source_file_name,
        force=True,
        use_llm=bool(
            JobMetadataHelper.get_parsing_param(
                target.job_metadata,
                "summary_use_llm",
                False,
            )
        ),
        top_summary_use_llm=bool(
            JobMetadataHelper.get_parsing_param(
                target.job_metadata,
                "top_summary_use_llm",
                True,
            )
        ),
        chunks=chunks,
    )
    zip_path, checksum, _statistics, zip_size = ZipResultService().generate_zip_package(
        job_id=target.job_id,
        chunks=chunks,
        add_dir=package_dir,
        source_file_name=target.source_file_name,
        data_id=JobMetadataHelper.get_data_id(target.job_metadata),
        job_metadata=target.job_metadata,
        temp_dir=workspace_dir,
    )
    bundle = storage.upload(
        job_id=target.job_id,
        result_dir=package_dir,
        zip_file_path=zip_path,
        artifact_refs=required_refs,
        snapshot_id=snapshot_id,
    )
    checksum_value = (
        str(checksum.get("value") or "")
        if isinstance(checksum, dict)
        else str(checksum or "")
    )
    return StoredSnapshot(
        snapshot_id=snapshot_id,
        stage="final",
        job_id=target.job_id,
        job_result_id=job_result_id,
        document_id=target.document_id,
        checksum=checksum_value,
        archive_key=bundle.zip_key,
    ), zip_size


def load_published_package_chunks(
    *,
    job_result_id: str,
    target: RevisionTarget,
) -> list[dict[str, Any]]:
    with get_sync_db_context() as db:
        rows = (
            db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == target.document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .order_by(DocumentChunk.sort_order, DocumentChunk.id)
            )
            .scalars()
            .all()
        )
        return [
            {
                "chunk_id": row.chunk_id,
                "type": row.chunk_type,
                "content": row.content or "",
                "path": row.source_chunk_path or "",
                "metadata": dict(row.chunk_metadata or {}),
                "order": row.sort_order,
            }
            for row in rows
        ]


def resolve_default_scope_publisher() -> ScopePublisher:
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )

    return replace_document_scope_content


def freeze_scope_artifacts(
    *,
    output: ScopeRefinementOutput,
    workspace_dir: str,
    claim_generation: str,
    raw_prefix: str | None = None,
    serving_raw_prefix: str | None = None,
) -> ScopeRefinementOutput:
    """Give one claim exclusive asset keys before any remote write.

    Fencing only the SQL publication cannot protect objects uploaded earlier.
    Claim-prefixed filenames make a late upload harmless while preserving the
    existing flat client asset directory contract. Persist their origins so
    partial Fine reads need no copy into the retained baseline snapshot.
    """
    if not output.artifact_refs:
        return output
    artifact_mapping: dict[str, str] = {
        reference: f"{reference.split('/', 1)[0]}/{claim_generation}-{sha256(reference.encode()).hexdigest()[:12]}-{Path(reference).name}"
        for reference in output.artifact_refs
    }
    add_dirs: list[Path] = [
        path for path in Path(workspace_dir).iterdir() if path.is_dir()
    ]
    if not add_dirs:
        raise ValueError("Scope artifact directory is missing")
    root: Path = add_dirs[0]
    for reference, immutable_reference in artifact_mapping.items():
        source: Path = root / reference
        target: Path = root / immutable_reference
        if not source.is_file():
            raise ValueError(f"Scope is missing required result asset: {reference}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        if target.suffix.lower() == ".html":
            target.write_text(
                _replace_artifact_references(
                    target.read_text(encoding="utf-8"), artifact_mapping
                ),
                encoding="utf-8",
            )
    chunks = cast(
        list[dict[str, Any]], _rewrite_artifact_value(output.chunks, artifact_mapping)
    )
    for previous, current in zip(output.chunks, chunks, strict=True):
        previous_content: str = str(previous.get("content") or "")
        metadata = current.get("metadata") or {}
        if raw_prefix:
            owned_refs: set[str] = collect_referenced_artifact_refs([current]) & set(
                artifact_mapping.values()
            )
            if owned_refs:
                metadata["artifact_origins"] = {
                    reference: raw_prefix for reference in sorted(owned_refs)
                }
                if serving_raw_prefix:
                    metadata["artifact_origin_snapshot_prefix"] = serving_raw_prefix
                current["metadata"] = metadata
        if "length" in metadata:
            metadata["length"] = len(str(current.get("content") or ""))
        connections = (current.get("metadata") or {}).get("connect_to") or []
        for connection in connections:
            if not isinstance(connection, dict) or not isinstance(
                connection.get("position"), dict
            ):
                continue
            position: dict[str, Any] = connection["position"]
            for boundary in ("start", "end"):
                value: object = position.get(boundary)
                if isinstance(value, int) and 0 <= value <= len(previous_content):
                    position[boundary] = len(
                        _replace_artifact_references(
                            previous_content[:value], artifact_mapping
                        )
                    )
    summaries = cast(
        dict[str, str],
        _rewrite_artifact_value(output.section_summaries, artifact_mapping),
    )
    return replace(
        output,
        chunks=chunks,
        section_summaries=summaries,
        artifact_refs=set(artifact_mapping.values()),
    )


def _replace_artifact_references(value: str, artifact_mapping: dict[str, str]) -> str:
    for previous in sorted(artifact_mapping, key=len, reverse=True):
        value = value.replace(previous, artifact_mapping[previous])
    return value


def _rewrite_artifact_value(value: object, artifact_mapping: dict[str, str]) -> object:
    if isinstance(value, str):
        return _replace_artifact_references(value, artifact_mapping)
    if isinstance(value, list):
        return [_rewrite_artifact_value(item, artifact_mapping) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_artifact_value(item, artifact_mapping)
            for key, item in value.items()
        }
    return value
