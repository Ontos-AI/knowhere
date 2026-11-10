"""Background final parse for chunk-track progressive documents."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, noload

from app.services.connect_builder.summary_builder import (
    build_section_summary_lookup,
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
    load_nav_top_summary,
)
from app.services.document_ingestion.artifact_refs import (
    collect_referenced_artifact_refs,
)
from app.services.document_ingestion.office_compat_normalizer import (
    normalize_office_source,
)
from app.services.document_ingestion.processing_context import (
    merge_persisted_job_token_usage,
)
from app.services.document_parser.formats.docx.baseline import (
    PAGE_TEXT_CACHE_REL,
    load_page_text_cache,
)
from app.services.document_parser.formats.docx.page_attribution import (
    attribute_pages_to_leaves,
)
from app.services.document_parser.orchestration.parse_session import _resolve_output_paths
from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess
from app.services.document_parser.support.internal_parse_name import (
    prepare_internal_parse_input,
)
from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.progressive_ingestion import (
    EnhancementStage,
    StoredSnapshot,
)
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.ai.llm_overrides import cleanup_llm_overrides, init_llm_overrides
from shared.services.ai.token_tracking import (
    cleanup_token_tracker,
    get_current_token_tracker,
    init_token_tracker,
)
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox, WebhookOutboxEvent
from shared.services.jobs.progressive_completion import commit_final_completion
from shared.services.jobs.progressive_stages import (
    claim_progressive_stage,
    finish_progressive_stage,
)
from shared.services.jobs.progressive_ingestion import (
    read_progressive_state,
    write_progressive_state,
)
from shared.services.retrieval.cache_service import (
    bump_retrieval_namespace_cache_version_sync,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.graph.service import DocumentGraphService
from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
from shared.services.retrieval.publication_content import (
    deduplicate_chunks_by_source_path,
    replace_document_revision_content,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.storage.job_file_storage import JobFileStorage
from shared.services.storage.result_storage import ResultStorage, get_result_storage
from shared.services.storage.zip_result_service import ZipResultService


@dataclass(frozen=True)
class _ChunkFinalTarget:
    document_id: str
    job_id: str
    user_id: str
    namespace: str
    source_file_name: str
    s3_key: str
    raw_prefix: str | None
    job_metadata: dict[str, Any]


def execute_chunk_final_enrichment(
    *,
    job_result_id: str,
    result_storage_factory: Callable[[], ResultStorage] = get_result_storage,
) -> dict[str, Any]:
    """Run the full chunk-track parse and replace the baseline revision."""
    target = _load_chunk_final_target(job_result_id=job_result_id)
    if target is None:
        return {"status": "aborted", "job_result_id": job_result_id}

    claim_token = claim_progressive_stage(
        job_result_id=job_result_id,
        stage_name="finalization",
    )
    if claim_token is None:
        return {"status": "skipped", "job_result_id": job_result_id}

    storage = result_storage_factory()
    workspace_dir = tempfile.mkdtemp(prefix=f"chunk_final_{job_result_id}_")
    init_llm_overrides(JobMetadataHelper.get_llm_config(target.job_metadata))
    init_token_tracker()
    try:
        input_dir = os.path.join(workspace_dir, "input")
        output_root = os.path.join(workspace_dir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_root, exist_ok=True)
        add_dir = _download_resume_artifacts(
            storage=storage,
            target=target,
            output_root=output_root,
        )
        source_path = _download_original_source(
            target=target,
            input_dir=input_dir,
            add_dir=add_dir,
        )
        parsed_df, add_dir = _run_full_chunk_parse(
            target=target,
            source_path=source_path,
            output_root=output_root,
            add_dir=add_dir,
        )
        chunks = dataframe_to_chunks(parsed_df)
        if not chunks:
            raise ValueError("Final chunk parse produced no chunks")
        section_summaries = _build_section_summaries(
            add_dir=add_dir,
            chunks=chunks,
            source_file_name=target.source_file_name,
            job_metadata=target.job_metadata,
        )
        document_top_summary = (
            load_nav_top_summary(add_dir, target.source_file_name)
            if add_dir and target.source_file_name
            else ""
        )
        publish_final = partial(
            _publish_final_revision,
            job_result_id=job_result_id,
            target=target,
            chunks=chunks,
            section_summaries=section_summaries,
            document_top_summary=document_top_summary,
        )
        is_native: bool = settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"
        if not is_native:
            publish_final()
        snapshot, zip_size = _upload_final_package(
            job_result_id=job_result_id,
            target=target,
            chunks=chunks,
            add_dir=add_dir,
            workspace_dir=workspace_dir,
            storage=storage,
        )
        if is_native:
            publish_final(complete=partial(
                _complete_final_snapshot, target=target, snapshot=snapshot,
                zip_size=zip_size, claim_token=claim_token,
            ))
        else:
            _commit_final_snapshot(
                job_result_id=job_result_id, target=target, snapshot=snapshot,
                zip_size=zip_size, claim_token=claim_token,
            )
        return {
            "status": "success",
            "job_result_id": job_result_id,
            "snapshot_id": snapshot.snapshot_id,
        }
    except Exception as exc:
        finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="finalization",
            claim_generation=claim_token,
            error=str(exc),
        )
        logger.warning(
            "Chunk final enrichment failed for {}: {}",
            job_result_id,
            exc,
        )
        raise
    finally:
        tracker = get_current_token_tracker()
        if tracker is not None:
            try:
                merge_persisted_job_token_usage(
                    job_id=target.job_id,
                    delta=dict(tracker),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to merge chunk-final token usage job_id={}: {}",
                    target.job_id,
                    exc,
                )
        cleanup_llm_overrides()
        cleanup_token_tracker()
        shutil.rmtree(workspace_dir, ignore_errors=True)


def _load_chunk_final_target(*, job_result_id: str) -> _ChunkFinalTarget | None:
    with get_sync_db_context() as db:
        job_result = db.execute(
            select(JobResult).where(JobResult.id == job_result_id)
        ).scalar_one_or_none()
        if job_result is None:
            return None
        document_id = job_result.document_id or job_result.demo_document_id
        document = (
            db.execute(
                select(Document).where(Document.document_id == str(document_id))
            ).scalar_one_or_none()
            if document_id
            else None
        )
        job = db.execute(
            select(Job).where(Job.job_id == job_result.job_id)
        ).scalar_one_or_none()
        if (
            document is None
            or job is None
            or document.status != "active"
            or document.current_job_result_id != job_result_id
            or not job.s3_key
        ):
            return None
        job_metadata = dict(job.job_metadata or {})
        if (
            JobMetadataHelper.get_parse_track(job_metadata) != "chunk"
            or JobMetadataHelper.get_ingestion_mode(job_metadata) != "progressive"
        ):
            return None
        raw_prefix_val = job_metadata.get("result_raw_prefix")
        raw_prefix = (
            str(raw_prefix_val)
            if isinstance(raw_prefix_val, str) and raw_prefix_val
            else None
        )
        source_file_name = (
            JobMetadataHelper.get_source_file_name(job_metadata)
            or document.source_file_name
            or os.path.basename(job.s3_key)
        )
        corpus_storage = CorpusStorage.resolve_namespace(document.namespace)
        return _ChunkFinalTarget(
            document_id=str(document.document_id),
            job_id=str(job.job_id),
            user_id=corpus_storage.resolve_owner(str(job.user_id)),
            namespace=normalize_retrieval_namespace(document.namespace),
            source_file_name=str(source_file_name),
            s3_key=str(job.s3_key),
            raw_prefix=raw_prefix,
            job_metadata=job_metadata,
        )


def _download_original_source(
    *,
    target: _ChunkFinalTarget,
    input_dir: str,
    add_dir: str | None = None,
) -> str:
    extension = os.path.splitext(target.source_file_name)[1].lower()
    source_name = os.path.basename(target.source_file_name)
    resumed_source = (
        os.path.join(add_dir, source_name)
        if add_dir and source_name
        else ""
    )
    if resumed_source and os.path.isfile(resumed_source):
        local_path = os.path.join(input_dir, source_name)
        Path(resumed_source).replace(local_path)
    else:
        local_path = JobFileStorage().download_upload_to_temp(
            target.s3_key,
            suffix=extension,
            temp_dir=input_dir,
        )
    prepared = prepare_internal_parse_input(
        local_path,
        target.source_file_name,
        fallback_ext=extension,
        prefer_fallback_ext=True,
    )
    return normalize_office_source(prepared.file_path).file_path


def _download_resume_artifacts(
    *,
    storage: ResultStorage,
    target: _ChunkFinalTarget,
    output_root: str,
) -> str:
    _relative_root, add_dir = _resolve_output_paths(
        filename=target.source_file_name,
        internal_output_filename=target.source_file_name,
        output_dir=output_root,
    )
    configured_refs = target.job_metadata.get("chunk_progressive_artifact_refs")
    if not isinstance(configured_refs, list):
        return add_dir
    for relative_path in (str(item) for item in configured_refs):
        if not relative_path:
            continue
        suffix = Path(relative_path).suffix or ".tmp"
        temp_path = storage.download_raw_to_temp(
            job_id=target.job_id,
            relative_path=relative_path,
            raw_prefix=target.raw_prefix,
            suffix=suffix,
            temp_dir=add_dir,
        )
        destination = Path(add_dir) / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        Path(temp_path).replace(destination)
    return add_dir


def _run_full_chunk_parse(
    *,
    target: _ChunkFinalTarget,
    source_path: str,
    output_root: str,
    add_dir: str,
) -> tuple[pd.DataFrame, str]:
    extension = os.path.splitext(target.source_file_name)[1].lower()
    relative_root, full_output_dir = _resolve_output_paths(
        filename=target.source_file_name,
        internal_output_filename=target.source_file_name,
        output_dir=output_root,
    )
    if full_output_dir != add_dir:
        add_dir = full_output_dir
    llm_paras = _base_llm_paras(target.job_metadata)
    if extension in {".md", ".txt"}:
        parsed_df = _parse_markdown_or_text(
            source_path=source_path,
            extension=extension,
            output_dir=full_output_dir,
            relative_root=relative_root,
            llm_paras=llm_paras,
        )
    elif extension == ".docx":
        parsed_df = _parse_docx_final(
            source_path=source_path,
            output_dir=full_output_dir,
            filename=target.source_file_name,
            relative_root=relative_root,
            llm_paras=llm_paras,
        )
    else:
        raise ValueError(
            f"chunk-track progressive final does not support {extension or 'unknown'}"
        )
    processed = apply_parse_postprocess(full_output_dir, parsed_df)
    if processed is None:
        raise ValueError("Final chunk parse returned no dataframe")
    return processed, full_output_dir


def _parse_markdown_or_text(
    *,
    source_path: str,
    extension: str,
    output_dir: str,
    relative_root: str,
    llm_paras: dict[str, Any],
) -> pd.DataFrame:
    from app.services.document_parser.formats.markdown.parser import parse_md
    from app.services.document_parser.formats.text.parser import parse_texts

    if extension == ".txt":
        md_lines = parse_texts(file_path=source_path)
        return parse_md(
            output_dir,
            source_type="md",
            md_lines=md_lines,
            base_llm_paras=llm_paras,
            relative_root=relative_root,
        )
    return parse_md(
        output_dir,
        source_type="md",
        file_path=source_path,
        base_llm_paras=llm_paras,
        relative_root=relative_root,
    )


def _parse_docx_final(
    *,
    source_path: str,
    output_dir: str,
    filename: str,
    relative_root: str,
    llm_paras: dict[str, Any],
) -> pd.DataFrame:
    from app.services.document_parser.formats.docx.parser import (
        convert_doc2dics,
        parse_docx,
    )

    parsed_structure, dataframe_list = parse_docx(
        source_path,
        llm_paras,
        output_dir,
        filename,
        "",
        relative_root=relative_root,
    )
    parsed_df = convert_doc2dics(
        parsed_structure,
        dataframe_list,
        output_dir,
        base_llm_paras=llm_paras,
        relative_root=relative_root,
    )
    if os.path.isfile(os.path.join(output_dir, PAGE_TEXT_CACHE_REL)):
        _write_leaf_page_nums(parsed_df, load_page_text_cache(output_dir))
    return parsed_df


def _write_leaf_page_nums(parsed_df: pd.DataFrame, page_texts: dict[int, str]) -> None:
    if parsed_df is None or parsed_df.empty or "type" not in parsed_df.columns:
        return
    text_indices: list[Any] = []
    leaf_texts: list[str] = []
    for index, row in parsed_df.iterrows():
        marker = str(row.get("type") or "").strip().split("\n", 1)[0].lower()
        if marker not in {"text", "ptxt"}:
            continue
        text_indices.append(index)
        leaf_texts.append(str(row.get("content") or ""))
    attributed = attribute_pages_to_leaves(leaf_texts, page_texts)
    for local_index, frame_index in enumerate(text_indices):
        pages = attributed.get(local_index, [])
        parsed_df.at[frame_index, "page_nums"] = ",".join(str(page) for page in pages)


def _publish_final_revision(
    *,
    job_result_id: str,
    target: _ChunkFinalTarget,
    chunks: list[dict[str, Any]],
    section_summaries: dict[str, str],
    document_top_summary: str | None = None,
    complete: Callable[[Session, JobResult], WebhookOutboxEvent | None] | None = None,
) -> None:
    publication_scope = DocumentPublicationScope(
        user_id=target.user_id,
        namespace=target.namespace,
        document_id=target.document_id,
        job_result_id=job_result_id,
        source_file_name=target.source_file_name,
    )
    def publish(db: Session, scope: DocumentPublicationScope) -> WebhookOutboxEvent | None:
        document = db.execute(
            select(Document)
            .where(Document.document_id == target.document_id)
            .with_for_update()
        ).scalar_one_or_none()
        job_result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if (
            document is None
            or job_result is None
            or document.status != "active"
            or document.current_job_result_id != job_result_id
        ):
            raise ValueError("Final publish lost the claimed revision")
        manifest_payload = replace_document_revision_content(
            db,
            scope=scope,
            chunks=deduplicate_chunks_by_source_path(chunks),
            section_summaries=section_summaries,
        )
        RetrievalPublicationService().update_namespace_snapshot(
            db,
            scope=scope,
            manifest_payload=manifest_payload,
        )
        DocumentGraphService().publish_document_graph(
            db,
            user_id=target.user_id,
            namespace=target.namespace,
            document_id=target.document_id,
            job_result_id=job_result_id,
            top_summary=document_top_summary,
        )
        return complete(db, job_result) if complete is not None else None

    event = ProgressivePublication.publishForServingBackend(
        scope=publication_scope, update=publish, openDatabase=get_sync_db_context,
    )
    bump_retrieval_namespace_cache_version_sync(
        user_id=target.user_id,
        namespace=target.namespace,
    )
    SyncJobWebhookOutbox().enqueue_after_commit(event)


def _upload_final_package(
    *,
    job_result_id: str,
    target: _ChunkFinalTarget,
    chunks: list[dict[str, Any]],
    add_dir: str,
    workspace_dir: str,
    storage: ResultStorage,
) -> tuple[StoredSnapshot, int]:
    artifact_refs = collect_referenced_artifact_refs(chunks)
    if add_dir and os.path.isfile(os.path.join(add_dir, "source.pdf")):
        artifact_refs.add("source.pdf")
    zip_path, checksum, _statistics, zip_size = ZipResultService().generate_zip_package(
        job_id=target.job_id,
        chunks=chunks,
        add_dir=add_dir,
        source_file_name=target.source_file_name,
        data_id=JobMetadataHelper.get_data_id(target.job_metadata),
        job_metadata=target.job_metadata,
        temp_dir=workspace_dir,
    )
    snapshot_id = f"snapshot_{uuid4().hex}"
    bundle = storage.upload(
        job_id=target.job_id,
        result_dir=add_dir,
        zip_file_path=zip_path,
        artifact_refs=artifact_refs,
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


def _commit_final_snapshot(
    *,
    job_result_id: str,
    target: _ChunkFinalTarget,
    snapshot: StoredSnapshot,
    zip_size: int,
    claim_token: str,
) -> None:
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
            raise ValueError("Final commit lost the claimed revision")
        event = _complete_final_snapshot(
            db, result, target=target, snapshot=snapshot, zip_size=zip_size, claim_token=claim_token,
        )
    SyncJobWebhookOutbox().enqueue_after_commit(event)


def _complete_final_snapshot(
    db: Session,
    result: JobResult,
    *,
    target: _ChunkFinalTarget,
    snapshot: StoredSnapshot,
    zip_size: int,
    claim_token: str,
) -> WebhookOutboxEvent | None:
    current = read_progressive_state(result.document_metadata)
    if current is None:
        raise ValueError("Final enhancement requires a committed baseline")
    stages: dict[str, EnhancementStage] = dict(current.stages)
    stage: EnhancementStage = {**stages.get("finalization", {})}
    if (
        stage.get("claim_generation") != claim_token
        or stage.get("status") != "running"
        or current.enhancement.status == "failed"
    ):
        raise ValueError("Finalization claim is no longer valid")
    stage["status"] = "complete"
    stages["finalization"] = stage
    current.stages = stages
    result.document_metadata = write_progressive_state(
        result.document_metadata,
        current,
    )
    event = commit_final_completion(
        db,
        job_id=target.job_id,
        job_result=result,
        snapshot=snapshot,
    )
    result.result_size = zip_size
    job = db.execute(
        select(Job).where(Job.job_id == target.job_id).with_for_update()
    ).scalar_one()
    job.job_metadata = {
        **dict(job.job_metadata or {}),
        "enrichment_status": "complete",
    }
    return event


def _build_section_summaries(
    *,
    add_dir: str,
    chunks: list[dict[str, Any]],
    source_file_name: str,
    job_metadata: dict[str, Any],
) -> dict[str, str]:
    if not add_dir or not source_file_name:
        return {}
    ensure_doc_nav_json(
        add_dir,
        chunks,
        source_file_name=source_file_name,
        overwrite=True,
    )
    try:
        enrich_doc_nav_summaries(
            os.path.dirname(add_dir),
            source_file=source_file_name,
            use_llm=bool(
                JobMetadataHelper.get_parsing_param(
                    job_metadata,
                    "summary_use_llm",
                    False,
                )
            ),
            top_summary_use_llm=bool(
                JobMetadataHelper.get_parsing_param(
                    job_metadata,
                    "top_summary_use_llm",
                    True,
                )
            ),
            chunks=chunks,
        )
        return build_section_summary_lookup(add_dir)
    except Exception as exc:
        logger.warning(f"doc_nav enrichment failed during chunk final (non-fatal): {exc}")
        return {}


def _base_llm_paras(job_metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "llm_histories": 5,
        "smart_title_parse": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "smart_title_parse",
            True,
        ),
        "summary_image": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "summary_image",
            True,
        ),
        "summary_table": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "summary_table",
            True,
        ),
        "summary_txt": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "summary_txt",
            True,
        ),
        "stopwords": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "stopwords",
            None,
        ),
        "doc_type": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "doc_type",
            "auto",
        ),
        "frag_desc": JobMetadataHelper.get_parsing_param(
            job_metadata,
            "add_frag_desc",
            "",
        ),
        "model_name": settings.NORMOL_MODEL,
        "hierarchy_model_name": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
    }
