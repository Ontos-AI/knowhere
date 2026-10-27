#!/usr/bin/env python3
# ruff: noqa: E402
"""Debug the production two-level progressive ingestion path.

Calls the same functions the worker uses. Does not reimplement profiling,
Phase A, or per-scope C1-C7.

  M0      memory_service.run(ingestion_mode="progressive")
  coarse  _resume_profile_from_m0_workspace + build_phase_a + persist_phase_b_inputs
  fine    run_single_scope_refinement

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_progressive.py --file /path/doc.pdf --step m0
  uv run python scripts/page_memory/debug_pm_progressive.py --file /path/doc.pdf --step coarse
  uv run python scripts/page_memory/debug_pm_progressive.py --file /path/doc.pdf --step coarse --list-scopes
  uv run python scripts/page_memory/debug_pm_progressive.py --file /path/doc.pdf --step fine --list-scopes
  uv run python scripts/page_memory/debug_pm_progressive.py --file /path/doc.pdf --step fine --scope-id p14-23
  uv run python scripts/page_memory/debug_pm_progressive.py --file /path/doc.pdf --step fine --all-scopes
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import time
from typing import Any, Literal

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    add_scope_selection_args,
    base_argparser,
    require_file,
    resolve_paths,
    stop_with_trace,
    update_pipeline_state,
    pipeline_state_path,
    record_stage,
)

ProgressiveStep = Literal["m0", "coarse", "fine"]
_DEBUG_STATE_NAME = "progressive_debug_state.json"


def main() -> int:
    parser = base_argparser(
        "Progressive ingestion debug: M0 text → coarse structure → per-scope fine"
    )
    parser.add_argument(
        "--step",
        choices=("m0", "coarse", "fine"),
        required=True,
        help="Which production progressive step to run",
    )
    add_scope_selection_args(parser)
    parser.add_argument(
        "--run-db",
        action="store_true",
        help="Publish this step to the local debug DB (same revision across steps)",
    )
    parser.add_argument(
        "--publish-job-id",
        default=None,
        help="Explicit job_id for --run-db (M0 creates it; later steps reuse it)",
    )
    parser.add_argument(
        "--skip-assets",
        action="store_true",
        help="With --run-db, skip asset upload",
    )
    parser.add_argument(
        "--fine-text-extractor",
        choices=("none", "vlm", "ocr", "mineru"),
        default="none",
        help="Fine-stage verbatim page-text extractor (default none; advanced: vlm|ocr|mineru)",
    )
    args = parser.parse_args()

    pdf_path, filename, out_dir = resolve_paths(args)
    logger.info("█" * 70)
    logger.info(f"  PROGRESSIVE {args.step.upper()} — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict[str, Any]] = []
    token_cost_tracker = TokenCostTracker()
    extra: dict[str, Any]
    restore_models = _apply_model_overrides(args)

    try:
        if args.step == "m0":
            extra = _run_m0(
                pdf_path=pdf_path,
                filename=filename,
                out_dir=out_dir,
                args=args,
                trace_stages=trace_stages,
                token_cost_tracker=token_cost_tracker,
            )
        elif args.step == "coarse":
            extra = _run_coarse(
                filename=filename,
                out_dir=out_dir,
                args=args,
                trace_stages=trace_stages,
                token_cost_tracker=token_cost_tracker,
            )
        else:
            extra = _run_fine(
                filename=filename,
                out_dir=out_dir,
                args=args,
                trace_stages=trace_stages,
                token_cost_tracker=token_cost_tracker,
            )
    finally:
        restore_models()

    elapsed = time.time() - t_start
    update_pipeline_state(
        pipeline_state_path(out_dir),
        stage={"m0": 0, "coarse": 2, "fine": 5}[args.step],
        payload={"progressive_step": args.step, **extra, "elapsed_s": elapsed},
    )
    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at=f"progressive_{args.step}",
        page_count=int(extra.get("page_count") or 0),
        pipeline_stage={"m0": 0, "coarse": 2, "fine": 5}[args.step],
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
        extra_summary=extra,
    )


def _run_m0(
    *,
    pdf_path: str,
    filename: str,
    out_dir: _Path,
    args: Any,
    trace_stages: list[dict[str, Any]],
    token_cost_tracker: TokenCostTracker,
) -> dict[str, Any]:
    from app.services.page_memory.memory_service import PageMemoryInput, run
    from app.services.page_memory.phase_a import (
        PHASE_B_BLACKBOARD_REL,
        PHASE_B_BODY_TEXT_REL,
        PHASE_B_FULL_TEXT_REL,
        PHASE_B_SOURCE_PDF_REL,
    )
    from shared.models.schemas.page_memory_config import PageMemoryConfig
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

    page_output_dir, parsed_df = run(
        PageMemoryInput(
            file_path=pdf_path,
            filename=filename,
            output_dir=str(out_dir.parent),
            job_id=filename,
            internal_output_filename=out_dir.name,
            page_memory_config=PageMemoryConfig.default(),
            ingestion_mode="progressive",
        )
    )
    if _Path(page_output_dir).resolve() != out_dir.resolve():
        raise RuntimeError(
            f"M0 wrote {page_output_dir}, expected {out_dir}. "
            "Do not change PageMemoryInput.output_dir / internal_output_filename."
        )

    chunks = list(dataframe_to_chunks(parsed_df))
    _write_chunk_bundle(out_dir / "m0_chunks.json", chunks)
    _write_chunk_bundle(out_dir / "chunks.json", chunks)
    record_stage(
        trace_stages,
        "progressive.m0",
        page_info={"page_count": len(chunks)},
        variables={
            "chunk_count": len(chunks),
            "blackboard": str(out_dir / PHASE_B_BLACKBOARD_REL),
            "source_pdf": str(out_dir / PHASE_B_SOURCE_PDF_REL),
        },
    )
    token_cost_tracker.snapshot_stage("progressive.m0")
    _maybe_publish(
        out_dir=out_dir,
        filename=filename,
        chunks=chunks,
        enrichment_status="text",
        args=args,
        replace_scope_id=None,
    )
    extra = {
        "page_count": len(chunks),
        "chunk_count": len(chunks),
        "phase_b": [
            PHASE_B_SOURCE_PDF_REL,
            PHASE_B_BLACKBOARD_REL,
            PHASE_B_FULL_TEXT_REL,
            PHASE_B_BODY_TEXT_REL,
        ],
    }
    logger.info("  M0 pages={} → {}", len(chunks), out_dir / "m0_chunks.json")
    return extra


def _run_coarse(
    *,
    filename: str,
    out_dir: _Path,
    args: Any,
    trace_stages: list[dict[str, Any]],
    token_cost_tracker: TokenCostTracker,
) -> dict[str, Any]:
    from app.services.page_memory.phase_a import (
        PHASE_B_ANATOMY_REL,
        PHASE_B_BLACKBOARD_REL,
        PHASE_B_MANIFEST_REL,
        PHASE_B_SOURCE_PDF_REL,
        build_phase_a,
        persist_phase_b_inputs,
    )
    from app.services.page_memory.scope_enrichment_service import (
        _ClaimedScopeTarget,
        _build_scope_section_summaries,
        _generate_and_persist_page_embeddings,
        _resume_profile_from_m0_workspace,
    )
    from shared.models.schemas.page_memory_config import PageMemoryConfig
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.publication_models import DocumentPublicationScope

    require_file(
        out_dir / PHASE_B_BLACKBOARD_REL,
        hint="Run --step m0 first.",
    )
    require_file(
        out_dir / PHASE_B_SOURCE_PDF_REL,
        hint="Run --step m0 first (source.pdf is written by memory_service.run).",
    )

    page_memory_config = PageMemoryConfig.default()
    state = _load_debug_state(out_dir)
    profile = _resume_profile_from_m0_workspace(
        add_dir=str(out_dir),
        target=_ClaimedScopeTarget(
            document_id=str(state["document_id"]) if state else "debug",
            job_id=str(state["job_id"]) if state else filename,
            user_id=str(state["user_id"]) if state else "debug_local_user",
            namespace=str(state["namespace"]) if state else "default",
            source_file_name=filename,
            raw_prefix=None,
            job_metadata={},
            page_memory_config=page_memory_config,
        ),
        job_result_id=str(state["job_result_id"]) if state else "debug",
    )
    phase_a = build_phase_a(profile=profile, filename=filename)
    persist_phase_b_inputs(
        output_dir=str(out_dir),
        profile=profile,
        manifest=phase_a.manifest,
    )
    chunks = list(dataframe_to_chunks(phase_a.coarse_df))
    _write_chunk_bundle(out_dir / "coarse_chunks.json", chunks)
    _write_chunk_bundle(out_dir / "chunks.json", chunks)
    coarse_summaries = _build_scope_section_summaries(
        output_dir=str(out_dir),
        chunks=chunks,
        source_file_name=filename,
        summary_use_llm=False,
    )
    scopes = list(phase_a.manifest.get("scopes") or [])
    _print_scopes(scopes)
    if args.list_scopes:
        logger.info("  --list-scopes: coarse built, printed scopes, not publishing")
    record_stage(
        trace_stages,
        "progressive.coarse",
        page_info={"page_count": int(phase_a.manifest.get("page_count") or 0)},
        variables={
            "scope_count": len(scopes),
            "chunk_count": len(chunks),
            "manifest": str(out_dir / PHASE_B_MANIFEST_REL),
            "anatomy": str(out_dir / PHASE_B_ANATOMY_REL),
        },
    )
    token_cost_tracker.snapshot_stage("progressive.coarse")
    if not args.list_scopes:
        _maybe_publish(
            out_dir=out_dir,
            filename=filename,
            chunks=chunks,
            enrichment_status="coarse",
            args=args,
            replace_scope_id="*",
            write_scope_rows=True,
            section_summaries=coarse_summaries,
        )
        if args.run_db:
            published_state = _load_debug_state(out_dir)
            if published_state is not None:
                _generate_and_persist_page_embeddings(
                    add_dir=str(out_dir),
                    workspace_dir=str(out_dir),
                    profile=profile,
                    publication_scope=DocumentPublicationScope(
                        user_id=str(published_state["user_id"]),
                        namespace=str(published_state["namespace"]),
                        document_id=str(published_state["document_id"]),
                        job_result_id=str(published_state["job_result_id"]),
                        source_file_name=filename,
                    ),
                )
    extra = {
        "page_count": int(phase_a.manifest.get("page_count") or 0),
        "chunk_count": len(chunks),
        "scope_ids": [str(entry.get("scope_id") or "") for entry in scopes],
    }
    logger.info(
        "  coarse scopes={} chunks={} → {}",
        len(scopes),
        len(chunks),
        out_dir / "coarse_chunks.json",
    )
    return extra


def _run_fine(
    *,
    filename: str,
    out_dir: _Path,
    args: Any,
    trace_stages: list[dict[str, Any]],
    token_cost_tracker: TokenCostTracker,
) -> dict[str, Any]:
    from app.services.document_agent.pdf_text import coerce_page_text_cache
    from app.services.page_memory.phase_a import (
        PHASE_B_ANATOMY_REL,
        PHASE_B_BODY_TEXT_REL,
        PHASE_B_FULL_TEXT_REL,
        PHASE_B_MANIFEST_REL,
        PHASE_B_SOURCE_PDF_REL,
    )
    from app.services.page_memory.scope_enrichment_service import (
        _load_anatomy_features_and_labels,
        _read_json_dict,
        run_single_scope_refinement,
    )
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    require_file(out_dir / PHASE_B_MANIFEST_REL, hint="Run --step coarse first.")
    require_file(out_dir / PHASE_B_SOURCE_PDF_REL, hint="Run --step m0 first.")
    require_file(out_dir / PHASE_B_FULL_TEXT_REL, hint="Run --step m0 first.")
    require_file(out_dir / PHASE_B_ANATOMY_REL, hint="Run --step coarse first.")

    manifest = _read_json_dict(str(out_dir / PHASE_B_MANIFEST_REL))
    scopes = [entry for entry in (manifest.get("scopes") or []) if isinstance(entry, dict)]
    _print_scopes(scopes)
    if args.list_scopes:
        return {
            "page_count": int(manifest.get("page_count") or 0),
            "scope_ids": [str(entry.get("scope_id") or "") for entry in scopes],
        }

    selected = _selected_scope_ids(args=args, scopes=scopes)
    page_texts = coerce_page_text_cache(
        _read_json_dict(str(out_dir / PHASE_B_FULL_TEXT_REL))
    )
    body_text_by_page = coerce_page_text_cache(
        _read_json_dict(str(out_dir / PHASE_B_BODY_TEXT_REL))
        if (out_dir / PHASE_B_BODY_TEXT_REL).is_file()
        else {}
    )
    page_features, page_labels = _load_anatomy_features_and_labels(
        str(out_dir / PHASE_B_ANATOMY_REL)
    )
    page_memory_config = PageMemoryConfig.from_mapping(
        {
            **PageMemoryConfig.default().to_dict(),
            "fine_text_extractor": args.fine_text_extractor,
        }
    )
    refined_scope_ids: list[str] = []
    last_chunk_count = 0
    for scope_id in selected:
        output = run_single_scope_refinement(
            scope_id=scope_id,
            pdf_path=str(out_dir / PHASE_B_SOURCE_PDF_REL),
            output_dir=str(out_dir),
            manifest=manifest,
            page_texts=page_texts,
            body_text_by_page=body_text_by_page,
            page_features=page_features,
            page_labels=page_labels,
            page_memory_config=page_memory_config,
            source_file_name=filename,
            summary_use_llm=False,
        )
        scope_dir = out_dir / "scopes" / scope_id
        scope_dir.mkdir(parents=True, exist_ok=True)
        _write_chunk_bundle(scope_dir / "refined_chunks.json", output.chunks)
        last_chunk_count = len(output.chunks)
        refined_scope_ids.append(scope_id)
        record_stage(
            trace_stages,
            "progressive.fine",
            page_info={"scope_id": scope_id},
            variables={
                "scope_id": scope_id,
                "chunk_count": len(output.chunks),
                "artifact_refs": sorted(output.artifact_refs),
            },
        )
        token_cost_tracker.snapshot_stage(f"progressive.fine.{scope_id}")
        _maybe_publish(
            out_dir=out_dir,
            filename=filename,
            chunks=output.chunks,
            enrichment_status="coarse",
            args=args,
            replace_scope_id=scope_id,
            section_summaries=output.section_summaries,
            artifact_refs=output.artifact_refs,
        )
        logger.info(
            "  fine {} chunks={} → {}",
            scope_id,
            len(output.chunks),
            scope_dir / "refined_chunks.json",
        )
    return {
        "page_count": int(manifest.get("page_count") or 0),
        "chunk_count": last_chunk_count,
        "scope_ids": refined_scope_ids,
    }


def _apply_model_overrides(args: Any):
    from shared.core.config import settings

    previous_image = settings.IMAGE_MODEL
    previous_hierarchy = settings.HIERARCHY_LLM_MODEL
    previous_env = os.environ.get("IMAGE_MODEL")
    if args.vlm_model:
        settings.IMAGE_MODEL = args.vlm_model
        os.environ["IMAGE_MODEL"] = args.vlm_model
    if args.model:
        settings.HIERARCHY_LLM_MODEL = args.model

    def restore() -> None:
        settings.IMAGE_MODEL = previous_image
        settings.HIERARCHY_LLM_MODEL = previous_hierarchy
        if previous_env is None:
            os.environ.pop("IMAGE_MODEL", None)
        else:
            os.environ["IMAGE_MODEL"] = previous_env

    return restore


def _selected_scope_ids(*, args: Any, scopes: list[dict[str, Any]]) -> list[str]:
    available_rows = [
        {
            "scope_id": str(entry.get("scope_id") or ""),
            "start_page": int(entry.get("start_page") or 0),
            "end_page": int(entry.get("end_page") or 0),
        }
        for entry in scopes
        if entry.get("scope_id")
    ]
    available = [row["scope_id"] for row in available_rows]
    raw = str(getattr(args, "scope_id", "") or "").strip()
    if raw:
        wanted = [part.strip() for part in raw.split(",") if part.strip()]
        missing = [scope_id for scope_id in wanted if scope_id not in available]
        if missing:
            raise SystemExit(
                f"Unknown --scope-id {missing}. Available: {', '.join(available)}"
            )
        return wanted
    page_range = str(getattr(args, "page_range", "") or "").strip()
    if page_range:
        parts = page_range.split("-")
        try:
            start = int(parts[0])
            end = int(parts[1]) if len(parts) > 1 else start
        except ValueError:
            raise SystemExit(f"Invalid --page-range {page_range!r}; expected e.g. 14-23")
        selected = [
            row["scope_id"]
            for row in available_rows
            if row["start_page"] <= end and row["end_page"] >= start
        ]
        if not selected:
            raise SystemExit(
                f"No scope overlaps --page-range {start}-{end}. Available: {', '.join(available)}"
            )
        return selected
    if args.all_scopes or args.fat_only:
        if args.fat_only and available_rows:
            fattest = max(
                available_rows,
                key=lambda row: int(row["end_page"]) - int(row["start_page"]),
            )
            return [fattest["scope_id"]]
        return available
    raise SystemExit(
        "fine step needs --scope-id, --page-range, --all-scopes, --fat-only, or --list-scopes"
    )


def _print_scopes(scopes: list[dict[str, Any]]) -> None:
    logger.info("  scopes ({}):", len(scopes))
    for entry in scopes:
        logger.info(
            "    {} p{}-{} strategy={}",
            entry.get("scope_id"),
            entry.get("start_page"),
            entry.get("end_page"),
            entry.get("strategy"),
        )


def _write_chunk_bundle(path: _Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable: list[dict[str, Any]] = []
    for chunk in chunks:
        meta = chunk.get("metadata")
        if isinstance(meta, dict) and "_page_embedding" in meta:
            serializable.append(
                {
                    **chunk,
                    "metadata": {
                        k: v for k, v in meta.items() if k != "_page_embedding"
                    },
                }
            )
        else:
            serializable.append(chunk)
    path.write_text(
        json.dumps({"chunks": serializable}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _maybe_publish(
    *,
    out_dir: _Path,
    filename: str,
    chunks: list[dict[str, Any]],
    enrichment_status: str,
    args: Any,
    replace_scope_id: str | None,
    write_scope_rows: bool = False,
    section_summaries: dict[str, str] | None = None,
    artifact_refs: set[str] | None = None,
) -> None:
    if not args.run_db:
        return
    from scripts._debug_publish import _upload_assets, publish_debug_result_dir
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.document import Document, DocumentScopeEnrichment
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.utils.utc_now import utc_now_naive
    from sqlalchemy import func, select, update

    state = _load_debug_state(out_dir)
    if state is None or replace_scope_id is None:
        from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder

        doc_nav = ZipDocNavigationBuilder().build_doc_nav(chunks, filename)
        (out_dir / "doc_nav.json").write_text(
            json.dumps(doc_nav, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        published = publish_debug_result_dir(
            result_dir=out_dir,
            source_file_name=filename,
            chunks=chunks,
            job_id=args.publish_job_id,
            parse_track="page_memory",
            upload_assets=not args.skip_assets,
        )
        with get_sync_db_context() as db:
            document = db.execute(
                select(Document).where(Document.document_id == published.document_id)
            ).scalar_one()
            job = db.execute(
                select(Job).where(Job.job_id == published.job_id)
            ).scalar_one()
            job.job_metadata = {
                **dict(job.job_metadata or {}),
                "parse_track": "page_memory",
                "ingestion_mode": "progressive",
                "enrichment_status": enrichment_status,
            }
            if document.current_job_result_id:
                job_result = db.execute(
                    select(JobResult).where(
                        JobResult.id == document.current_job_result_id
                    )
                ).scalar_one()
                job_result.document_metadata = {
                    **dict(job_result.document_metadata or {}),
                    "parse_track": "page_memory",
                    "ingestion_mode": "progressive",
                    "enrichment_status": enrichment_status,
                }
            state = {
                "job_id": published.job_id,
                "document_id": published.document_id,
                "job_result_id": document.current_job_result_id,
                "user_id": published.user_id,
                "namespace": published.namespace,
            }
            if write_scope_rows and published.document_id and document.current_job_result_id:
                from shared.services.jobs.lifecycle.scope_enrichments import (
                    write_coarse_scope_enrichments,
                )

                write_coarse_scope_enrichments(
                    db,
                    document_id=published.document_id,
                    job_result_id=document.current_job_result_id,
                )
        _write_debug_state(out_dir, state)
        logger.info(
            "  published job_id={} document_id={} status={}",
            state["job_id"],
            state["document_id"],
            enrichment_status,
        )
        return

    from shared.services.jobs.lifecycle.scope_enrichments import (
        write_coarse_scope_enrichments,
    )
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )

    if not args.skip_assets and artifact_refs:
        _upload_assets(
            job_id=str(state["job_id"]),
            result_dir=str(out_dir),
            refs=artifact_refs,
        )

    effective_status = enrichment_status
    with get_sync_db_context() as db:
        replace_document_scope_content(
            db,
            scope=DocumentPublicationScope(
                user_id=str(state["user_id"]),
                namespace=str(state["namespace"]),
                document_id=str(state["document_id"]),
                job_result_id=str(state["job_result_id"]),
                source_file_name=filename,
            ),
            scope_id=replace_scope_id,
            chunks=chunks,
            section_summaries=section_summaries,
        )
        if write_scope_rows:
            write_coarse_scope_enrichments(
                db,
                document_id=str(state["document_id"]),
                job_result_id=str(state["job_result_id"]),
            )
        elif replace_scope_id != "*":
            now = utc_now_naive()
            db.execute(
                update(DocumentScopeEnrichment)
                .where(
                    DocumentScopeEnrichment.job_result_id
                    == str(state["job_result_id"])
                )
                .where(DocumentScopeEnrichment.scope_id == replace_scope_id)
                .values(
                    state="refined",
                    refined_at=now,
                    updated_at=now,
                )
            )
            remaining = int(
                db.execute(
                    select(func.count())
                    .select_from(DocumentScopeEnrichment)
                    .where(
                        DocumentScopeEnrichment.job_result_id
                        == str(state["job_result_id"])
                    )
                    .where(DocumentScopeEnrichment.state != "refined")
                ).scalar_one()
            )
            total_scopes = int(
                db.execute(
                    select(func.count())
                    .select_from(DocumentScopeEnrichment)
                    .where(
                        DocumentScopeEnrichment.job_result_id
                        == str(state["job_result_id"])
                    )
                ).scalar_one()
            )
            if total_scopes > 0 and remaining == 0:
                effective_status = "complete"
        job = db.execute(select(Job).where(Job.job_id == state["job_id"])).scalar_one()
        job.job_metadata = {
            **dict(job.job_metadata or {}),
            "enrichment_status": effective_status,
        }
        job_result = db.execute(
            select(JobResult).where(JobResult.id == state["job_result_id"])
        ).scalar_one()
        job_result.document_metadata = {
            **dict(job_result.document_metadata or {}),
            "enrichment_status": effective_status,
        }
    logger.info(
        "  replaced scope={} on document_id={} status={}",
        replace_scope_id,
        state["document_id"],
        effective_status,
    )


def _load_debug_state(out_dir: _Path) -> dict[str, Any] | None:
    path = out_dir / _DEBUG_STATE_NAME
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw.get("job_result_id"):
        return None
    return raw


def _write_debug_state(out_dir: _Path, state: dict[str, Any]) -> None:
    (out_dir / _DEBUG_STATE_NAME).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
