"""Progressive spreadsheet ingestion: coarse sheets grouped by formula scope."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.testing.contract_runtime import get_contract_database_url


@pytest.fixture(autouse=True)
def _contract_environment(worker_contract_environment: None) -> None:
    del worker_contract_environment


def _write_workbook(path: Path) -> Path:
    workbook = openpyxl.Workbook()
    summary = workbook.active
    assert summary is not None
    summary.title = "Summary"
    summary["A1"] = "Total"
    summary["B1"] = "=SUM(Detail!B2:B3)"
    detail = workbook.create_sheet("Detail")
    detail.append(["Item", "Cost"])
    detail.append(["Steel", 10])
    detail.append(["Glass", 5])
    detail.column_dimensions["Z"].width = 20
    detail["Z40"].font = openpyxl.styles.Font(bold=True)
    notes = workbook.create_sheet("Notes")
    notes["A1"] = "Assumption"
    notes["B1"] = "=Hidden!A1"
    hidden = workbook.create_sheet("Hidden")
    hidden["A1"] = 3
    hidden.sheet_state = "hidden"
    workbook.create_sheet("Empty")
    workbook.save(path)
    return path


def test_sheet_groups_follow_resolved_cross_sheet_formulas(tmp_path: Path) -> None:
    from app.services.document_parser.formats.excel.formula_graph import (
        build_formula_graph,
    )
    from app.services.document_parser.formats.excel.sheet_groups import (
        parseable_sheet_names,
        plan_sheet_groups,
    )
    from app.services.document_parser.formats.excel.workbook_grid import (
        load_workbook_grid,
    )

    workbook = load_workbook_grid(str(_write_workbook(tmp_path / "budget.xlsx")))
    names = parseable_sheet_names(workbook, include_hidden_sheets=False)
    groups = plan_sheet_groups(workbook, names, build_formula_graph(workbook))

    assert names == ["Summary", "Detail", "Notes", "Empty"]
    assert [(group.scope_id, group.sheet_names) for group in groups] == [
        ("sheets:1", ("Summary", "Detail")),
        ("sheets:3", ("Notes",)),
        ("sheets:5", ("Empty",)),
    ]


def test_sheet_baseline_emits_one_coarse_table_per_sheet(tmp_path: Path) -> None:
    from app.services.document_parser.formats.excel.baseline import (
        WORKBOOK_REL,
        parse_excel_baseline,
    )
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

    source = _write_workbook(tmp_path / "budget.xlsx")
    output_dir = tmp_path / "out"
    chunks = dataframe_to_chunks(
        parse_excel_baseline(
            str(source),
            file_name="budget.xlsx",
            output_dir=str(output_dir),
            relative_root="budget.xlsx",
        )
    )

    assert (output_dir / WORKBOOK_REL).is_file()
    assert [chunk["path"] for chunk in chunks] == [
        "budget.xlsx/Summary",
        "budget.xlsx/Detail",
        "budget.xlsx/Notes",
    ], "a sheet without occupied cells emits no coarse table"
    detail = chunks[1]["metadata"]
    assert chunks[1]["type"] == "table"
    assert detail["excel_range"] == "A1:B3", "styled empty cells stay outside"
    assert detail["header_row_count"] == 0
    assert detail["scope_kind"] == "sheet_group"
    assert detail["scope_id"] == "sheets:1"
    assert detail["sheet_names"] == ["Summary", "Detail"]
    assert detail["enrichment_state"] == "coarse"
    table_html = (output_dir / str(detail["file_path"])).read_text(encoding="utf-8")
    assert "Steel" in table_html and "<thead>" not in table_html
    assert detail["coarse_search_text"] == "Item\nCost\nSteel\nGlass", (
        "numeric cells stay out of the coarse search text"
    )
    from shared.services.retrieval.search.lexical_text import build_content_search_text

    search_text = build_content_search_text(chunks[1]) or ""
    assert "steel" in search_text.lower().split()
    assert "10" not in search_text.split()


def test_sheet_baseline_finalization_writes_one_scope_per_group(tmp_path: Path) -> None:
    from app.services.document_parser.formats.excel.baseline import (
        parse_excel_baseline,
    )
    from shared.models.database.document import DocumentScopeEnrichment
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.user import User
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.jobs.progressive_ingestion import read_progressive_state

    chunks = dataframe_to_chunks(
        parse_excel_baseline(
            str(_write_workbook(tmp_path / "budget.xlsx")),
            file_name="budget.xlsx",
            output_dir=str(tmp_path / "out"),
            relative_root="budget.xlsx",
        )
    )
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db, db.begin():
            db.add(User(id="sheet-owner", name="Sheet owner", email="sheet@example.invalid"))
            db.flush()
            db.add(
                Job(
                    job_id="sheet-job",
                    user_id="sheet-owner",
                    job_type="document_ingestion",
                    source_type="file",
                    status="running",
                    job_metadata={
                        "document_id": "doc_sheet",
                        "namespace": "default",
                        "source_file_name": "budget.xlsx",
                        "parse_track": "sheet",
                        "ingestion_mode": "progressive",
                        "enrichment_status": "coarse",
                    },
                )
            )
            db.flush()
            finalization = SyncJobSuccessFinalizer().finalize(
                db,
                job_id="sheet-job",
                result_s3_key="results/baseline.zip",
                checksum="a" * 64,
                zip_size=100,
                stored_count=len(chunks),
                delivery_mode="url",
                section_summaries=None,
                chunks=chunks,
            )
            assert finalization.response.should_commit()
            assert finalization.post_commit_effects.structure_job_result_ids == ()

        with Session(engine) as db:
            result = db.scalars(
                select(JobResult).where(JobResult.job_id == "sheet-job")
            ).one()
            state = read_progressive_state(result.document_metadata)
            assert state is not None
            assert set(state.stages) == {"finalization"}
            scopes = db.scalars(
                select(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == result.id)
                .order_by(DocumentScopeEnrichment.scope_id)
            ).all()
            assert [
                (scope.scope_id, scope.scope_kind, scope.sheet_names, scope.state)
                for scope in scopes
            ] == [
                ("sheets:1", "sheet_group", ["Summary", "Detail"], "coarse"),
                ("sheets:3", "sheet_group", ["Notes"], "coarse"),
            ]
            assert all(scope.page_start is None for scope in scopes)
    finally:
        engine.dispose()


@pytest.mark.parametrize("ingestion_mode", ["progressive", "full"])
def test_sheet_parse_execution_routes_only_progressive_to_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ingestion_mode: str,
) -> None:
    from app.services.document_ingestion import parse_execution
    from app.services.document_ingestion.processing_context import ParseJobContext
    from app.services.document_ingestion.source_preparation import PreparedSourceFile
    from app.services.document_parser.orchestration.parse_output import ParseOutput

    calls: list[str] = []
    monkeypatch.setattr(
        parse_execution,
        "_execute_sheet_baseline_parse",
        lambda **_: calls.append("baseline") or ParseOutput(output_dir="", parsed_df=None),
    )
    monkeypatch.setattr(
        parse_execution.parse_service,
        "checkerboard_parse_output",
        lambda **_: calls.append("full") or ParseOutput(output_dir="", parsed_df=None),
    )
    source = _write_workbook(tmp_path / "budget.xlsx")
    parse_execution.execute_document_parse(
        job_id="job",
        job_context=ParseJobContext(
            job_metadata={"parse_track": "sheet", "ingestion_mode": ingestion_mode},
            job_user_id="u1",
            metadata_service=None,  # pyright: ignore[reportArgumentType]
            redis_service=None,
            s3_key="uploads/budget.xlsx",
        ),
        prepared_source=PreparedSourceFile(
            source_file_name="budget.xlsx",
            internal_parse_name="budget.xlsx",
            local_file_path=str(source),
            file_extension=".xlsx",
        ),
        output_dir=str(tmp_path / "out"),
    )

    assert calls == ["baseline" if ingestion_mode == "progressive" else "full"]


_LUNA_CHOOSE = (
    "app.services.document_parser.formats.excel.luna_decisions."
    "GatewaySpreadsheetRegionJudge.choose"
)
_HEADER_DETECT = (
    "app.services.document_parser.formats.excel.header_detect."
    "TextClientSpreadsheetHeaderDetector.detect_spreadsheet_headers"
)
_HEADER_LOCATE = (
    "app.services.document_parser.formats.excel.header_detect."
    "TextClientSpreadsheetHeaderDetector.locate_header_start"
)


class _InMemoryResultStorage:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.uploaded_zips: list[str] = []

    def download_raw_to_temp(
        self,
        *,
        job_id: str,
        relative_path: str,
        raw_prefix: str | None = None,
        suffix: str,
        temp_dir: str,
    ) -> str:
        del job_id, raw_prefix
        target = Path(temp_dir) / f"_dl_{Path(relative_path).name}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[relative_path])
        return str(target)

    def upload_raw_file(
        self,
        *,
        job_id: str,
        relative_path: str,
        local_file_path: str,
        raw_prefix: str | None = None,
    ) -> None:
        del job_id, raw_prefix
        self.files[relative_path] = Path(local_file_path).read_bytes()

    def upload(
        self,
        *,
        job_id: str,
        result_dir: str,
        zip_file_path: str,
        artifact_refs: set[str] | None = None,
        snapshot_id: str | None = None,
    ):
        del result_dir, artifact_refs, snapshot_id
        from shared.services.storage.result_storage import UploadedResultBundle

        zip_key = f"results/{job_id}.zip"
        self.uploaded_zips.append(zip_key)
        Path(zip_file_path).unlink(missing_ok=True)
        return UploadedResultBundle(
            zip_key=zip_key, raw_prefix=f"results/{job_id}/", raw_files={}
        )


def _publish_sheet_baseline(engine, tmp_path: Path) -> tuple[str, _InMemoryResultStorage]:
    from sqlalchemy.orm import sessionmaker

    from app.services.document_parser.formats.excel.baseline import (
        WORKBOOK_REL,
        parse_excel_baseline,
    )
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.user import User
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.worker import IndexingWorker

    output_dir = tmp_path / "baseline"
    chunks = dataframe_to_chunks(
        parse_excel_baseline(
            str(_write_workbook(tmp_path / "budget.xlsx")),
            file_name="budget.xlsx",
            output_dir=str(output_dir),
            relative_root="budget.xlsx",
        )
    )
    storage = _InMemoryResultStorage(
        {WORKBOOK_REL: (output_dir / WORKBOOK_REL).read_bytes()}
    )
    with Session(engine) as db, db.begin():
        db.add(User(id="sheet-owner", name="Sheet owner", email="sheet@example.invalid"))
        db.flush()
        db.add(
            Job(
                job_id="sheet-job",
                user_id="sheet-owner",
                job_type="document_ingestion",
                source_type="file",
                status="running",
                job_metadata={
                    "document_id": "doc_sheet",
                    "namespace": "default",
                    "source_file_name": "budget.xlsx",
                    "parse_track": "sheet",
                    "ingestion_mode": "progressive",
                    "enrichment_status": "coarse",
                    "result_raw_prefix": "results/sheet-job/",
                    "result_serving_raw_prefix": "results/sheet-job/snapshots/base/",
                    "sheet_progressive_artifact_refs": [WORKBOOK_REL],
                    "parsing_params": {"summary_table": False},
                },
            )
        )
        db.flush()
        assert SyncJobSuccessFinalizer().finalize(
            db,
            job_id="sheet-job",
            result_s3_key="results/baseline.zip",
            checksum="a" * 64,
            zip_size=100,
            stored_count=len(chunks),
            delivery_mode="url",
            section_summaries=None,
            chunks=chunks,
        ).response.should_commit()
    assert (
        IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(
            "sheet-job"
        )
        == "completed"
    )
    with Session(engine) as db:
        revision_id = db.scalars(
            select(JobResult.id).where(JobResult.job_id == "sheet-job")
        ).one()
    return revision_id, storage


def test_returned_coarse_sheet_row_queues_its_sheet_group() -> None:
    from shared.services.jobs.lifecycle.scope_enrichments import (
        ENRICH_SHEET_GROUP_TASK_NAME,
        _row_returned_coarse_scope,
        enrich_scope_task_name,
    )

    row = {
        "chunk_type": "table",
        "job_result_id": "revision",
        "content": "tables/table-[1] Summary.html",
        "chunk_metadata": {
            "scope_id": "sheets:1",
            "scope_kind": "sheet_group",
            "enrichment_state": "coarse",
        },
    }
    assert _row_returned_coarse_scope(row)
    refined = {**row, "chunk_metadata": {"scope_id": "sheets:1"}}
    assert not _row_returned_coarse_scope(refined)
    assert enrich_scope_task_name("sheet_group") == ENRICH_SHEET_GROUP_TASK_NAME


def test_sheet_group_refinement_replaces_only_its_coarse_sheets_then_finalizes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_ingestion import scope_refinement
    from app.services.document_ingestion.sheet_enrichment_service import (
        execute_sheet_group_enrichment,
    )
    from shared.models.database.document import DocumentChunk, DocumentScopeEnrichment
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.progressive_ingestion import read_progressive_state

    monkeypatch.setattr(_LUNA_CHOOSE, lambda self, **kwargs: None)
    monkeypatch.setattr(_HEADER_DETECT, lambda self, rows_text: None)
    monkeypatch.setattr(_HEADER_LOCATE, lambda self, rows_text: None)
    monkeypatch.setattr(
        scope_refinement, "enrich_doc_nav_summaries", lambda *args, **kwargs: {}
    )
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        revision_id, storage = _publish_sheet_baseline(engine, tmp_path)

        def published() -> list[tuple[str, str | None, str | None]]:
            with Session(engine) as db:
                rows = db.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.job_result_id == revision_id)
                    .order_by(DocumentChunk.sort_order)
                ).all()
                return [
                    (
                        row.source_chunk_path or "",
                        (row.chunk_metadata or {}).get("scope_id"),
                        (row.chunk_metadata or {}).get("enrichment_state"),
                    )
                    for row in rows
                ]

        first = execute_sheet_group_enrichment(
            job_result_id=revision_id,
            scope_id="sheets:1",
            result_storage_factory=lambda: storage,
        )
        assert first["status"] == "refined" and first["all_refined"] is False
        rows = published()
        assert ("budget.xlsx/Notes", "sheets:3", "coarse") in rows
        group_rows = [row for row in rows if row[1] == "sheets:1"]
        assert [path for path, _scope, _state in group_rows] == ["budget.xlsx/Detail"]
        assert all(state is None for _path, _scope, state in group_rows)
        with Session(engine) as db:
            detail = db.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.job_result_id == revision_id)
                .where(DocumentChunk.source_chunk_path == "budget.xlsx/Detail")
            ).one()
            # Summary!B1 sums Detail, so the Summary fragment is Detail's satellite,
            # exactly as in a whole-workbook parse.
            assert any(
                "Total" in item for item in (detail.chunk_metadata or {})["satellites"]
            )
        assert storage.uploaded_zips == []

        second = execute_sheet_group_enrichment(
            job_result_id=revision_id,
            scope_id="sheets:3",
            result_storage_factory=lambda: storage,
        )
        assert second["all_refined"] is True
        assert [(path, scope) for path, scope, _state in published()] == [
            ("budget.xlsx/Detail", "sheets:1"),
            ("budget.xlsx/Notes", "sheets:3"),
        ], "refined groups keep group order, as the full parse emits them"
        assert all(state is None for _path, _scope, state in published())
        assert storage.uploaded_zips == ["results/sheet-job.zip"]
        with Session(engine) as db:
            assert {
                scope.state
                for scope in db.scalars(
                    select(DocumentScopeEnrichment).where(
                        DocumentScopeEnrichment.job_result_id == revision_id
                    )
                )
            } == {"refined"}
            state = read_progressive_state(
                db.get(JobResult, revision_id).document_metadata
            )
            assert state is not None
            assert state.stages["finalization"]["status"] == "complete"
            job = db.get(Job, "sheet-job")
            assert job is not None
            assert job.job_metadata["enrichment_status"] == "complete"
    finally:
        engine.dispose()



def test_sheet_group_scopes_sort_by_group_ordinal() -> None:
    from shared.services.retrieval.publication_scope_content import _scope_sort_key

    keys = [
        _scope_sort_key({"scope_id": scope_id})
        for scope_id in ("sheets:12", "sheets:3", "sheets:1")
    ]
    assert sorted(keys) == [(0, 1, 0), (0, 3, 0), (0, 12, 0)]
    assert _scope_sort_key({"scope_id": "sheets:x"}) == (1, 0, 0)
