"""Refine one coarse spreadsheet sheet group of a progressive revision.

A retrieval hit on any coarse sheet queues its whole formula-closed group.
The group is structure-parsed exactly as the full parser parses it, its
coarse sheet tables are replaced by the refined tables, and the revision is
finalized once every group is refined.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.document_ingestion.artifact_refs import (
    collect_referenced_artifact_refs,
)
from app.services.document_ingestion.scope_refinement import (
    REFINED_ENRICHMENT_STATE,
    ClaimedScopeRevision,
    PackageRefs,
    ScopePublisher,
    ScopeRefinementOutput,
    claim_scope_revision,
    finalize_all_scopes_refined,
    flush_scope_token_usage,
    freeze_scope_artifacts,
    publish_and_mark_refined,
    resolve_default_scope_publisher,
    upload_scope_artifacts,
)
from app.services.document_parser.formats.excel.baseline import WORKBOOK_REL
from app.services.document_parser.formats.excel.formula_graph import build_formula_graph
from app.services.document_parser.formats.excel.sheet_groups import (
    INCLUDE_HIDDEN_SHEETS,
    parseable_sheet_names,
    plan_sheet_groups,
)
from app.services.document_parser.formats.excel.table_parser import (
    ExcelWorkbookParseRequest,
    excel_rows_to_dataframe,
    normalise_excel_llm_parameters,
    parse_sheet_group,
)
from app.services.document_parser.formats.excel.workbook_grid import load_workbook_grid
from app.services.document_parser.orchestration.parse_session import (
    _resolve_output_paths,
    llm_parameters_from_job_metadata,
)
from app.services.document_parser.support.identifiers import get_str_time
from shared.models.database.document import SCOPE_KIND_SHEET_GROUP
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.ai.llm_overrides import cleanup_llm_overrides, init_llm_overrides
from shared.services.ai.token_tracking import cleanup_token_tracker, init_token_tracker
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.jobs.lifecycle.scope_enrichments import (
    mark_scope_enrichment_failed,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.storage.result_storage import ResultStorage, get_result_storage

# Every refined chunk references its own frozen asset; the sheet track keeps no
# baseline asset or extra working file in the final package.
SHEET_PACKAGE_REFS = PackageRefs(extra_refs=frozenset(), baseline_refs=frozenset())


@dataclass(frozen=True)
class SheetRevisionTarget:
    document_id: str
    job_id: str
    user_id: str
    namespace: str
    source_file_name: str
    raw_prefix: str | None
    job_metadata: dict[str, Any]
    claim_generation: str | None
    serving_raw_prefix: str | None


def build_sheet_revision_target(
    *,
    document_id: str,
    document_namespace: str,
    job_id: str,
    job_user_id: str,
    job_metadata: dict[str, Any],
    source_file_name: str,
    claim_generation: str | None,
) -> SheetRevisionTarget:
    raw_prefix = job_metadata.get("result_raw_prefix")
    serving_raw_prefix = job_metadata.get("result_serving_raw_prefix")
    return SheetRevisionTarget(
        document_id=document_id,
        job_id=job_id,
        user_id=CorpusStorage.resolve_namespace(document_namespace).resolve_owner(
            job_user_id
        ),
        namespace=normalize_retrieval_namespace(document_namespace),
        source_file_name=source_file_name,
        raw_prefix=raw_prefix if isinstance(raw_prefix, str) and raw_prefix else None,
        job_metadata=job_metadata,
        claim_generation=claim_generation,
        serving_raw_prefix=(
            serving_raw_prefix
            if isinstance(serving_raw_prefix, str) and serving_raw_prefix
            else None
        ),
    )


@dataclass(frozen=True)
class _ClaimedSheetGroup:
    target: SheetRevisionTarget
    sheet_names: tuple[str, ...]


def execute_sheet_group_enrichment(
    *,
    job_result_id: str,
    scope_id: str,
    result_storage_factory: Callable[[], ResultStorage] = get_result_storage,
    scope_publisher: ScopePublisher | None = None,
) -> dict[str, Any]:
    """Claim one sheet group, refine it, and publish its refined tables."""
    claim_status, claimed = claim_scope_revision(
        job_result_id=job_result_id,
        scope_id=scope_id,
        build_target=_build_claimed_sheet_group,
    )
    if claimed is None:
        return {"status": claim_status, "job_result_id": job_result_id, "scope_id": scope_id}
    target = claimed.target
    storage = result_storage_factory()
    publisher = scope_publisher or resolve_default_scope_publisher()
    workspace_dir = tempfile.mkdtemp(prefix=f"sheet_scope_{job_result_id}_")
    init_llm_overrides(JobMetadataHelper.get_llm_config(target.job_metadata))
    init_token_tracker()
    try:
        output = refine_sheet_group(
            workspace_dir=workspace_dir,
            target=target,
            scope_id=scope_id,
            sheet_names=claimed.sheet_names,
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
            return {"status": "aborted", "job_result_id": job_result_id, "scope_id": scope_id}
        if all_refined:
            finalize_all_scopes_refined(
                job_result_id=job_result_id,
                target=target,
                storage=storage,
                workspace_dir=workspace_dir,
                package_refs=SHEET_PACKAGE_REFS,
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


def refine_sheet_group(
    *,
    workspace_dir: str,
    target: SheetRevisionTarget,
    scope_id: str,
    sheet_names: tuple[str, ...],
    storage: ResultStorage,
) -> ScopeRefinementOutput:
    """Structure-parse one group of the baseline workbook into refined table chunks."""
    relative_root, add_dir = _resolve_output_paths(
        filename=target.source_file_name,
        internal_output_filename=target.source_file_name,
        output_dir=workspace_dir,
    )
    os.makedirs(add_dir, exist_ok=True)
    workbook_path = _download_baseline_workbook(
        storage=storage, target=target, add_dir=add_dir
    )
    workbook = load_workbook_grid(workbook_path)
    graph = build_formula_graph(workbook)
    request = ExcelWorkbookParseRequest(
        file_path=workbook_path,
        file_name=target.source_file_name,
        output_dir=add_dir,
        base_llm_paras=normalise_excel_llm_parameters(
            llm_parameters_from_job_metadata(target.job_metadata)
        ),
        relative_root=relative_root,
        include_hidden_sheets=INCLUDE_HIDDEN_SHEETS,
    )
    groups = plan_sheet_groups(
        workbook,
        parseable_sheet_names(
            workbook, include_hidden_sheets=request.include_hidden_sheets
        ),
        graph,
    )
    group = next((item for item in groups if item.scope_id == scope_id), None)
    if group is None or group.sheet_names != sheet_names:
        raise ValueError(
            f"Sheet group {scope_id} no longer matches the baseline workbook"
        )
    dataframe = excel_rows_to_dataframe(
        parse_sheet_group(
            request,
            workbook=workbook,
            graph=graph,
            group=group,
            time_stamp=get_str_time(),
        )
    )
    chunks = dataframe_to_chunks(dataframe)
    return ScopeRefinementOutput(
        scope_id=scope_id,
        dataframe=dataframe,
        chunks=chunks,
        section_summaries={},
        artifact_refs=collect_referenced_artifact_refs(chunks),
    )


def _build_claimed_sheet_group(claimed: ClaimedScopeRevision) -> _ClaimedSheetGroup:
    if claimed.scope.scope_kind != SCOPE_KIND_SHEET_GROUP or not claimed.scope.sheet_names:
        raise ValueError(f"Scope {claimed.scope.scope_id} is not a sheet group")
    return _ClaimedSheetGroup(
        target=build_sheet_revision_target(
            document_id=str(claimed.document.document_id),
            document_namespace=claimed.document.namespace,
            job_id=str(claimed.job.job_id),
            job_user_id=str(claimed.job.user_id),
            job_metadata=claimed.job_metadata,
            source_file_name=claimed.source_file_name,
            claim_generation=claimed.claim_generation,
        ),
        sheet_names=tuple(claimed.scope.sheet_names),
    )


def _download_baseline_workbook(
    *,
    storage: ResultStorage,
    target: SheetRevisionTarget,
    add_dir: str,
) -> str:
    if WORKBOOK_REL not in (target.job_metadata.get("sheet_progressive_artifact_refs") or []):
        raise ValueError("Progressive spreadsheet baseline did not keep its workbook")
    temp_path = storage.download_raw_to_temp(
        job_id=target.job_id,
        relative_path=WORKBOOK_REL,
        raw_prefix=target.raw_prefix,
        suffix=Path(WORKBOOK_REL).suffix,
        temp_dir=add_dir,
    )
    destination = os.path.join(add_dir, WORKBOOK_REL)
    Path(temp_path).replace(destination)
    return destination
