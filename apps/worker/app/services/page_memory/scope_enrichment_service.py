"""Phase B per-scope enrichment service for page-memory documents."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from typing import Any, Callable, cast

import pandas as pd
from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, noload

from app.services.connect_builder.summary_builder import (
    build_section_summary_lookup,
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
)
from app.services.document_agent.manifest import PageFeature, PageLabel
from app.services.document_agent.pdf_text import coerce_page_text_cache
from app.services.document_ingestion.artifact_refs import (
    collect_referenced_artifact_refs,
)
from app.services.document_ingestion.processing_context import (
    merge_persisted_job_token_usage,
)
from app.services.document_parser.support.parser_rows import PARSER_DF_COLUMNS
from app.services.page_memory._utils import CoarseScope, sort_skeletons
from app.services.page_memory.memory_service import (
    assemble_c7_node_rows,
    embed_rendered_pages,
    run_single_scope,
    treat_profile_as_scanned,
)
from app.services.page_memory.phase_a import (
    PHASE_B_ANATOMY_REL,
    PHASE_B_BLACKBOARD_REL,
    PHASE_B_BODY_TEXT_REL,
    PHASE_B_FULL_TEXT_REL,
    PHASE_B_MANIFEST_REL,
    PHASE_B_SOURCE_PDF_REL,
    build_phase_a,
    next_title_by_path_from_manifest,
    persist_phase_b_inputs,
    skeletons_from_rows,
)
from app.services.page_memory.skeleton_extractor import (
    SectionSkeleton,
    collapse_single_child_chains,
)
from shared.core.config import app_config
from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentScopeEnrichment,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    EnhancementStage,
    StoredSnapshot,
)
from shared.services.jobs.progressive_ingestion import (
    read_progressive_state,
    write_progressive_state,
)
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.ai.llm_overrides import cleanup_llm_overrides, init_llm_overrides
from shared.services.ai.token_tracking import (
    cleanup_token_tracker,
    get_current_token_tracker,
    init_token_tracker,
)
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.jobs.lifecycle.scope_enrichments import (
    scope_claimable_filter,
    write_coarse_scope_enrichments,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.storage.result_storage import ResultStorage, get_result_storage
from shared.services.storage.zip_result_service import ZipResultService
from shared.utils.utc_now import utc_now_naive

REFINED_ENRICHMENT_STATE = "refined"


def _flush_parse_token_usage(job_id: str) -> None:
    tracker = get_current_token_tracker()
    if tracker is None:
        return
    try:
        merge_persisted_job_token_usage(job_id=job_id, delta=dict(tracker))
    except Exception as exc:
        logger.warning(
            "[page_memory] failed to merge enrichment token usage job_id={}: {}",
            job_id,
            exc,
        )

ScopePublisher = Callable[..., Any]


@dataclass(frozen=True)
class _ClaimedScopeTarget:
    document_id: str
    job_id: str
    user_id: str
    namespace: str
    source_file_name: str
    raw_prefix: str | None
    job_metadata: dict[str, Any]
    page_memory_config: PageMemoryConfig
    claim_generation: str | None = None


@dataclass(frozen=True)
class ScopeRefinementOutput:
    scope_id: str
    dataframe: pd.DataFrame
    chunks: list[dict[str, Any]]
    section_summaries: dict[str, str]
    artifact_refs: set[str]


def execute_structure_enrichment(
    *,
    job_result_id: str,
    result_storage_factory: Callable[[], ResultStorage] = get_result_storage,
    scope_publisher: ScopePublisher | None = None,
) -> dict[str, Any]:
    """Resume profiling (TOC + anatomy + Phase A) and publish the coarse structure."""
    claim_status, target = _load_and_validate_structure_target(
        job_result_id=job_result_id,
    )
    if target is None:
        return {
            "status": claim_status,
            "job_result_id": job_result_id,
        }

    storage = result_storage_factory()
    publisher = scope_publisher or _resolve_default_scope_publisher()
    workspace_dir = tempfile.mkdtemp(prefix=f"pm_struct_{job_result_id}_")
    init_llm_overrides(JobMetadataHelper.get_llm_config(target.job_metadata))
    init_token_tracker()
    try:
        add_dir = os.path.join(
            workspace_dir,
            Path(target.source_file_name).stem or "document",
        )
        os.makedirs(add_dir, exist_ok=True)
        _download_phase_b_inputs(
            storage=storage,
            job_id=target.job_id,
            raw_prefix=target.raw_prefix,
            job_metadata=target.job_metadata,
            output_dir=add_dir,
        )
        publication_scope = _build_publication_scope(
            target=target,
            job_result_id=job_result_id,
        )
        _generate_and_persist_page_embeddings(
            add_dir=add_dir,
            workspace_dir=workspace_dir,
            publication_scope=publication_scope,
        )
        profile = _resume_profile_from_m0_workspace(
            add_dir=add_dir,
            target=target,
            job_result_id=job_result_id,
        )
        phase_a = build_phase_a(
            profile=profile,
            filename=target.source_file_name,
            trace_recorder=None,
        )
        phase_b_refs = persist_phase_b_inputs(
            output_dir=add_dir,
            profile=profile,
            manifest=phase_a.manifest,
        )
        phase_b_artifacts: dict[str, str] = {
            relative_path: f"phase_b/{target.claim_generation}/{Path(relative_path).name}"
            for relative_path in {
                PHASE_B_MANIFEST_REL,
                PHASE_B_ANATOMY_REL,
                PHASE_B_FULL_TEXT_REL,
                PHASE_B_BODY_TEXT_REL,
            }
            & set(phase_b_refs)
        }
        _upload_scope_artifacts(
            storage=storage,
            job_id=target.job_id,
            raw_prefix=target.raw_prefix,
            workspace_dir=workspace_dir,
            add_dir=add_dir,
            artifact_refs=set(phase_b_artifacts),
            artifact_mapping=phase_b_artifacts,
        )
        coarse_chunks = cast(
            list[dict[str, Any]], dataframe_to_chunks(phase_a.coarse_df)
        )
        coarse_summaries = _build_scope_section_summaries(
            output_dir=add_dir,
            chunks=coarse_chunks,
            source_file_name=target.source_file_name,
            summary_use_llm=bool(
                JobMetadataHelper.get_parsing_param(
                    target.job_metadata, "summary_use_llm", False
                )
            ),
        )
        published_ok = _publish_coarse_structure_and_write_scopes(
            job_result_id=job_result_id,
            target=target,
            publication_scope=publication_scope,
            chunks=coarse_chunks,
            section_summaries=coarse_summaries,
            phase_b_refs=phase_b_refs,
            phase_b_artifacts=phase_b_artifacts,
            scope_publisher=publisher,
        )
        if not published_ok:
            return {"status": "aborted", "job_result_id": job_result_id}

        return {
            "status": "coarse",
            "job_result_id": job_result_id,
            "scope_count": len(phase_a.coarse_scopes),
            "chunk_count": len(coarse_chunks),
        }
    except Exception:
        _release_structure_claim(
            job_id=target.job_id,
            job_result_id=job_result_id,
            claim_generation=target.claim_generation,
        )
        _finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="structure",
            claim_generation=str(target.claim_generation),
            error="Structure enhancement failed",
        )
        raise
    finally:
        _flush_parse_token_usage(target.job_id)
        cleanup_token_tracker()
        cleanup_llm_overrides()
        shutil.rmtree(workspace_dir, ignore_errors=True)


def execute_scope_enrichment(
    *,
    job_result_id: str,
    scope_id: str,
    result_storage_factory: Callable[[], ResultStorage] = get_result_storage,
    scope_publisher: ScopePublisher | None = None,
) -> dict[str, Any]:
    """Claim one scope, run C1-C7 on its subtree, and publish its refined rows."""
    claim_status, target = _claim_and_validate_scope(
        job_result_id=job_result_id,
        scope_id=scope_id,
    )
    if target is None:
        return {
            "status": claim_status,
            "job_result_id": job_result_id,
            "scope_id": scope_id,
        }

    storage = result_storage_factory()
    publisher = scope_publisher or _resolve_default_scope_publisher()
    workspace_dir = tempfile.mkdtemp(prefix=f"pm_scope_{scope_id}_")
    init_llm_overrides(JobMetadataHelper.get_llm_config(target.job_metadata))
    init_token_tracker()
    try:
        output = _run_scope_in_workspace(
            workspace_dir=workspace_dir,
            target=target,
            scope_id=scope_id,
            storage=storage,
        )
        output = _freeze_scope_artifacts(
            output=output,
            workspace_dir=workspace_dir,
            claim_generation=str(target.claim_generation),
        )
        _upload_scope_artifacts(
            storage=storage,
            job_id=target.job_id,
            raw_prefix=target.raw_prefix,
            workspace_dir=workspace_dir,
            artifact_refs=output.artifact_refs,
        )
        all_refined = _publish_and_mark_refined(
            job_result_id=job_result_id,
            scope_id=scope_id,
            target=target,
            output=output,
            scope_publisher=publisher,
        )
        if all_refined is None:
            return {
                "status": "aborted",
                "job_result_id": job_result_id,
                "scope_id": scope_id,
            }
        if all_refined:
            _finalize_all_scopes_refined(
                job_result_id=job_result_id,
                target=target,
                storage=storage,
                workspace_dir=workspace_dir,
            )
        return {
            "status": REFINED_ENRICHMENT_STATE,
            "job_result_id": job_result_id,
            "scope_id": scope_id,
            "chunk_count": len(output.chunks),
            "all_refined": all_refined,
        }
    except Exception:
        _mark_scope_failed(
            job_result_id=job_result_id,
            scope_id=scope_id,
            claim_generation=target.claim_generation,
        )
        raise
    finally:
        _flush_parse_token_usage(target.job_id)
        cleanup_token_tracker()
        cleanup_llm_overrides()
        shutil.rmtree(workspace_dir, ignore_errors=True)


def run_single_scope_refinement(
    *,
    scope_id: str,
    pdf_path: str,
    output_dir: str,
    manifest: dict[str, Any],
    page_texts: dict[int, str],
    body_text_by_page: dict[int, str],
    page_features: list[PageFeature],
    page_labels: list[PageLabel],
    page_memory_config: PageMemoryConfig,
    source_file_name: str | None = None,
    summary_use_llm: bool = False,
) -> ScopeRefinementOutput:
    """Run C1-C7 for a single frozen scope and convert its subtree to chunks."""
    entry, scope_index, scope_count = _find_manifest_scope(manifest, scope_id)
    filename = str(source_file_name or manifest["filename"])
    page_count = max(int(manifest.get("page_count") or 0), 0)
    toc_pages = [int(page) for page in (manifest.get("toc_pages") or [])]
    next_title_by_path = next_title_by_path_from_manifest(manifest)

    coarse_scope = CoarseScope(
        scope_id=scope_id,
        skeletons=skeletons_from_rows(list(entry.get("skeletons") or [])),
        strategy=str(entry["strategy"]),
        start_page=int(entry["start_page"]),
        end_page=int(entry["end_page"]),
    )
    vlm_model = os.environ.get("IMAGE_MODEL")

    scope_result = run_single_scope(
        scope=coarse_scope,
        scope_index=scope_index,
        scope_count=scope_count,
        pdf_path=pdf_path,
        filename=filename,
        output_dir=output_dir,
        page_count=page_count,
        page_texts=page_texts,
        page_features=page_features,
        page_labels=page_labels,
        vlm_model=vlm_model,
        asset_extraction_enabled=page_memory_config.asset_extraction_enabled,
        trace_recorder=None,
        page_memory_config=page_memory_config,
        next_title_by_path=next_title_by_path,
        toc_pages=toc_pages,
    )

    collapsed_skeletons = collapse_single_child_chains(
        sort_skeletons(list(scope_result.hierarchy))
    )
    rows = _assemble_scope_rows(
        scope_id=scope_id,
        skeletons=collapsed_skeletons,
        rendered=scope_result.rendered,
        tags=scope_result.tags,
        assets_by_page=scope_result.assets_by_page,
        page_texts=page_texts,
        body_text_by_page=body_text_by_page,
        page_labels=page_labels,
        toc_pages=toc_pages,
        page_count=page_count,
        output_dir=output_dir,
        filename=filename,
        vlm_model=vlm_model,
        page_memory_config=page_memory_config,
        fine_text_by_page=scope_result.fine_text_by_page,
    )
    dataframe = pd.DataFrame(rows, columns=pd.Index(PARSER_DF_COLUMNS))
    chunks = cast(list[dict[str, Any]], dataframe_to_chunks(dataframe))
    section_summaries = _build_scope_section_summaries(
        output_dir=output_dir,
        chunks=chunks,
        source_file_name=filename,
        summary_use_llm=summary_use_llm,
    )
    artifact_refs = collect_referenced_artifact_refs(chunks)
    return ScopeRefinementOutput(
        scope_id=scope_id,
        dataframe=dataframe,
        chunks=chunks,
        section_summaries=section_summaries,
        artifact_refs=artifact_refs,
    )


def _assemble_scope_rows(
    *,
    scope_id: str,
    skeletons: list[SectionSkeleton],
    rendered: list[Any],
    tags: list[Any],
    assets_by_page: dict[int, list[Any]],
    page_texts: dict[int, str],
    body_text_by_page: dict[int, str],
    page_labels: list[PageLabel],
    toc_pages: list[int],
    page_count: int,
    output_dir: str,
    filename: str,
    vlm_model: str | None,
    page_memory_config: PageMemoryConfig,
    fine_text_by_page: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    rows, _final_pages = assemble_c7_node_rows(
        skeletons=skeletons,
        rendered=rendered,
        tags=tags,
        assets_by_page=assets_by_page,
        page_texts=page_texts,
        body_text_by_page=body_text_by_page,
        page_labels=page_labels,
        toc_pages=toc_pages,
        page_count=page_count,
        output_dir=output_dir,
        filename=filename,
        verdict="page",
        vlm_model=vlm_model,
        page_memory_config=page_memory_config,
        fine_text_by_page=fine_text_by_page,
    )
    for row in rows:
        extra = row.get("extra_metadata")
        extra_dict = dict(extra) if isinstance(extra, dict) else {}
        extra_dict["scope_id"] = scope_id
        if str(row.get("type") or "") == "page":
            extra_dict["enrichment_state"] = REFINED_ENRICHMENT_STATE
        row["extra_metadata"] = extra_dict
    return rows


def _build_claimed_target(
    *,
    document: Document,
    job: Job,
    job_metadata: dict[str, Any],
    source_file_name: str,
    claim_generation: str | None = None,
) -> _ClaimedScopeTarget:
    raw_prefix_val = job_metadata.get("result_raw_prefix")
    raw_prefix = (
        str(raw_prefix_val)
        if isinstance(raw_prefix_val, str) and raw_prefix_val
        else None
    )
    corpus_storage = CorpusStorage.resolve_namespace(document.namespace)
    return _ClaimedScopeTarget(
        document_id=str(document.document_id),
        job_id=str(job.job_id),
        user_id=corpus_storage.resolve_owner(str(job.user_id)),
        namespace=normalize_retrieval_namespace(document.namespace),
        source_file_name=source_file_name,
        raw_prefix=raw_prefix,
        job_metadata=job_metadata,
        page_memory_config=JobMetadataHelper.get_page_memory_config(job_metadata),
        claim_generation=claim_generation,
    )


def _structure_claim_expired(
    *,
    job_result: JobResult,
    job: Job,
    now: Any,
) -> bool:
    doc_meta = (
        job_result.document_metadata
        if isinstance(job_result.document_metadata, dict)
        else {}
    )
    job_meta = job.job_metadata if isinstance(job.job_metadata, dict) else {}
    raw_claimed_at = doc_meta.get("structure_claimed_at") or job_meta.get(
        "structure_claimed_at"
    )
    if not raw_claimed_at:
        return True
    try:
        from datetime import datetime, timedelta
        from shared.core.config import app_config

        claimed_at = datetime.fromisoformat(
            str(raw_claimed_at).replace("Z", "+00:00")
        ).replace(tzinfo=None)
        return (now - claimed_at) >= timedelta(
            seconds=app_config.TASK_TIME_LIMIT_SECONDS
        )
    except Exception:
        return True


def _release_structure_claim(
    *, job_id: str, job_result_id: str, claim_generation: str | None = None
) -> None:
    with get_sync_db_context() as db:
        jr = db.execute(
            select(JobResult)
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        job = db.execute(
            select(Job).where(Job.job_id == job_id).with_for_update()
        ).scalar_one_or_none()
        if job is not None and isinstance(job.job_metadata, dict):
            meta = dict(job.job_metadata)
            if (
                str(meta.get("enrichment_status") or "").strip().lower() == "text"
                and meta.get("structure_claim_generation") == claim_generation
            ):
                meta.pop("structure_claimed_at", None)
                job.job_metadata = meta
        if jr is not None and isinstance(jr.document_metadata, dict):
            doc_meta = dict(jr.document_metadata)
            if (
                str(doc_meta.get("enrichment_status") or "").strip().lower() == "text"
                and doc_meta.get("structure_claim_generation") == claim_generation
            ):
                doc_meta.pop("structure_claimed_at", None)
                jr.document_metadata = doc_meta


def _load_and_validate_structure_target(
    *,
    job_result_id: str,
) -> tuple[str, _ClaimedScopeTarget | None]:
    now = utc_now_naive()
    with get_sync_db_context() as db:
        job_result = db.execute(
            select(JobResult)
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if job_result is None:
            return "aborted", None
        doc_id = job_result.document_id or job_result.demo_document_id
        document = (
            db.execute(
                select(Document).where(Document.document_id == str(doc_id))
            ).scalar_one_or_none()
            if doc_id
            else None
        )
        job = db.execute(
            select(Job).where(Job.job_id == job_result.job_id).with_for_update()
        ).scalar_one_or_none()
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
            or job is None
            or not source_file_name
        ):
            return "aborted", None
        enrichment_status = (
            str(
                (job_result.document_metadata or {}).get("enrichment_status")
                or job_metadata.get("enrichment_status")
                or ""
            )
            .strip()
            .lower()
        )
        if enrichment_status != "text":
            return "skipped", None
        if not _structure_claim_expired(job_result=job_result, job=job, now=now):
            return "skipped", None
        claim_generation: str | None = str(uuid4())
        if read_progressive_state(job_result.document_metadata) is not None:
            claim_generation = _claim_progressive_stage(
                job_result_id=job_result_id, stage_name="structure", db=db
            )
            if claim_generation is None:
                return "skipped", None
        claimed_iso = now.isoformat()
        job_metadata["structure_claim_generation"] = claim_generation
        job_metadata["structure_claimed_at"] = claimed_iso
        job.job_metadata = job_metadata
        job.updated_at = now
        job_result.document_metadata = {
            **dict(job_result.document_metadata or {}),
            "structure_claimed_at": claimed_iso,
            "structure_claim_generation": claim_generation,
        }
        return (
            "claimed",
            _build_claimed_target(
                document=document,
                job=job,
                job_metadata=job_metadata,
                source_file_name=source_file_name,
                claim_generation=claim_generation,
            ),
        )


def _load_m0_document_profile(bb_raw: dict[str, Any]) -> Any:
    from app.services.document_agent.manifest import DocumentProfile
    from app.services.document_parser.profiling.domain_taxonomy import OTHER_CODE

    dp_raw = bb_raw.get("document_profile") or {}
    return DocumentProfile(
        is_scanned=bool(dp_raw.get("is_scanned", False)),
        category=list(dp_raw.get("category") or [OTHER_CODE]),
        category_label=str(dp_raw.get("category_label") or "unknown"),
        routing_category=str(dp_raw.get("routing_category") or "generic"),
        language=str(dp_raw.get("language") or "unknown"),
        header_y=dp_raw.get("header_y"),
        footer_y=dp_raw.get("footer_y"),
    )


def _resume_profile_from_m0_workspace(
    *,
    add_dir: str,
    target: _ClaimedScopeTarget,
    job_result_id: str,
) -> Any:
    from app.services.document_agent.coordinator import ProfileCoordinator
    from app.services.document_agent.pdf_text import (
        page_bands_map,
        page_body_content_map,
        page_content_map,
    )
    from app.services.document_agent.tools.find_toc_anchor_pages import (
        _all_scan_keywords,
        _prefix_toc_keywords_for_categories,
    )
    from app.services.document_parser.profiling.doc_profiler import _map_toc_profile
    from app.services.document_parser.profiling.profile_model import (
        ParserDocumentProfile,
    )
    from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
    from shared.core.config import settings
    from shared.services.retrieval.page_embedding_index import (
        query_visual_candidate_pages,
    )

    pdf_path = os.path.join(add_dir, PHASE_B_SOURCE_PDF_REL)
    bb_path = os.path.join(add_dir, PHASE_B_BLACKBOARD_REL)
    coordinator = ProfileCoordinator(
        pdf_path=pdf_path,
        job_id=target.job_id,
        output_dir=os.path.join(add_dir, "_doc_agent"),
        db=None,
        model=settings.IMAGE_MODEL,
        settings={
            "vlm_model": settings.IMAGE_MODEL,
            "toc_profile_enabled": True,
            "skip_toc_anchoring": False,
            "model": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
        },
    )
    bb_raw = _read_json_dict(bb_path)
    bb = coordinator.blackboard
    bb.page_count = int(bb_raw.get("page_count") or 0)
    doc_profile = _load_m0_document_profile(bb_raw)
    bb.document_profile = doc_profile
    coordinator._coarse_profile_cache = doc_profile
    features, labels = _parse_features_and_labels(
        bb_raw.get("page_features") or [],
        bb_raw.get("page_labels") or [],
    )
    bb.page_features = features
    bb.page_labels = labels
    bb.doc_stats = dict(bb_raw.get("doc_stats") or {})
    bb.global_signals = dict(bb_raw.get("global_signals") or {})
    bb.page_full_text_cache = page_bands_map(bb_raw["page_full_text_bands"])
    bb.page_text_search_view = None
    if not bb.page_full_text_cache:
        coordinator._run_text_scan()
    routing_category = PdfRoutingCategory.normalize(doc_profile.routing_category)
    if treat_profile_as_scanned(doc_profile):
        queries = sorted(
            _all_scan_keywords(
                prefix_keywords=_prefix_toc_keywords_for_categories(
                    doc_profile.category
                )
            )
        )
        with get_sync_db_context() as db:
            visual_pages = query_visual_candidate_pages(
                db,
                namespace=target.namespace,
                document_id=target.document_id,
                job_result_id=job_result_id,
                queries=queries,
            )
        if visual_pages:
            coordinator.ctx.settings["toc_visual_candidate_pages"] = sorted(
                visual_pages
            )
    coordinator._ensure_toc_profile(strict=False)

    profile = ParserDocumentProfile(
        file_type="pdf",
        category=doc_profile.category,
        category_label=doc_profile.category_label,
        routing_category=routing_category,
        is_scanned=doc_profile.is_scanned,
        page_count=coordinator.blackboard.page_count,
        language=doc_profile.language,
        metrics={
            "doc_stats": coordinator.blackboard.doc_stats,
            "doc_shape": coordinator.blackboard.global_signals.get("doc_shape", {}),
            "page_kind_counts": coordinator.blackboard.global_signals.get(
                "page_kind_counts", {}
            ),
        },
        page_full_text_cache=page_content_map(
            coordinator.blackboard.page_full_text_cache
        ),
        page_body_text_cache=page_body_content_map(
            coordinator.blackboard.page_full_text_cache
        ),
    )
    if not profile.is_atlas:
        if profile.page_count > settings.MAX_PDF_PAGE_LIMIT:
            profile.anatomy = coordinator.run_structural()
        else:
            profile.anatomy = coordinator.run_lightweight_anatomy()
    profile.toc = _map_toc_profile(coordinator)
    return profile


def _build_publication_scope(
    *,
    target: _ClaimedScopeTarget,
    job_result_id: str,
) -> DocumentPublicationScope:
    return DocumentPublicationScope(
        user_id=target.user_id,
        namespace=target.namespace,
        document_id=target.document_id,
        job_result_id=job_result_id,
        source_file_name=target.source_file_name,
    )


def _publish_coarse_structure_and_write_scopes(
    *,
    job_result_id: str,
    target: _ClaimedScopeTarget,
    publication_scope: DocumentPublicationScope,
    chunks: list[dict[str, Any]],
    section_summaries: dict[str, str],
    phase_b_refs: list[str],
    scope_publisher: ScopePublisher,
    phase_b_artifacts: dict[str, str] | None = None,
) -> bool:
    with get_sync_db_context() as db:
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
            return False
        job_result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if (
            job_result is None
            or (job_result.document_metadata or {}).get("structure_claim_generation")
            != target.claim_generation
        ):
            return False
        state = read_progressive_state(job_result.document_metadata)
        if state is not None:
            stage: EnhancementStage = state.stages.get("structure", {})
            if (
                stage.get("status") != "running"
                or stage.get("claim_generation") != target.claim_generation
                or state.enhancement.status == "failed"
            ):
                return False
        scope_publisher(
            db,
            scope=publication_scope,
            scope_id="*",
            chunks=chunks,
            section_summaries=section_summaries,
        )
        write_coarse_scope_enrichments(
            db,
            document_id=target.document_id,
            job_result_id=job_result_id,
        )
        job_result.document_metadata = {
            **dict(job_result.document_metadata or {}),
            "enrichment_status": "coarse",
            "page_memory_phase_b": phase_b_refs,
            "page_memory_phase_b_artifacts": dict(phase_b_artifacts or {}),
        }
        state = read_progressive_state(job_result.document_metadata)
        if state is not None:
            stages: dict[str, EnhancementStage] = dict(state.stages)
            stages["structure"] = {**stages.get("structure", {}), "status": "complete"}
            vector_repair_stage: EnhancementStage = {
                **(stages.get("vector_repair") or {}),
            }
            pending_pages = list(vector_repair_stage.get("pending_pages") or [])
            if pending_pages:
                corpus_storage: CorpusStorage = CorpusStorage.resolve_namespace(
                    target.namespace
                )
                persisted_pages = set(
                    db.scalars(
                        select(corpus_storage.DocumentPageEmbedding.page_num)
                        .where(
                            corpus_storage.DocumentPageEmbedding.document_id
                            == target.document_id
                        )
                        .where(
                            corpus_storage.DocumentPageEmbedding.job_result_id
                            == job_result_id
                        )
                    ).all()
                )
                remaining_pages = [
                    page for page in pending_pages if page not in persisted_pages
                ]
                vector_repair_stage["pending_pages"] = remaining_pages
                if not remaining_pages:
                    vector_repair_stage["status"] = "complete"
                    vector_repair_stage["error"] = None
                stages["vector_repair"] = vector_repair_stage
            state.stages = stages
            job_result.document_metadata = write_progressive_state(
                job_result.document_metadata, state
            )
        job = db.execute(
            select(Job).where(Job.job_id == target.job_id).with_for_update()
        ).scalar_one()
        job.job_metadata = {
            **dict(job.job_metadata or {}),
            "enrichment_status": "coarse",
            "page_memory_phase_b": phase_b_refs,
            "page_memory_phase_b_artifacts": dict(phase_b_artifacts or {}),
        }

    return True


def _generate_and_persist_page_embeddings(
    *,
    add_dir: str,
    workspace_dir: str,
    publication_scope: DocumentPublicationScope,
) -> int:
    """Render and embed pages for electronic documents before coarse structure build."""
    from app.services.document_agent.pdf_text import (
        page_bands_map,
        page_content_map,
    )
    from app.services.page_memory.page_renderer import render_document_pages
    from shared.core.config import settings
    from shared.services.retrieval.cache_service import (
        bump_retrieval_namespace_cache_version_sync,
    )
    from shared.services.retrieval.page_embedding_index import (
        replace_document_page_embeddings,
    )

    if not bool(getattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)):
        return 0

    try:
        bb_raw = _read_json_dict(os.path.join(add_dir, PHASE_B_BLACKBOARD_REL))
        doc_profile = _load_m0_document_profile(bb_raw)
        if treat_profile_as_scanned(doc_profile):
            return 0
        page_count = max(int(bb_raw.get("page_count") or 0), 0)
        if page_count <= 0:
            return 0
        pdf_path = os.path.join(add_dir, PHASE_B_SOURCE_PDF_REL)
        pages = list(range(1, page_count + 1))
        full_text_path = os.path.join(add_dir, PHASE_B_FULL_TEXT_REL)
        if os.path.isfile(full_text_path):
            page_texts = coerce_page_text_cache(_read_json_dict(full_text_path))
        else:
            page_texts = page_content_map(
                page_bands_map(bb_raw.get("page_full_text_bands") or {})
            )
        rendered = render_document_pages(
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=workspace_dir,
            scope_id="m0_defer",
            pages=pages,
            page_texts=page_texts,
        )
        image_path_by_page = {
            int(item.page_index): str(item.image_path)
            for item in rendered
            if item.image_path
        }
        embeddings_by_page = embed_rendered_pages(
            image_path_by_page=image_path_by_page,
            pages=pages,
            filename=publication_scope.source_file_name,
        )
        if not embeddings_by_page:
            return 0
        with get_sync_db_context() as db:
            document = db.execute(
                select(Document)
                .where(Document.document_id == publication_scope.document_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                document is None
                or document.status != "active"
                or document.current_job_result_id
                != publication_scope.job_result_id
            ):
                return 0
            inserted = replace_document_page_embeddings(
                db,
                scope=publication_scope,
                incoming_embeddings_by_page=embeddings_by_page,
            )
        if inserted:
            bump_retrieval_namespace_cache_version_sync(
                user_id=publication_scope.user_id,
                namespace=publication_scope.namespace,
            )
        return inserted
    except Exception as exc:
        logger.warning(
            "[page_memory] failed to generate deferred page embeddings "
            "job_result_id={}: {}",
            publication_scope.job_result_id,
            exc,
        )
        return 0


def _claim_and_validate_scope(
    *,
    job_result_id: str,
    scope_id: str,
) -> tuple[str, _ClaimedScopeTarget | None]:
    now = utc_now_naive()
    claim_generation = str(uuid4())
    with get_sync_db_context() as db:
        claimed_doc_id = db.execute(
            update(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == job_result_id)
            .where(DocumentScopeEnrichment.scope_id == scope_id)
            .where(scope_claimable_filter(now))
            .values(
                state="running",
                claim_generation=claim_generation,
                attempts=DocumentScopeEnrichment.attempts + 1,
                updated_at=now,
            )
            .returning(DocumentScopeEnrichment.document_id)
        ).scalar_one_or_none()
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

        return (
            "claimed",
            _build_claimed_target(
                document=document,
                job=job,
                job_metadata=job_metadata,
                source_file_name=source_file_name,
                claim_generation=claim_generation,
            ),
        )


def _run_scope_in_workspace(
    *,
    workspace_dir: str,
    target: _ClaimedScopeTarget,
    scope_id: str,
    storage: ResultStorage,
) -> ScopeRefinementOutput:
    add_dir = os.path.join(
        workspace_dir,
        Path(target.source_file_name).stem or "document",
    )
    os.makedirs(add_dir, exist_ok=True)
    _download_phase_b_inputs(
        storage=storage,
        job_id=target.job_id,
        raw_prefix=target.raw_prefix,
        job_metadata=target.job_metadata,
        output_dir=add_dir,
    )
    manifest = _read_json_dict(os.path.join(add_dir, PHASE_B_MANIFEST_REL))
    page_texts = coerce_page_text_cache(
        _read_json_dict(os.path.join(add_dir, PHASE_B_FULL_TEXT_REL))
    )
    body_text_by_page = coerce_page_text_cache(
        _read_json_dict(os.path.join(add_dir, PHASE_B_BODY_TEXT_REL))
    )
    page_features, page_labels = _load_anatomy_features_and_labels(
        os.path.join(add_dir, PHASE_B_ANATOMY_REL)
    )
    summary_use_llm = bool(
        JobMetadataHelper.get_parsing_param(
            target.job_metadata,
            "summary_use_llm",
            False,
        )
    )
    return run_single_scope_refinement(
        scope_id=scope_id,
        pdf_path=os.path.join(add_dir, PHASE_B_SOURCE_PDF_REL),
        output_dir=add_dir,
        manifest=manifest,
        page_texts=page_texts,
        body_text_by_page=body_text_by_page,
        page_features=page_features,
        page_labels=page_labels,
        page_memory_config=target.page_memory_config,
        source_file_name=target.source_file_name,
        summary_use_llm=summary_use_llm,
    )


def _download_phase_b_inputs(
    *,
    storage: ResultStorage,
    job_id: str,
    raw_prefix: str | None,
    job_metadata: dict[str, Any],
    output_dir: str,
) -> None:
    configured_refs = job_metadata.get("page_memory_phase_b")
    if not isinstance(configured_refs, list) or not configured_refs:
        raise ValueError("job metadata is missing the page_memory_phase_b input list")

    raw_mapping: object = job_metadata.get("page_memory_phase_b_artifacts")
    artifact_mapping: dict[str, str] = (
        {str(key): str(value) for key, value in raw_mapping.items()}
        if isinstance(raw_mapping, dict)
        else {}
    )
    for relative_path in (str(item) for item in configured_refs):
        suffix = Path(relative_path).suffix or ".tmp"
        temp_path = storage.download_raw_to_temp(
            job_id=job_id,
            relative_path=artifact_mapping.get(relative_path, relative_path),
            raw_prefix=raw_prefix,
            suffix=suffix,
            temp_dir=output_dir,
        )
        target_path = Path(output_dir) / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        Path(temp_path).replace(target_path)


def _upload_scope_artifacts(
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


def _publish_and_mark_refined(
    *,
    job_result_id: str,
    scope_id: str,
    target: _ClaimedScopeTarget,
    output: ScopeRefinementOutput,
    scope_publisher: ScopePublisher,
) -> bool | None:
    now = utc_now_naive()
    publication_scope = _build_publication_scope(
        target=target,
        job_result_id=job_result_id,
    )
    with get_sync_db_context() as db:
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
            scope=publication_scope,
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


def _finalize_all_scopes_refined(
    *,
    job_result_id: str,
    target: _ClaimedScopeTarget,
    storage: ResultStorage,
    workspace_dir: str,
) -> None:
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
            if (
                stages.get("structure", {}).get("status") != "complete"
                or stages.get("vector_repair", {}).get("status") != "complete"
            ):
                return
    token = (
        _claim_progressive_stage(job_result_id=job_result_id, stage_name="finalization")
        if state is not None
        else None
    )
    if state is not None and token is None:
        return
    try:
        snapshot, zip_size = _regenerate_and_upload_full_package(
            job_result_id=job_result_id,
            target=target,
            storage=storage,
            workspace_dir=workspace_dir,
            snapshot_id=f"snapshot_{uuid4().hex}",
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
            _finish_progressive_stage(
                job_result_id=job_result_id,
                stage_name="finalization",
                claim_generation=token,
                error="Final package upload failed",
            )
        logger.warning(
            "[page_memory] final package incomplete for {}: {}", job_result_id, exc
        )


def _regenerate_and_upload_full_package(
    *,
    job_result_id: str,
    target: _ClaimedScopeTarget,
    storage: ResultStorage,
    workspace_dir: str,
    snapshot_id: str,
) -> tuple[StoredSnapshot | None, int]:
    chunks = _load_published_package_chunks(
        job_result_id=job_result_id, target=target
    )
    if not chunks:
        return None, 0

    package_dir = os.path.join(workspace_dir, "_full_package", target.source_file_name)
    os.makedirs(package_dir, exist_ok=True)
    required_refs: set[str] = collect_referenced_artifact_refs(chunks) | {
        PHASE_B_SOURCE_PDF_REL
    }
    for relative_path in sorted(required_refs):
        target_path = Path(package_dir) / relative_path
        if target_path.is_file():
            continue
        suffix = Path(relative_path).suffix or ".tmp"
        temp_path = storage.download_raw_to_temp(
            job_id=target.job_id,
            relative_path=relative_path,
            raw_prefix=target.raw_prefix,
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


def _load_published_package_chunks(
    *,
    job_result_id: str,
    target: _ClaimedScopeTarget,
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


def _mark_scope_failed(
    *, job_result_id: str, scope_id: str, claim_generation: str | None
) -> None:
    now = utc_now_naive()
    with get_sync_db_context() as db:
        db.execute(
            update(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == job_result_id)
            .where(DocumentScopeEnrichment.scope_id == scope_id)
            .where(DocumentScopeEnrichment.claim_generation == claim_generation)
            .where(DocumentScopeEnrichment.state == "running")
            .values(state="failed", updated_at=now)
        )


def _build_scope_section_summaries(
    *,
    output_dir: str,
    chunks: list[dict[str, Any]],
    source_file_name: str,
    summary_use_llm: bool,
) -> dict[str, str]:
    if not chunks:
        return {}
    nav_dir = os.path.join(output_dir, "_scope_nav", source_file_name)
    os.makedirs(nav_dir, exist_ok=True)
    ensure_doc_nav_json(
        nav_dir,
        chunks,
        source_file_name=source_file_name,
        overwrite=True,
    )
    enrich_doc_nav_summaries(
        os.path.dirname(nav_dir),
        source_file=source_file_name,
        force=True,
        use_llm=summary_use_llm,
        top_summary_use_llm=False,
        chunks=chunks,
    )
    lookup = build_section_summary_lookup(nav_dir)
    lookup.pop("Root", None)
    return lookup


def _find_manifest_scope(
    manifest: dict[str, Any],
    scope_id: str,
) -> tuple[dict[str, Any], int, int]:
    scopes = [
        entry for entry in (manifest.get("scopes") or []) if isinstance(entry, dict)
    ]
    for index, entry in enumerate(scopes, start=1):
        if str(entry.get("scope_id") or "") == scope_id:
            return entry, index, len(scopes)
    raise ValueError(f"scope_id {scope_id!r} not found in phase A manifest")


def _load_anatomy_features_and_labels(
    anatomy_path: str,
) -> tuple[list[PageFeature], list[PageLabel]]:
    raw = _read_optional_json(anatomy_path)
    if not isinstance(raw, dict):
        return [], []
    return _parse_features_and_labels(
        raw.get("page_features") or [],
        raw.get("page_labels") or [],
    )


def _parse_features_and_labels(
    raw_features: list[Any],
    raw_labels: list[Any],
) -> tuple[list[PageFeature], list[PageLabel]]:
    features: list[PageFeature] = []
    for item in raw_features:
        if not isinstance(item, dict):
            continue
        orientation = item.get("orientation")
        features.append(
            PageFeature(
                page=int(item.get("page") or 0),
                raw_text_length=int(item.get("raw_text_length") or 0),
                text_density=float(item.get("text_density") or 0.0),
                image_coverage=float(item.get("image_coverage") or 0.0),
                image_count=int(item.get("image_count") or 0),
                table_count=int(item.get("table_count") or 0),
                drawings_count=int(item.get("drawings_count") or 0),
                orientation="landscape" if orientation == "landscape" else "portrait",
                width=float(item.get("width") or 0.0),
                height=float(item.get("height") or 0.0),
                has_asset=bool(item.get("has_asset", False)),
                is_blank_like=bool(item.get("is_blank_like", False)),
                invisible_text_length=int(item.get("invisible_text_length") or 0),
            )
        )
    labels: list[PageLabel] = []
    for item in raw_labels:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        labels.append(
            PageLabel(
                page=int(item.get("page") or 0),
                kind="landscape" if kind == "landscape" else "normal",
                evidence=dict(item.get("evidence") or {}),
            )
        )
    return features, labels


def _read_json_dict(path: str) -> dict[str, Any]:
    data = _read_optional_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _read_optional_json(path: str) -> Any:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_default_scope_publisher() -> ScopePublisher:
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )

    return replace_document_scope_content


def _claim_progressive_stage(
    *, job_result_id: str, stage_name: str, db: Session | None = None
) -> str | None:
    now: datetime = utc_now_naive()
    with nullcontext(db) if db is not None else get_sync_db_context() as db:
        result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if result is None:
            return None
        state = read_progressive_state(result.document_metadata)
        if state is None or state.enhancement.status in ("complete", "failed"):
            return None
        stages: dict[str, EnhancementStage] = dict(state.stages)
        stage: EnhancementStage = {**stages.get(stage_name, {})}
        if stage.get("status") == "complete":
            return None
        if stage.get("status") == "running" and stage.get("claimed_at"):
            claimed_at = datetime.fromisoformat(str(stage.get("claimed_at"))).replace(
                tzinfo=None
            )
            if now - claimed_at < timedelta(seconds=app_config.TASK_TIME_LIMIT_SECONDS):
                return None
        attempts: int = int(stage.get("attempts", 0))
        if attempts >= 3:
            stages[stage_name] = {**stage, "status": "failed", "claim_generation": None}
            state.stages = stages
            state.enhancement = EnhancementProgress(
                status="failed",
                can_retry=True,
                attempt=state.enhancement.attempt,
                error="Background enhancement exhausted its automatic retries",
            )
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            return None
        token: str = str(uuid4())
        stages[stage_name] = {
            **stage,
            "status": "running",
            "attempts": attempts + 1,
            "claim_generation": token,
            "claimed_at": now.isoformat(),
            "error": None,
        }
        state.stages = stages
        state.enhancement = EnhancementProgress(
            status="running", attempt=state.enhancement.attempt
        )
        result.document_metadata = write_progressive_state(
            result.document_metadata, state
        )
        return token


def _finish_progressive_stage(
    *,
    job_result_id: str,
    stage_name: str,
    claim_generation: str,
    error: str | None,
    pending_pages: list[int] | None = None,
) -> bool:
    with get_sync_db_context() as db:
        result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if result is None:
            return False
        state = read_progressive_state(result.document_metadata)
        if state is None:
            return False
        stages: dict[str, EnhancementStage] = dict(state.stages)
        stage: EnhancementStage = {**stages.get(stage_name, {})}
        if (
            stage.get("claim_generation") != claim_generation
            or stage.get("status") != "running"
        ):
            return False
        stage["status"] = "failed" if error else "complete"
        stage["error"] = error
        if pending_pages is not None:
            stage["pending_pages"] = pending_pages
        stages[stage_name] = stage
        state.stages = stages
        if error and int(stage.get("attempts", 0)) >= 3:
            state.enhancement = EnhancementProgress(
                status="failed",
                can_retry=True,
                attempt=state.enhancement.attempt,
                error="Background enhancement exhausted its automatic retries",
            )
        result.document_metadata = write_progressive_state(
            result.document_metadata, state
        )
        return True


def execute_progressive_recovery(
    *,
    job_result_id: str,
    result_storage_factory: Callable[[], ResultStorage] = get_result_storage,
) -> dict[str, Any]:
    """Recover persisted stages without replaying parsing or completed scope work."""
    with get_sync_db_context() as db:
        document_id: str | None = db.scalar(
            select(JobResult.document_id).where(JobResult.id == job_result_id)
        )
        if document_id is None:
            return {"status": "aborted"}
        document = db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .with_for_update()
        ).scalar_one_or_none()
        result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if result is None:
            return {"status": "aborted"}
        state = read_progressive_state(result.document_metadata)
        job = db.get(Job, result.job_id)
        if (
            state is None
            or state.enhancement.status in ("failed", "complete")
            or document is None
            or job is None
            or document.status != "active"
            or document.current_job_result_id != job_result_id
        ):
            return {"status": "skipped"}
        target = _build_claimed_target(
            document=document,
            job=job,
            job_metadata=dict(job.job_metadata or {}),
            source_file_name=str(document.source_file_name),
        )
        stages: dict[str, EnhancementStage] = dict(state.stages)
        scope_states = db.execute(
            select(
                DocumentScopeEnrichment.state, DocumentScopeEnrichment.attempts
            ).where(DocumentScopeEnrichment.job_result_id == job_result_id)
        ).all()
        if any(
            status == "failed" and attempts >= 3 for status, attempts in scope_states
        ):
            state.enhancement = EnhancementProgress(
                status="failed",
                can_retry=True,
                attempt=state.enhancement.attempt,
                error="Scope enhancement exhausted its automatic retries",
            )
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            return {"status": "failed"}
    if stages.get("structure", {}).get("status") != "complete":
        from shared.services.jobs.lifecycle.scope_enrichments import (
            enqueue_page_memory_structure_after_commit,
        )

        enqueue_page_memory_structure_after_commit(job_result_id)
    workspace: str = tempfile.mkdtemp(prefix="pm_recovery_")
    storage = result_storage_factory()
    try:
        if stages.get("vector_repair", {}).get("status") != "complete":
            _repair_missing_page_embeddings(
                job_result_id=job_result_id,
                target=target,
                storage=storage,
                workspace_dir=workspace,
            )
        _finalize_all_scopes_refined(
            job_result_id=job_result_id,
            target=target,
            storage=storage,
            workspace_dir=workspace,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return {"status": "recovered", "job_result_id": job_result_id}


def _repair_missing_page_embeddings(
    *,
    job_result_id: str,
    target: _ClaimedScopeTarget,
    storage: ResultStorage,
    workspace_dir: str,
) -> None:
    from app.services.page_memory.page_renderer import render_document_pages
    from shared.services.ai.embedding import embed_images
    from shared.services.retrieval.page_embedding_index import (
        upsert_document_page_embeddings,
    )
    from shared.services.retrieval.publication_scope_content import (
        _invalidate_namespace_after_commit,
    )

    token = _claim_progressive_stage(
        job_result_id=job_result_id, stage_name="vector_repair"
    )
    if token is None:
        return
    pending: list[int] = []
    try:
        with get_sync_db_context() as db:
            result = db.get(JobResult, job_result_id)
            if result is None:
                return
            state = read_progressive_state(result.document_metadata)
            if state is None:
                return
            pending = sorted(
                set(state.stages["vector_repair"].get("pending_pages", []))
            )
        if not pending:
            _finish_progressive_stage(
                job_result_id=job_result_id,
                stage_name="vector_repair",
                claim_generation=token,
                error=None,
                pending_pages=[],
            )
            return
        source = storage.download_raw_to_temp(
            job_id=target.job_id,
            relative_path=PHASE_B_SOURCE_PDF_REL,
            raw_prefix=target.raw_prefix,
            suffix=".pdf",
            temp_dir=workspace_dir,
        )
        rendered = render_document_pages(
            pdf_path=source,
            page_count=max(pending),
            output_dir=workspace_dir,
            scope_id="vector_repair",
            pages=pending,
            page_texts={},
        )
        paths: dict[int, str] = {
            int(page.page_index): page.image_path
            for page in rendered
            if page.image_path
        }
        pages: list[int] = [page for page in pending if page in paths]
        vectors = embed_images(
            [paths[page] for page in pages], usage_task="page_memory.embedding_repair"
        )
        repaired: dict[int, list[float]] = {
            page: vector for page, vector in zip(pages, vectors, strict=True) if vector
        }
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
            ).scalar_one()
            state = read_progressive_state(result.document_metadata)
            if state is None:
                return
            stages: dict[str, EnhancementStage] = dict(state.stages)
            stage: EnhancementStage = {**stages["vector_repair"]}
            if (
                document is None
                or document.current_job_result_id != job_result_id
                or document.status != "active"
                or stage.get("claim_generation") != token
                or stage.get("status") != "running"
            ):
                return
            upsert_document_page_embeddings(
                db,
                scope=DocumentPublicationScope(
                    user_id=target.user_id,
                    namespace=target.namespace,
                    document_id=target.document_id,
                    job_result_id=job_result_id,
                    source_file_name=target.source_file_name,
                ),
                incoming_embeddings_by_page=repaired,
            )
            if repaired:
                _invalidate_namespace_after_commit(
                    db, user_id=target.user_id, namespace=target.namespace
                )
            corpus_storage: CorpusStorage = CorpusStorage.resolve_namespace(
                target.namespace
            )
            persisted_pages = set(
                db.scalars(
                    select(corpus_storage.DocumentPageEmbedding.page_num)
                    .where(
                        corpus_storage.DocumentPageEmbedding.document_id
                        == target.document_id
                    )
                    .where(
                        corpus_storage.DocumentPageEmbedding.job_result_id
                        == job_result_id
                    )
                ).all()
            )
            pending = [page for page in pending if page not in persisted_pages]
            stage["pending_pages"] = pending
            stages["vector_repair"] = stage
            state.stages = stages
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
        _finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="vector_repair",
            claim_generation=token,
            error="Page embedding provider left missing coverage" if pending else None,
            pending_pages=pending,
        )
    except Exception as exc:
        _finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="vector_repair",
            claim_generation=token,
            error="Page embedding repair failed",
            pending_pages=pending,
        )
        logger.warning(
            "[page_memory] embedding repair incomplete for {}: {}", job_result_id, exc
        )


def _freeze_scope_artifacts(
    *, output: ScopeRefinementOutput, workspace_dir: str, claim_generation: str
) -> ScopeRefinementOutput:
    """Give one claim exclusive asset keys before any remote write.

    Fencing only the SQL publication cannot protect objects uploaded earlier.
    Claim-prefixed filenames make a late upload harmless while preserving the
    existing flat client asset directory contract.
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
