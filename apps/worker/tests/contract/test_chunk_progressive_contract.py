"""Chunk-track progressive baseline, attribution, publish, and recovery contracts."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pandas as pd
import pytest

from shared.core.constants import ProcessingConstants
from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    ProgressiveIngestionState,
    StoredSnapshot,
)
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.retrieval.search.lexical_text import build_term_search_text


def test_parse_md_baseline_splits_headings_and_keeps_original_text(
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.markdown.parser import parse_md_baseline

    parsed = parse_md_baseline(
        [
            "# Overview",
            "First section body mentions alpha.",
            "## Details",
            "Second section body mentions beta.",
        ],
        str(tmp_path),
        relative_root="notes.md",
    )

    assert list(parsed["type"]) == ["PTXT", "PTXT"]
    assert "First section body mentions alpha." in parsed.iloc[0]["content"]
    assert "Second section body mentions beta." in parsed.iloc[1]["content"]
    assert "Overview" in parsed.iloc[0]["path"]
    assert "Details" in parsed.iloc[1]["path"]
    chunks = dataframe_to_chunks(parsed)
    assert all(chunk["type"] == "text" for chunk in chunks)
    term_texts = [
        build_term_search_text(chunk, path_text=str(chunk["path"])) or ""
        for chunk in chunks
    ]
    assert any("alpha" in text for text in term_texts)
    assert any("beta" in text for text in term_texts)


def test_parse_md_baseline_keeps_headingless_short_text_on_root(
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.markdown.parser import parse_md_baseline

    parsed = parse_md_baseline(
        ["Visible headingless sentence."],
        str(tmp_path),
        relative_root="plain.txt",
    )

    assert len(parsed) == 1
    assert parsed.iloc[0]["path"] == "plain.txt"
    assert "Visible headingless sentence." in parsed.iloc[0]["content"]
    assert "part " not in str(parsed.iloc[0]["path"])


def test_parse_md_baseline_divides_headingless_text_that_exceeds_threshold(
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.markdown.parser import parse_md_baseline

    threshold = int(3 * ProcessingConstants.POSTPROCESS_SUMMARY_LEN)
    first = "A" * (threshold - 500)
    second = "B" * (ProcessingConstants.MIN_THRESHOLD + 20)
    parsed = parse_md_baseline(
        [first, second],
        str(tmp_path),
        relative_root="plain.txt",
    )

    assert len(parsed) == 2
    assert "part 1" in str(parsed.iloc[0]["path"])
    assert "part 2" in str(parsed.iloc[1]["path"])
    assert "A" * 20 in parsed.iloc[0]["content"]
    assert "B" * 20 in parsed.iloc[1]["content"]


def test_attribute_pages_to_leaves_covers_cross_page_and_shared_page() -> None:
    from app.services.document_parser.formats.docx.page_attribution import (
        attribute_pages_to_leaves,
    )

    page_texts = {
        1: "Alpha paragraph sits on the first rendered page.",
        2: "Beta paragraph continues after the page break onto page two.",
    }
    attributed = attribute_pages_to_leaves(
        [
            "Alpha paragraph sits on the first rendered page.",
            "Beta paragraph continues after the page break onto page two.",
        ],
        page_texts,
    )

    assert attributed[0] == [1]
    assert attributed[1] == [2]

    shared = attribute_pages_to_leaves(
        ["Alpha paragraph", "sits on the first rendered page."],
        {1: "Alpha paragraph sits on the first rendered page."},
    )
    assert shared[0] == [1]
    assert shared[1] == [1]

    crossed = attribute_pages_to_leaves(
        ["first rendered page. Beta paragraph continues"],
        page_texts,
    )
    assert crossed[0] == [1, 2]


def test_attribute_pages_to_leaves_leaves_unmatched_empty() -> None:
    from app.services.document_parser.formats.docx.page_attribution import (
        attribute_pages_to_leaves,
    )

    attributed = attribute_pages_to_leaves(
        ["this leaf never appears in the rendered pages"],
        {1: "completely different rendered text"},
    )
    assert attributed[0] == []


def test_parse_docx_baseline_emits_page_rows_and_writes_resume_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_parser.formats.docx import baseline as docx_baseline

    rendered_pdf = tmp_path / "rendered.pdf"
    rendered_pdf.write_bytes(b"%PDF-1.4")

    def fake_convert(**_kwargs: object) -> tuple[str, str]:
        return str(rendered_pdf), rendered_pdf.name

    monkeypatch.setattr(docx_baseline, "_convert_with_libreoffice", fake_convert)
    monkeypatch.setattr(
        docx_baseline,
        "_read_rendered_page_texts",
        lambda _pdf_path: {1: "Rendered page one body"},
    )

    parsed = docx_baseline.parse_docx_baseline(
        str(tmp_path / "source.docx"),
        str(tmp_path),
        filename="source.docx",
    )

    assert list(parsed["type"]) == ["page"]
    assert parsed.iloc[0]["content"] == "Rendered page one body"
    assert parsed.iloc[0]["page_nums"] == "1"
    assert "scope_id" not in str(parsed.iloc[0].get("extra_metadata") or "")
    assert (tmp_path / docx_baseline.RENDERED_PDF_REL).is_file()
    cache = json.loads((tmp_path / docx_baseline.PAGE_TEXT_CACHE_REL).read_text())
    assert cache["1"] == "Rendered page one body"
    chunks = dataframe_to_chunks(parsed)
    assert chunks[0]["type"] == "page"
    term_text = build_term_search_text(chunks[0], path_text=str(chunks[0]["path"])) or ""
    assert "Rendered page one body" in term_text


def test_chunk_parse_execution_routes_progressive_to_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion.parse_execution import execute_document_parse
    from app.services.document_ingestion.processing_context import ParseJobContext
    from app.services.document_ingestion.source_preparation import PreparedSourceFile

    source = tmp_path / "notes.md"
    source.write_text("# Title\nVisible baseline sentence.\n", encoding="utf-8")

    def fake_full_parse(**_kwargs: object) -> None:
        raise AssertionError("full chunk parse must not run for progressive")

    monkeypatch.setattr(
        "app.services.document_parser.parse_service.checkerboard_parse_output",
        fake_full_parse,
    )
    output = execute_document_parse(
        job_id="job_chunk_progressive",
        job_context=ParseJobContext(
            job_metadata={
                "parse_track": "chunk",
                "ingestion_mode": "progressive",
                "source_file_name": "notes.md",
            },
            job_user_id="u1",
            metadata_service=None,
            redis_service=None,
            s3_key="uploads/notes.md",
        ),
        prepared_source=PreparedSourceFile(
            source_file_name="notes.md",
            internal_parse_name="notes.md",
            local_file_path=str(source),
            file_extension=".md",
        ),
        output_dir=str(tmp_path / "out"),
    )

    assert output.parsed_df is not None
    assert "Visible baseline sentence." in str(output.parsed_df.iloc[0]["content"])
    assert (Path(output.output_dir) / "notes.md").is_file()


def test_full_chunk_parse_execution_stays_on_checkerboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion.parse_execution import execute_document_parse
    from app.services.document_ingestion.processing_context import ParseJobContext
    from app.services.document_ingestion.source_preparation import PreparedSourceFile
    from app.services.document_parser.orchestration.parse_output import ParseOutput

    called = {"full": False}

    def fake_full_parse(**_kwargs: object) -> ParseOutput:
        called["full"] = True
        return ParseOutput(output_dir=str(tmp_path), parsed_df=pd.DataFrame())

    monkeypatch.setattr(
        "app.services.document_parser.parse_service.checkerboard_parse_output",
        fake_full_parse,
    )
    execute_document_parse(
        job_id="job_chunk_full",
        job_context=ParseJobContext(
            job_metadata={"parse_track": "chunk", "ingestion_mode": "full"},
            job_user_id="u1",
            metadata_service=None,
            redis_service=None,
            s3_key="uploads/notes.md",
        ),
        prepared_source=PreparedSourceFile(
            source_file_name="notes.md",
            internal_parse_name="notes.md",
            local_file_path=str(tmp_path / "notes.md"),
            file_extension=".md",
        ),
        output_dir=str(tmp_path / "out"),
    )
    assert called["full"] is True


def test_chunk_progressive_rejects_unsupported_extension(
    tmp_path: Path,
) -> None:
    from app.services.document_ingestion.parse_execution import execute_document_parse
    from app.services.document_ingestion.processing_context import ParseJobContext
    from app.services.document_ingestion.source_preparation import PreparedSourceFile

    with pytest.raises(ValidationException):
        execute_document_parse(
            job_id="job_chunk_xlsx",
            job_context=ParseJobContext(
                job_metadata={
                    "parse_track": "chunk",
                    "ingestion_mode": "progressive",
                },
                job_user_id="u1",
                metadata_service=None,
                redis_service=None,
                s3_key="uploads/policy.xlsx",
            ),
            prepared_source=PreparedSourceFile(
                source_file_name="policy.xlsx",
                internal_parse_name="policy.xlsx",
                local_file_path=str(tmp_path / "policy.xlsx"),
                file_extension=".xlsx",
            ),
            output_dir=str(tmp_path / "out"),
        )


def test_post_commit_plan_enqueues_chunk_final_not_structure() -> None:
    plan = PostCommitEffectPlan.from_success(
        cache_invalidation=None,
        webhook_event_id=None,
        structure_job_result_id=None,
        chunk_final_job_result_id="rev_chunk_1",
    )
    assert plan.chunk_final_job_result_ids == ("rev_chunk_1",)
    assert plan.structure_job_result_ids == ()


def test_write_leaf_page_nums_does_not_block_unmatched_rows() -> None:
    from app.services.document_ingestion.chunk_enrichment_service import (
        _write_leaf_page_nums,
    )

    parsed = pd.DataFrame(
        [
            {
                "content": "matching leaf on page one",
                "path": "doc/one",
                "type": "PTXT",
                "page_nums": "",
            },
            {
                "content": "unmatched leaf stays empty",
                "path": "doc/two",
                "type": "PTXT",
                "page_nums": "",
            },
        ]
    )
    _write_leaf_page_nums(parsed, {1: "matching leaf on page one"})
    assert parsed.iloc[0]["page_nums"] == "1"
    assert parsed.iloc[1]["page_nums"] == ""


def test_chunk_progressive_upload_marks_text_and_resume_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion import (
        success_finalization as success_finalization_mod,
    )
    from app.services.document_ingestion.parse_result_package import (
        GeneratedResultPackage,
        ParseArtifact,
        ParseResultPackage,
    )
    from app.services.document_parser.formats.docx.baseline import (
        PAGE_TEXT_CACHE_REL,
        RENDERED_PDF_REL,
    )

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / RENDERED_PDF_REL).write_bytes(b"%PDF")
    (result_dir / PAGE_TEXT_CACHE_REL).write_text('{"1":"page"}', encoding="utf-8")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")
    uploaded: list[str] = []

    class FakeStorage:
        def upload_raw_file(self, **kwargs: object) -> None:
            uploaded.append(str(kwargs["relative_path"]))
            assert kwargs["raw_prefix"] == "results/job-chunk/"

        def upload(self, **kwargs: object) -> SimpleNamespace:
            refs = sorted(kwargs["artifact_refs"] or [])
            return SimpleNamespace(
                zip_key="results/job-chunk/result.zip",
                raw_prefix="results/job-chunk/snap/",
                raw_files={ref: f"results/job-chunk/snap/{ref}" for ref in refs},
            )

    persisted: dict[str, object] = {}
    monkeypatch.setattr(
        success_finalization_mod,
        "persist_job_metadata_updates",
        lambda **kw: persisted.update(kw["metadata_updates"]),
    )
    success_finalization_mod._upload_result_package(
        result_package=ParseResultPackage(
            artifact=ParseArtifact(add_dir=str(result_dir), dataframe=pd.DataFrame()),
            chunks=[
                {
                    "chunk_id": "page-1",
                    "type": "page",
                    "content": "page body",
                    "path": "source.docx/p1",
                    "metadata": {"page_nums": [1]},
                }
            ],
        ),
        generated_package=GeneratedResultPackage(
            zip_file_path=str(zip_path),
            checksum_value="abc",
            statistics={},
            zip_size=3,
        ),
        job_id="job-chunk",
        result_storage_factory=FakeStorage,
        job_context=SimpleNamespace(
            job_metadata={
                "parse_track": "chunk",
                "ingestion_mode": "progressive",
                "source_file_name": "source.docx",
            }
        ),
    )
    assert persisted["enrichment_status"] == "text"
    assert persisted["chunk_progressive_artifact_refs"] == [
        RENDERED_PDF_REL,
        PAGE_TEXT_CACHE_REL,
    ]
    assert persisted["result_raw_prefix"] == "results/job-chunk/"
    assert "page_memory_phase_b" not in persisted
    assert uploaded == [RENDERED_PDF_REL, PAGE_TEXT_CACHE_REL]


def test_chunk_final_recovery_skips_complete_and_fresh_claims() -> None:
    from shared.services.jobs.lifecycle.scope_enrichments import (
        _should_skip_chunk_final_recovery,
    )
    from shared.utils.utc_now import utc_now_naive

    now = utc_now_naive()
    assert _should_skip_chunk_final_recovery(doc_meta={}, now=now) is True
    complete = ProgressiveIngestionState(
        enhancement=EnhancementProgress(status="complete"),
        stages={"finalization": {"status": "complete", "attempts": 1}},
    )
    assert (
        _should_skip_chunk_final_recovery(
            doc_meta={"progressive": complete.model_dump(mode="json")},
            now=now,
        )
        is True
    )
    pending = ProgressiveIngestionState(
        stages={"finalization": {"status": "pending", "attempts": 0}},
    )
    assert (
        _should_skip_chunk_final_recovery(
            doc_meta={"progressive": pending.model_dump(mode="json")},
            now=now,
        )
        is True
    )
    requested = ProgressiveIngestionState(
        stages={"finalization": {"status": "running", "attempts": 0}},
    )
    assert (
        _should_skip_chunk_final_recovery(
            doc_meta={"progressive": requested.model_dump(mode="json")},
            now=now,
        )
        is False
    )
    running = ProgressiveIngestionState(
        enhancement=EnhancementProgress(status="running"),
        stages={
            "finalization": {
                "status": "running",
                "attempts": 1,
                "claimed_at": now.isoformat(),
            }
        },
    )
    assert (
        _should_skip_chunk_final_recovery(
            doc_meta={"progressive": running.model_dump(mode="json")},
            now=now,
        )
        is True
    )
    stale = ProgressiveIngestionState(
        enhancement=EnhancementProgress(status="running"),
        stages={
            "finalization": {
                "status": "running",
                "attempts": 1,
                "claimed_at": (now - timedelta(hours=2)).isoformat(),
            }
        },
    )
    assert (
        _should_skip_chunk_final_recovery(
            doc_meta={"progressive": stale.model_dump(mode="json")},
            now=now,
        )
        is False
    )


def test_enqueue_chunk_final_sends_named_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core import celery_app as celery_app_mod
    from shared.services.jobs.lifecycle.scope_enrichments import (
        CHUNK_FINAL_TASK_NAME,
        NORMAL_ENRICHMENT_QUEUE,
        enqueue_chunk_final_after_commit,
    )

    sent: list[tuple[str, list[object], str | None]] = []

    class FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            queue: str | None = None,
            **_kwargs: object,
        ) -> None:
            sent.append((name, list(args or []), queue))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: FakeCelery())
    assert enqueue_chunk_final_after_commit("rev_chunk_1") is True
    assert sent == [
        (CHUNK_FINAL_TASK_NAME, ["rev_chunk_1"], NORMAL_ENRICHMENT_QUEUE)
    ]


def test_chunk_final_orchestration_replaces_revision_then_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion import chunk_enrichment_service as service
    from app.services.document_ingestion.chunk_enrichment_service import (
        _ChunkFinalTarget,
        execute_chunk_final_enrichment,
    )

    parsed = pd.DataFrame(
        [
            {
                "content": "final section body",
                "path": "notes.md/Overview",
                "type": "PTXT",
                "page_nums": "",
            }
        ]
    )
    snapshot = StoredSnapshot(
        snapshot_id="snapshot_final",
        stage="final",
        job_id="job_chunk",
        job_result_id="rev_chunk",
        document_id="doc_chunk",
        checksum="a" * 64,
        archive_key="results/job_chunk/final.zip",
    )
    calls: list[str] = []
    target = _ChunkFinalTarget(
        document_id="doc_chunk",
        job_id="job_chunk",
        user_id="u1",
        namespace="default",
        source_file_name="notes.md",
        s3_key="uploads/notes.md",
        raw_prefix="results/job_chunk/",
        job_metadata={
            "parse_track": "chunk",
            "ingestion_mode": "progressive",
        },
    )

    monkeypatch.setattr(service, "_load_chunk_final_target", lambda **_: target)
    monkeypatch.setattr(service, "_claim_progressive_stage", lambda **_: "claim-1")
    monkeypatch.setattr(service, "_download_original_source", lambda **_: str(tmp_path / "notes.md"))
    monkeypatch.setattr(service, "_download_resume_artifacts", lambda **_: str(tmp_path))
    monkeypatch.setattr(
        service,
        "_run_full_chunk_parse",
        lambda **_: (parsed, str(tmp_path)),
    )
    monkeypatch.setattr(service, "_build_section_summaries", lambda **_: {"notes.md/Overview": "s"})

    def capture_publish(**kwargs: Any) -> None:
        calls.append("publish")
        assert kwargs["chunks"][0]["content"] == "final section body"
        assert kwargs["job_result_id"] == "rev_chunk"

    def capture_upload(**_kwargs: Any) -> tuple[StoredSnapshot, int]:
        calls.append("upload")
        return snapshot, 12

    def capture_commit(**kwargs: Any) -> None:
        calls.append("commit")
        assert kwargs["snapshot"].stage == "final"
        assert kwargs["claim_token"] == "claim-1"

    monkeypatch.setattr(service, "_publish_final_revision", capture_publish)
    monkeypatch.setattr(service, "_upload_final_package", capture_upload)
    monkeypatch.setattr(service, "_commit_final_snapshot", capture_commit)
    monkeypatch.setattr(service, "init_llm_overrides", lambda _cfg: None)
    monkeypatch.setattr(service, "init_token_tracker", lambda: None)
    monkeypatch.setattr(service, "get_current_token_tracker", lambda: None)
    monkeypatch.setattr(service, "cleanup_llm_overrides", lambda: None)
    monkeypatch.setattr(service, "cleanup_token_tracker", lambda: None)

    result = execute_chunk_final_enrichment(job_result_id="rev_chunk")
    assert result["status"] == "success"
    assert result["snapshot_id"] == "snapshot_final"
    assert calls == ["publish", "upload", "commit"]


def test_sweeper_reenqueues_chunk_final_not_structure(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import create_engine, text, update
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from shared.core import celery_app as celery_app_mod
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.scope_enrichments import (
        BUILD_STRUCTURE_TASK_NAME,
        CHUNK_FINAL_TASK_NAME,
        sweep_progressive_enrichments_sync,
    )
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state,
        read_progressive_state,
        write_progressive_state,
    )
    from shared.testing.contract_runtime import get_contract_database_url
    from shared.utils.utc_now import utc_now_naive

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    sent: list[tuple[str, list[object]]] = []

    class FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            **_kwargs: object,
        ) -> None:
            sent.append((name, list(args or [])))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: FakeCelery())
    stale = utc_now_naive() - timedelta(hours=1)
    checksum = "b" * 64
    try:
        with Session(engine) as session:
            session.execute(
                text(
                    'INSERT INTO "user" (id, name, email) '
                    "VALUES (:id, :name, :email) ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": "chunk-sweep-user",
                    "name": "Chunk Sweep",
                    "email": "chunk-sweep@test.local",
                },
            )
            job = Job(
                job_id="job_chunk_sweep",
                user_id="chunk-sweep-user",
                job_type="document_ingestion",
                status="done",
                source_type="file",
                webhook_enabled=False,
                job_metadata={
                    "namespace": "ns-chunk-sweep",
                    "parse_track": "chunk",
                    "ingestion_mode": "progressive",
                    "source_file_name": "notes.md",
                    "enrichment_status": "text",
                },
            )
            session.add(job)
            session.flush()
            result = JobResult(
                id="rev_chunk_sweep",
                job_id="job_chunk_sweep",
                delivery_mode="url",
                result_s3_key="results/job_chunk_sweep.zip",
                result_size=10,
                inline_payload={"checksum": checksum},
            )
            session.add(result)
            session.flush()
            session.add(
                Document(
                    document_id="doc_chunk_sweep",
                    user_id="chunk-sweep-user",
                    namespace="ns-chunk-sweep",
                    status="active",
                    current_job_result_id="rev_chunk_sweep",
                    source_file_name="notes.md",
                    parse_track="chunk",
                )
            )
            session.flush()
            result.document_id = "doc_chunk_sweep"
            result.document_metadata = {
                "progressive": initialize_baseline_state(
                    snapshot=StoredSnapshot(
                        snapshot_id="snapshot_base",
                        stage="baseline",
                        job_id="job_chunk_sweep",
                        job_result_id="rev_chunk_sweep",
                        document_id="doc_chunk_sweep",
                        checksum=checksum,
                        archive_key="results/job_chunk_sweep.zip",
                    ),
                    missing_pages=[],
                ).model_dump(mode="json")
            }
            session.flush()
            session.execute(
                update(Job)
                .where(Job.job_id == "job_chunk_sweep")
                .values(updated_at=stale)
            )
            session.commit()

        sweep_progressive_enrichments_sync()
        assert sent == []
        assert BUILD_STRUCTURE_TASK_NAME not in {name for name, _args in sent}

        with Session(engine) as session:
            result = session.get(JobResult, "rev_chunk_sweep")
            state = read_progressive_state(result.document_metadata)
            stages = dict(state.stages)
            stages["finalization"] = {**stages.get("finalization", {}), "status": "running"}
            state.stages = stages
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            session.execute(
                update(Job)
                .where(Job.job_id == "job_chunk_sweep")
                .values(updated_at=stale)
            )
            session.commit()

        sweep_progressive_enrichments_sync()
        assert [name for name, _args in sent] == [CHUNK_FINAL_TASK_NAME]
        assert sent[0][1] == ["rev_chunk_sweep"]

        sent.clear()
        from app.services.page_memory.scope_enrichment_service import (
            execute_progressive_recovery,
        )

        recovery_outcome = execute_progressive_recovery(job_result_id="rev_chunk_sweep")
        assert recovery_outcome == {
            "status": "recovered",
            "job_result_id": "rev_chunk_sweep",
        }
        assert [name for name, _args in sent] == [CHUNK_FINAL_TASK_NAME]
        assert sent[0][1] == ["rev_chunk_sweep"]
    finally:
        engine.dispose()


def test_chunk_progressive_markdown_zip_package_does_not_require_source_pdf(
    tmp_path: Path,
) -> None:
    from zipfile import ZipFile

    from shared.services.storage.zip_result_service import ZipResultService

    add_dir = tmp_path / "notes"
    add_dir.mkdir()
    zip_path, checksum, stats, zip_size = ZipResultService().generate_zip_package(
        job_id="job_md_prog",
        chunks=[
            {
                "chunk_id": "c1",
                "type": "text",
                "content": "Markdown progressive baseline text",
                "path": "notes.md/Intro",
                "metadata": {"page_nums": []},
            }
        ],
        add_dir=str(add_dir),
        source_file_name="notes.md",
        data_id=None,
        job_metadata={"ingestion_mode": "progressive", "parse_track": "chunk"},
        temp_dir=str(tmp_path),
    )

    assert zip_size > 0
    assert checksum["value"]
    assert stats["text_chunks"] == 1
    with ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert {"chunks.json", "doc_nav.json", "manifest.json"} <= names
        assert "source.pdf" not in names


def test_download_original_source_reuses_resumed_markdown_file_without_s3_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion.chunk_enrichment_service import (
        _ChunkFinalTarget,
        _download_original_source,
    )
    from shared.services.storage.job_file_storage import JobFileStorage

    add_dir = tmp_path / "output" / "notes"
    input_dir = tmp_path / "input"
    add_dir.mkdir(parents=True)
    input_dir.mkdir(parents=True)
    resumed = add_dir / "notes.md"
    resumed.write_text("# Resumed\nBody\n", encoding="utf-8")

    def forbid_s3_download(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("S3 upload download must not run when resume source exists")

    monkeypatch.setattr(
        JobFileStorage,
        "download_upload_to_temp",
        forbid_s3_download,
    )
    target = _ChunkFinalTarget(
        document_id="doc_1",
        job_id="job_1",
        user_id="u1",
        namespace="default",
        source_file_name="notes.md",
        s3_key="uploads/notes.md",
        raw_prefix="results/job_1/",
        job_metadata={"parse_track": "chunk", "ingestion_mode": "progressive"},
    )
    resolved = _download_original_source(
        target=target,
        input_dir=str(input_dir),
        add_dir=str(add_dir),
    )
    assert Path(resolved).is_file()
    assert "# Resumed" in Path(resolved).read_text(encoding="utf-8")
    assert not resumed.exists()


def test_promote_on_read_enqueues_chunk_final_once(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from shared.core import celery_app as celery_app_mod
    from shared.core.database import get_db_context
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.scope_enrichments import (
        BUILD_STRUCTURE_TASK_NAME,
        CHUNK_FINAL_TASK_NAME,
        ENRICH_SCOPE_TASK_NAME,
        promote_scopes_on_read,
    )
    from shared.services.jobs.progressive_ingestion import initialize_baseline_state
    from shared.testing.contract_runtime import get_contract_database_url

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    sent: list[tuple[str, list[object]]] = []

    class FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            **_kwargs: object,
        ) -> None:
            sent.append((name, list(args or [])))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: FakeCelery())
    checksum = "c" * 64
    try:
        with Session(engine) as session:
            session.execute(
                text(
                    'INSERT INTO "user" (id, name, email) '
                    "VALUES (:id, :name, :email) ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": "chunk-promote-user",
                    "name": "Chunk Promote",
                    "email": "chunk-promote@test.local",
                },
            )
            job = Job(
                job_id="job_chunk_promote",
                user_id="chunk-promote-user",
                job_type="document_ingestion",
                status="done",
                source_type="file",
                webhook_enabled=False,
                job_metadata={
                    "namespace": "ns-chunk-promote",
                    "parse_track": "chunk",
                    "ingestion_mode": "progressive",
                    "source_file_name": "notes.md",
                    "enrichment_status": "text",
                },
            )
            session.add(job)
            session.flush()
            result = JobResult(
                id="rev_chunk_promote",
                job_id="job_chunk_promote",
                delivery_mode="url",
                result_s3_key="results/job_chunk_promote.zip",
                result_size=10,
                inline_payload={"checksum": checksum},
                document_metadata={
                    "enrichment_status": "text",
                    "progressive": initialize_baseline_state(
                        snapshot=StoredSnapshot(
                            snapshot_id="snapshot_promote",
                            stage="baseline",
                            job_id="job_chunk_promote",
                            job_result_id="rev_chunk_promote",
                            document_id="doc_chunk_promote",
                            checksum=checksum,
                            archive_key="results/job_chunk_promote.zip",
                        ),
                        missing_pages=[],
                    ).model_dump(mode="json"),
                },
            )
            session.add(result)
            session.flush()
            session.add(
                Document(
                    document_id="doc_chunk_promote",
                    user_id="chunk-promote-user",
                    namespace="ns-chunk-promote",
                    status="active",
                    current_job_result_id="rev_chunk_promote",
                    source_file_name="notes.md",
                    parse_track="chunk",
                )
            )
            session.flush()
            result.document_id = "doc_chunk_promote"
            session.commit()

        row = {
            "document_id": "doc_chunk_promote",
            "job_result_id": "rev_chunk_promote",
            "chunk_id": "c1",
            "chunk_type": "text",
            "content": "Visible baseline sentence.",
        }

        async def _promote() -> None:
            async with get_db_context() as db:
                await promote_scopes_on_read(db, [row])

        asyncio.run(_promote())
        assert [name for name, _args in sent] == [CHUNK_FINAL_TASK_NAME]
        assert sent[0][1] == ["rev_chunk_promote"]
        assert ENRICH_SCOPE_TASK_NAME not in {name for name, _args in sent}
        assert BUILD_STRUCTURE_TASK_NAME not in {name for name, _args in sent}

        asyncio.run(_promote())
        assert [name for name, _args in sent] == [CHUNK_FINAL_TASK_NAME]

        async def _promote_empty() -> None:
            async with get_db_context() as db:
                await promote_scopes_on_read(
                    db,
                    [
                        {
                            "document_id": "doc_chunk_promote",
                            "job_result_id": "rev_chunk_promote",
                            "chunk_id": "c2",
                            "chunk_type": "text",
                            "content": "",
                        }
                    ],
                )

        asyncio.run(_promote_empty())
        assert [name for name, _args in sent] == [CHUNK_FINAL_TASK_NAME]
    finally:
        engine.dispose()
