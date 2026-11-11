"""Phase B per-scope enrichment service for page-memory documents."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5
from typing import Any, Callable, cast

import pandas as pd
from loguru import logger
from sqlalchemy import select
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
from app.services.document_ingestion.scope_refinement import (
    REFINED_ENRICHMENT_STATE,
    claim_scope_revision,
    flush_scope_token_usage,
    PackageRefs,
    ScopePublisher,
    ScopeRefinementOutput,
    build_publication_scope,
    finalize_all_scopes_refined,
    freeze_scope_artifacts,
    publish_and_mark_refined,
    resolve_default_scope_publisher,
    upload_scope_artifacts,
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
from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import (
    Document,
    DocumentScopeEnrichment,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    EnhancementStage,
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
    init_token_tracker,
)
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.jobs.lifecycle.scope_enrichments import (
    mark_scope_enrichment_failed,
    write_coarse_scope_enrichments,
)
from shared.services.jobs.progressive_stages import (
    claim_progressive_stage,
    finish_progressive_stage,
)
from shared.services.jobs.lifecycle.directory_ready import capture_initial_directory
from shared.services.jobs.lifecycle.webhook_outbox import (
    SyncJobWebhookOutbox,
    WebhookOutboxEvent,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
from shared.services.storage.result_storage import ResultStorage, get_result_storage
from shared.utils.utc_now import utc_now_naive

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
    serving_raw_prefix: str | None = None


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
    publisher = scope_publisher or resolve_default_scope_publisher()
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
        publication_scope = build_publication_scope(
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
        upload_scope_artifacts(
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
        finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="structure",
            claim_generation=str(target.claim_generation),
            error="Structure enhancement failed",
        )
        raise
    finally:
        flush_scope_token_usage(target.job_id)
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
    publisher = scope_publisher or resolve_default_scope_publisher()
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
        output = freeze_scope_artifacts(
            output=output,
            workspace_dir=workspace_dir,
            claim_generation=str(target.claim_generation),
            raw_prefix=target.raw_prefix,
            serving_raw_prefix=target.serving_raw_prefix,
        )
        upload_scope_artifacts(
            storage=storage,
            job_id=target.job_id,
            raw_prefix=target.raw_prefix,
            workspace_dir=workspace_dir,
            artifact_refs=output.artifact_refs,
        )
        all_refined = publish_and_mark_refined(
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
            finalize_all_scopes_refined(
                job_result_id=job_result_id,
                target=target,
                storage=storage,
                workspace_dir=workspace_dir,
                package_refs=_page_package_refs(target),
            )
        return {
            "status": REFINED_ENRICHMENT_STATE,
            "job_result_id": job_result_id,
            "scope_id": scope_id,
            "chunk_count": len(output.chunks),
            "all_refined": all_refined,
        }
    except Exception:
        mark_scope_enrichment_failed(
            job_result_id=job_result_id,
            scope_id=scope_id,
            claim_generation=target.claim_generation,
        )
        raise
    finally:
        flush_scope_token_usage(target.job_id)
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
    raw_prefix_val: object = job_metadata.get(
        "page_memory_working_raw_prefix"
    ) or job_metadata.get("result_raw_prefix")
    raw_prefix: str | None = (
        str(raw_prefix_val)
        if isinstance(raw_prefix_val, str) and raw_prefix_val
        else None
    )
    serving_prefix_value: object = job_metadata.get(
        "result_serving_raw_prefix"
    ) or job_metadata.get("result_raw_prefix")
    serving_raw_prefix: str | None = (
        serving_prefix_value
        if isinstance(serving_prefix_value, str) and serving_prefix_value
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
        serving_raw_prefix=serving_raw_prefix,
    )


def _page_package_refs(target: _ClaimedScopeTarget) -> PackageRefs:
    return PackageRefs(
        extra_refs=frozenset({PHASE_B_SOURCE_PDF_REL}),
        baseline_refs=frozenset(
            str(reference)
            for reference in target.job_metadata.get("page_memory_baseline_artifact_refs")
            or []
        ),
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
            claim_generation = claim_progressive_stage(
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
    # M0 may contain only native text features. Finish detection before TOC
    # and anatomy consumers, then persist the completed features for fine tasks.
    coordinator._ensure_asset_probe()
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
    if not profile.is_atlas and profile.page_count > settings.MAX_PDF_PAGE_LIMIT:
        profile.anatomy = coordinator.run_structural()
    else:
        profile.anatomy = coordinator.run_lightweight_anatomy()
    profile.toc = _map_toc_profile(coordinator)
    return profile


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
    from shared.services.retrieval.page_embedding_index import (
        load_missing_document_page_embedding_pages,
    )

    outbox: SyncJobWebhookOutbox = SyncJobWebhookOutbox()

    def publish(
        db: Session, scope: DocumentPublicationScope,
    ) -> tuple[bool, WebhookOutboxEvent | None]:
        document = db.execute(
            select(Document)
            .where(Document.document_id == target.document_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            document is None
            or document.status != "active"
            or document.current_job_result_id != job_result_id
            or document.namespace != target.namespace
            or document.user_id != target.user_id
        ):
            return False, None
        job_result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if (
            job_result is None
            or job_result.document_id != target.document_id
            or job_result.job_id != target.job_id
            or (job_result.document_metadata or {}).get("structure_claim_generation")
            != target.claim_generation
        ):
            return False, None
        if (job_result.document_metadata or {}).get("initial_directory") is not None:
            return True, None
        state = read_progressive_state(job_result.document_metadata)
        if state is not None:
            stage: EnhancementStage = state.stages.get("structure", {})
            if (
                stage.get("status") != "running"
                or stage.get("claim_generation") != target.claim_generation
                or state.enhancement.status == "failed"
            ):
                return False, None
        scope_publisher(
            db,
            scope=scope,
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
            remaining_pages = load_missing_document_page_embedding_pages(
                db, scope=scope
            )
            vector_repair_stage["pending_pages"] = remaining_pages
            if not remaining_pages:
                vector_repair_stage["status"] = "complete"
                vector_repair_stage["error"] = None
            elif vector_repair_stage.get("status") == "complete":
                vector_repair_stage["status"] = "pending"
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
        directory = capture_initial_directory(
            db, scope=scope, job_id=target.job_id
        )
        event_id: str = str(
            uuid5(
                NAMESPACE_URL,
                json.dumps(
                    [
                        "job.document.directory_ready",
                        target.user_id,
                        target.namespace,
                        target.document_id,
                        job_result_id,
                    ]
                ),
            )
        )
        job_result.document_metadata = {
            **dict(job_result.document_metadata or {}),
            "initial_directory": directory.model_dump(mode="json"),
        }
        directory_event: WebhookOutboxEvent | None = outbox.create_event(
            db,
            job_id=target.job_id,
            event_type="job.document.directory_ready",
            event_id=event_id,
            extra_payload={
                **directory.model_dump(mode="json", exclude={"sections"}),
                "event_id": event_id,
                "completed_at": directory.directory_committed_at,
            },
        )
        return True, directory_event

    published, directory_event = ProgressivePublication.publishForServingBackend(
        scope=publication_scope, update=publish, openDatabase=get_sync_db_context,
    )
    outbox.enqueue_after_commit(directory_event)
    return published


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
                or document.current_job_result_id != publication_scope.job_result_id
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
    return claim_scope_revision(
        job_result_id=job_result_id,
        scope_id=scope_id,
        build_target=lambda claimed: _build_claimed_target(
            document=claimed.document,
            job=claimed.job,
            job_metadata=claimed.job_metadata,
            source_file_name=claimed.source_file_name,
            claim_generation=claimed.claim_generation,
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
                image_coverage=(
                    float(item["image_coverage"])
                    if item.get("image_coverage") is not None
                    else None
                ),
                image_count=(
                    int(item["image_count"])
                    if item.get("image_count") is not None
                    else None
                ),
                table_count=(
                    int(item["table_count"])
                    if item.get("table_count") is not None
                    else None
                ),
                drawings_count=(
                    int(item["drawings_count"])
                    if item.get("drawings_count") is not None
                    else None
                ),
                orientation="landscape" if orientation == "landscape" else "portrait",
                width=float(item.get("width") or 0.0),
                height=float(item.get("height") or 0.0),
                has_asset=(
                    bool(item["has_asset"])
                    if item.get("has_asset") is not None
                    else None
                ),
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
        parse_track = str(
            JobMetadataHelper.get_parse_track(job.job_metadata or {})
            or document.parse_track
            or ""
        )
        stages: dict[str, EnhancementStage] = dict(state.stages)
        if parse_track == "chunk":
            if stages.get("finalization", {}).get("status") != "complete":
                from shared.services.jobs.lifecycle.scope_enrichments import (
                    enqueue_chunk_final_after_commit,
                )

                enqueue_chunk_final_after_commit(job_result_id)
            return {"status": "recovered", "job_result_id": job_result_id}
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
        if parse_track == "sheet":
            from app.services.document_ingestion.sheet_enrichment_service import (
                SHEET_PACKAGE_REFS,
                build_sheet_revision_target,
            )

            sheet_target = build_sheet_revision_target(
                document_id=str(document.document_id),
                document_namespace=document.namespace,
                job_id=str(job.job_id),
                job_user_id=str(job.user_id),
                job_metadata=dict(job.job_metadata or {}),
                source_file_name=str(document.source_file_name),
                claim_generation=None,
            )
        else:
            target = _build_claimed_target(
                document=document,
                job=job,
                job_metadata=dict(job.job_metadata or {}),
                source_file_name=str(document.source_file_name),
            )
    if parse_track == "sheet":
        # Sheet groups refine on demand; recovery only retries finalization.
        workspace_dir: str = tempfile.mkdtemp(prefix="sheet_recovery_")
        try:
            finalize_all_scopes_refined(
                job_result_id=job_result_id,
                target=sheet_target,
                storage=result_storage_factory(),
                workspace_dir=workspace_dir,
                package_refs=SHEET_PACKAGE_REFS,
            )
        finally:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        return {"status": "recovered", "job_result_id": job_result_id}
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
        finalize_all_scopes_refined(
            job_result_id=job_result_id,
            target=target,
            storage=storage,
            workspace_dir=workspace,
            package_refs=_page_package_refs(target),
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
        load_missing_document_page_embedding_pages,
        upsert_document_page_embeddings,
    )
    from shared.services.retrieval.publication_scope_content import (
        _invalidate_namespace_after_commit,
    )

    token = claim_progressive_stage(
        job_result_id=job_result_id, stage_name="vector_repair"
    )
    if token is None:
        return
    pending: list[int] = []
    publication_scope = build_publication_scope(
        target=target, job_result_id=job_result_id
    )
    try:
        with get_sync_db_context() as db:
            result = db.get(JobResult, job_result_id)
            if result is None:
                return
            state = read_progressive_state(result.document_metadata)
            if state is None:
                return
            pending = load_missing_document_page_embedding_pages(
                db, scope=publication_scope
            )
        if not pending:
            finish_progressive_stage(
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
                or document.user_id != target.user_id
                or document.namespace != target.namespace
                or document.current_job_result_id != job_result_id
                or document.status != "active"
                or stage.get("claim_generation") != token
                or stage.get("status") != "running"
            ):
                return
            upsert_document_page_embeddings(
                db,
                scope=publication_scope,
                incoming_embeddings_by_page=repaired,
            )
            if repaired:
                _invalidate_namespace_after_commit(
                    db, user_id=target.user_id, namespace=target.namespace
                )
            pending = load_missing_document_page_embedding_pages(
                db, scope=publication_scope
            )
            stage["pending_pages"] = pending
            stages["vector_repair"] = stage
            state.stages = stages
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
        finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="vector_repair",
            claim_generation=token,
            error="Page embedding provider left missing coverage" if pending else None,
            pending_pages=pending,
        )
    except Exception as exc:
        finish_progressive_stage(
            job_result_id=job_result_id,
            stage_name="vector_repair",
            claim_generation=token,
            error="Page embedding repair failed",
            pending_pages=pending,
        )
        logger.warning(
            "[page_memory] embedding repair attempt failed for {}: {}",
            job_result_id,
            exc,
        )

