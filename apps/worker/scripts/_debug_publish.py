from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

DEFAULT_DEBUG_USER_ID = "debug_local_user"
DEFAULT_NAMESPACE = "default"


@dataclass(frozen=True)
class DebugPublishResult:
    job_id: str
    document_id: str | None
    user_id: str
    namespace: str
    source_file_name: str
    chunk_count: int
    referenced_asset_count: int
    uploaded_asset_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "document_id": self.document_id,
            "user_id": self.user_id,
            "namespace": self.namespace,
            "source_file_name": self.source_file_name,
            "chunk_count": self.chunk_count,
            "referenced_asset_count": self.referenced_asset_count,
            "uploaded_asset_count": self.uploaded_asset_count,
        }


def load_chunks_from_result_dir(result_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    chunks_path = Path(result_dir).expanduser().resolve() / "chunks.json"
    with chunks_path.open(encoding="utf-8") as f:
        raw = json.load(f)
    chunks = raw.get("chunks", raw) if isinstance(raw, dict) else raw
    if not isinstance(chunks, list):
        raise ValueError(f"Invalid chunks.json shape: {chunks_path}")
    return chunks


def publish_debug_result_dir(
    *,
    result_dir: str | os.PathLike[str],
    source_file_name: str,
    chunks: list[dict[str, Any]] | None = None,
    job_id: str | None = None,
    user_id: str = DEFAULT_DEBUG_USER_ID,
    namespace: str = DEFAULT_NAMESPACE,
    parse_track: str | None = None,
    upload_assets: bool = True,
    upload_only: bool = False,
) -> DebugPublishResult:
    """Publish an already materialized parse result directory to local debug DB."""
    add_dir = str(Path(result_dir).expanduser().resolve())
    resolved_chunks = chunks if chunks is not None else load_chunks_from_result_dir(add_dir)
    resolved_job_id = job_id or f"debug_{uuid4().hex[:8]}"
    resolved_parse_track = parse_track or _infer_parse_track(resolved_chunks)

    from app.services.document_ingestion.artifact_refs import (
        collect_referenced_artifact_refs,
    )

    refs = collect_referenced_artifact_refs(resolved_chunks)
    logger.info(
        "debug publish: chunks={} parse_track={} referenced_assets={}",
        len(resolved_chunks),
        resolved_parse_track,
        len(refs),
    )

    if upload_assets:
        _ensure_buckets()

    uploaded_count = 0
    if upload_only:
        if not job_id:
            raise ValueError("upload_only requires an explicit job_id")
        if upload_assets:
            uploaded_count = _upload_assets(
                job_id=resolved_job_id,
                result_dir=add_dir,
                refs=refs,
            )
        return DebugPublishResult(
            job_id=resolved_job_id,
            document_id=None,
            user_id=user_id,
            namespace=namespace,
            source_file_name=source_file_name,
            chunk_count=len(resolved_chunks),
            referenced_asset_count=len(refs),
            uploaded_asset_count=uploaded_count,
        )

    document_id = _publish_chunks_to_db(
        chunks=resolved_chunks,
        add_dir=add_dir,
        source_file_name=source_file_name,
        job_id=resolved_job_id,
        user_id=user_id,
        namespace=namespace,
        parse_track=resolved_parse_track,
    )

    if upload_assets:
        uploaded_count = _upload_assets(
            job_id=resolved_job_id,
            result_dir=add_dir,
            refs=refs,
        )

    return DebugPublishResult(
        job_id=resolved_job_id,
        document_id=document_id,
        user_id=user_id,
        namespace=namespace,
        source_file_name=source_file_name,
        chunk_count=len(resolved_chunks),
        referenced_asset_count=len(refs),
        uploaded_asset_count=uploaded_count,
    )


def _infer_parse_track(chunks: list[dict[str, Any]]) -> str:
    return (
        "page_memory"
        if any(str(c.get("type") or c.get("chunk_type") or "").lower() == "page" for c in chunks)
        else "chunk"
    )


def _ensure_buckets() -> None:
    from shared.core.config import settings

    client = settings.get_s3_client()
    for bucket in {
        settings.S3_BUCKET_NAME,
        getattr(settings, "S3_RESULTS_BUCKET", settings.S3_BUCKET_NAME),
    }:
        try:
            client.head_bucket(Bucket=bucket)
            logger.info("  bucket exists: {}", bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
            logger.info("  bucket created: {}", bucket)


def _upload_assets(*, job_id: str, result_dir: str, refs: set[str]) -> int:
    """Upload only referenced client artifacts to results/{job_id}/."""
    from concurrent.futures import ThreadPoolExecutor

    from shared.services.storage.result_storage import get_result_storage

    storage = get_result_storage()
    result_path = Path(result_dir)
    bucket = storage.results_bucket

    tasks: list[tuple[str, str]] = []
    missing = 0
    for relative in sorted(refs):
        local = result_path / relative
        if not local.is_file():
            missing += 1
            logger.warning("  referenced asset missing on disk: {}", relative)
            continue
        raw_key = storage.build_raw_key(job_id=job_id, relative_path=relative)
        tasks.append((str(local), raw_key))

    def _put(item: tuple[str, str]) -> None:
        local_path, raw_key = item
        storage._job_file_storage.upload_local_file(  # noqa: SLF001
            local_path,
            raw_key,
            bucket=bucket,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_put, tasks))

    logger.info(
        "  uploaded {} referenced asset files under results/{} (missing={})",
        len(tasks),
        job_id,
        missing,
    )
    return len(tasks)


def _publish_chunks_to_db(
    *,
    chunks: list[dict[str, Any]],
    add_dir: str,
    source_file_name: str,
    job_id: str,
    user_id: str,
    namespace: str,
    parse_track: str,
) -> str:
    from sqlalchemy import select, text as sql_text

    from app.services.connect_builder.summary_builder import (
        build_section_summary_lookup,
        enrich_doc_nav_summaries,
    )
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.document import DocumentSection, GraphNode
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.publication_service import RetrievalPublicationService

    file_dir_name = os.path.basename(add_dir)
    logger.info("Step 1: enrich_doc_nav_summaries")
    enrich_doc_nav_summaries(
        os.path.dirname(add_dir),
        source_file=file_dir_name,
        use_llm=False,
    )

    logger.info("Step 2: build_section_summary_lookup")
    section_summaries = build_section_summary_lookup(add_dir)
    logger.info("  section_summaries entries: {}", len(section_summaries))

    logger.info("Step 3: inject document_top_summary into chunk metadata")
    _inject_navigation_metadata(
        chunks=chunks,
        add_dir=add_dir,
        source_file_name=source_file_name,
    )

    logger.info("Step 4-6: DB publication (job_id={})", job_id)
    with get_sync_db_context() as db:
        db.execute(
            sql_text(
                'INSERT INTO "user" (id, name, email) VALUES (:uid, :name, :email) '
                "ON CONFLICT DO NOTHING"
            ),
            {"uid": user_id, "name": "Debug User", "email": "debug@local.test"},
        )
        db.flush()

        job = Job(
            job_id=job_id,
            user_id=user_id,
            status="PROCESSING",
            job_type="parse",
            source_type="direct_upload",
            file_path=source_file_name,
            job_metadata={
                "namespace": namespace,
                "source_file_name": source_file_name,
                "parse_track": parse_track,
            },
        )
        db.add(job)
        db.flush()

        job_result = JobResult(
            job_id=job_id,
            delivery_mode="url",
            document_metadata={},
        )
        db.add(job_result)
        db.flush()
        job_result_id = job_result.id

        pub_service = RetrievalPublicationService()
        published = pub_service.publish_document_state(
            db,
            job_id=job_id,
            job_result_id=job_result_id,
            chunks=chunks,
            section_summaries=section_summaries,
        )
        document_id = published.document_id if published else None
        if not document_id:
            db.rollback()
            raise RuntimeError("publish_document_state returned None")
        logger.info("  published document_id: {}", document_id)

        pub_service.publish_document_graph(
            db,
            job_id=job_id,
            job_result_id=job_result_id,
        )

        sections = list(
            db.execute(
                select(
                    DocumentSection.section_level,
                    DocumentSection.section_title,
                    DocumentSection.summary,
                )
                .where(DocumentSection.document_id == document_id)
                .where(DocumentSection.job_result_id == job_result_id)
                .order_by(DocumentSection.sort_order)
            ).all()
        )
        with_summary = sum(1 for _, _, summary in sections if summary)
        logger.info(
            "  DocumentSection rows: {}, with_summary: {}",
            len(sections),
            with_summary,
        )

        graph_node = db.execute(
            select(GraphNode)
            .where(GraphNode.owner_document_id == document_id)
            .where(GraphNode.node_kind == "document")
        ).scalar_one_or_none()
        logger.info("  GraphNode published: {}", bool(graph_node))

        db.commit()
        logger.info("  DB transaction committed")
        return str(document_id)


def _inject_navigation_metadata(
    *,
    chunks: list[dict[str, Any]],
    add_dir: str,
    source_file_name: str,
) -> None:
    from app.services.connect_builder.summary_builder import load_nav_top_summary

    document_top_summary = load_nav_top_summary(add_dir, source_file_name)
    for chunk in chunks:
        metadata = chunk.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            chunk["metadata"] = metadata
        if document_top_summary:
            metadata["document_top_summary"] = document_top_summary
