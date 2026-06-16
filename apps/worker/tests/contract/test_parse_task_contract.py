from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from support.worker_parse_contract import WorkerParseContract

_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_FIXTURES_ROOT: Path = _REPO_ROOT / "apps" / "worker" / "tests" / "fixtures"
_SAMPLE_XLSX_PATH: Path = _FIXTURES_ROOT / "sample_100rows.xlsx"


def _write_blank_pdf(file_path: Path, page_count: int) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)

    with file_path.open("wb") as pdf_file:
        writer.write(pdf_file)


def test_parse_task_should_process_uploaded_file_through_real_contract_boundaries(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=False)

    job = contract.create_file_job(
        source_file_name="contract-real.xlsx",
        job_id_prefix="job_parse_real",
    )
    contract.upload_source_file(
        local_file_path=_SAMPLE_XLSX_PATH,
        s3_key=job["s3_key"],
    )

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.successful()
    assert celery_result.result["status"] == "success"
    assert celery_result.result["job_id"] == job["job_id"]
    assert celery_result.result["delivery_mode"] == "url"
    assert celery_result.result["result_s3_key"] == contract.storage.build_result_zip_key(
        job_id=job["job_id"]
    )

    observed = contract.observe_successful_job(job["job_id"])
    job_row = observed["job"]
    result_row = observed["result"]
    job_chunks = observed["job_chunks"]
    document_chunks = observed["document_chunks"]

    assert job_row["status"] == "done"
    assert job_row["billing_status"] == "skipped"
    assert job_row["page_count"] and job_row["page_count"] > 0
    assert job_row["error_message"] is None

    assert result_row["document_id"]
    assert result_row["result_s3_key"] == contract.storage.build_result_zip_key(
        job_id=job["job_id"]
    )
    assert result_row["result_size"] and result_row["result_size"] > 0

    assert len(job_chunks) > 0
    assert len(document_chunks) == len(job_chunks)
    assert observed["document_sections_count"] > 0
    assert all(row["chunk_type"] == "table" for row in job_chunks)
    assert any("tables/" in str(row["path"]) for row in job_chunks)

    assert contract.get_task_status(job["job_id"]) == "done"
    task_progress = contract.get_task_progress(job["job_id"])
    assert task_progress["progress"] == 100
    assert task_progress["message"] == "Task complete!"

    result_file_info = contract.verify_result_zip_object(result_row["result_s3_key"])
    assert result_file_info["exists"] is True
    assert result_file_info["size"] == result_row["result_size"]

    result_zip = contract.read_result_zip(
        result_s3_key=result_row["result_s3_key"],
        tmp_path=tmp_path,
    )
    assert {"chunks.json", "doc_nav.json", "manifest.json"}.issubset(
        result_zip["members"]
    )
    assert any(member.startswith("tables/") for member in result_zip["members"])

    chunks_payload = result_zip["chunks"]
    assert len(chunks_payload["chunks"]) == len(job_chunks)
    assert all(chunk["type"] == "table" for chunk in chunks_payload["chunks"])
    assert any(
        "Table summary:" in chunk["content"] for chunk in chunks_payload["chunks"]
    )

    manifest_payload = result_zip["manifest"]
    assert manifest_payload["source_file_name"] == job["source_file_name"]
    assert manifest_payload["statistics"]["total_chunks"] == len(job_chunks)

    assert contract.find_task_workspaces(tmp_path, job["job_id"]) == []


def test_parse_task_should_charge_user_when_billing_is_enabled(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=True)

    job = contract.create_file_job(
        source_file_name="contract-billing.xlsx",
        job_id_prefix="job_parse_billing",
    )
    contract.upload_source_file(
        local_file_path=_SAMPLE_XLSX_PATH,
        s3_key=job["s3_key"],
    )

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.successful()
    observed = contract.observe_successful_job(job["job_id"])
    job_row = observed["job"]
    expected_charge = job_row["page_count"] * int(
        contract.settings.MICRO_DOLLARS_PER_PAGE
    )

    assert job_row["status"] == "done"
    assert job_row["billing_status"] == "charged"
    assert job_row["credits_charged"] == expected_charge

    billing = contract.observe_user_billing(job["user_id"])
    expected_initial_balance = int(contract.settings.FREE_PLAN_INITIAL_CREDITS) * 1_000_000
    assert billing["balance"] == expected_initial_balance - expected_charge
    assert billing["transaction_types"] == ["initial_grant", "usage"]

    assert contract.observe_job_state_transitions(job["job_id"]) == [
        ("start_processing", "running"),
        ("mark_completed", "done"),
    ]


def test_parse_task_should_export_full_result_when_same_content_was_already_published(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=False)

    user_id = f"worker-contract-user-{uuid4().hex[:12]}"
    first_job = contract.create_file_job(
        user_id=user_id,
        source_file_name="contract-original.xlsx",
        job_id_prefix="job_parse_original",
    )
    second_job = contract.create_file_job(
        user_id=user_id,
        source_file_name="contract-duplicate.xlsx",
        job_id_prefix="job_parse_duplicate",
    )
    for job in [first_job, second_job]:
        contract.upload_source_file(
            local_file_path=_SAMPLE_XLSX_PATH,
            s3_key=job["s3_key"],
        )

    first_result = contract.enqueue_parse_task(
        job_id=first_job["job_id"],
        user_id=user_id,
    )
    second_result = contract.enqueue_parse_task(
        job_id=second_job["job_id"],
        user_id=user_id,
    )

    assert first_result.successful()
    assert second_result.successful()

    observed = contract.observe_successful_job(second_job["job_id"])
    result_row = observed["result"]
    job_chunks = observed["job_chunks"]
    document_chunks = observed["document_chunks"]
    result_zip = contract.read_result_zip(
        result_s3_key=result_row["result_s3_key"],
        tmp_path=tmp_path,
    )

    assert len(job_chunks) > 0
    assert len(document_chunks) == len(job_chunks)
    assert len(result_zip["chunks"]["chunks"]) == len(job_chunks)
    assert any(member.startswith("tables/") for member in result_zip["members"])
    assert "chunk_overlap" not in dict(result_row["document_metadata"] or {})


def test_parse_task_should_initialize_billing_once_for_concurrent_parse_tasks(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=True)

    user_id = f"worker-contract-user-{uuid4().hex[:12]}"
    jobs = [
        contract.create_file_job(
            user_id=user_id,
            source_file_name=f"contract-concurrent-{index}.xlsx",
            job_id_prefix=f"job_parse_concurrent_{index}",
        )
        for index in range(2)
    ]
    for job in jobs:
        contract.upload_source_file(
            local_file_path=_SAMPLE_XLSX_PATH,
            s3_key=job["s3_key"],
        )

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        celery_results = list(
            executor.map(
                lambda job: contract.enqueue_parse_task(
                    job_id=job["job_id"],
                    user_id=user_id,
                ),
                jobs,
            )
        )

    assert all(result.successful() for result in celery_results)
    observed_jobs = [
        contract.observe_successful_job(job["job_id"])["job"] for job in jobs
    ]
    assert all(row["billing_status"] == "charged" for row in observed_jobs)

    billing = contract.observe_user_billing(user_id)
    expected_total_charge = sum(row["credits_charged"] for row in observed_jobs)
    expected_initial_balance = int(contract.settings.FREE_PLAN_INITIAL_CREDITS) * 1_000_000
    assert billing["balance"] == expected_initial_balance - expected_total_charge
    assert billing["transaction_counts"] == {
        "initial_grant": 1,
        "usage": len(jobs),
    }
    assert billing["system_grant_payment_count"] == 1


def test_parse_task_should_skip_terminal_job_without_creating_outputs(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)

    job = contract.create_file_job(
        source_file_name="contract-skip.xlsx",
        status="done",
        billing_status="charged",
        job_id_prefix="job_skip",
    )

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.successful()
    assert celery_result.result == {
        "status": "skipped",
        "job_id": job["job_id"],
        "reason": "job_already_terminal",
    }
    assert contract.get_task_progress(job["job_id"]) == {}
    assert contract.find_task_workspaces(tmp_path, job["job_id"]) == []

    job_row = contract.observe_job_status(job["job_id"])
    assert job_row["status"] == "done"
    assert job_row["billing_status"] == "charged"
    assert contract.count_job_results(job["job_id"]) == 0


def test_parse_task_should_mark_failed_and_cleanup_when_uploaded_source_is_missing(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=False)

    job = contract.create_file_job(
        source_file_name="contract-missing-source.xlsx",
        job_id_prefix="job_missing",
    )

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.failed()
    assert contract.find_task_workspaces(tmp_path, job["job_id"]) == []

    job_row = contract.observe_job_status(job["job_id"])
    assert job_row["status"] == "failed"
    assert job_row["billing_status"] in {"pending", "skipped"}
    assert job_row["error_code"]
    assert job_row["error_message"]
    assert contract.count_job_results(job["job_id"]) == 0


def test_parse_task_should_refund_charged_job_when_uploaded_file_cannot_be_parsed(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=True)

    invalid_xlsx_path = tmp_path / "invalid.xlsx"
    invalid_xlsx_path.write_bytes(b"this is not an xlsx workbook")
    job = contract.create_file_job(
        source_file_name="contract-invalid.xlsx",
        job_id_prefix="job_invalid_parse",
    )
    contract.upload_source_file(
        local_file_path=invalid_xlsx_path,
        s3_key=job["s3_key"],
    )

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.failed()
    assert contract.find_task_workspaces(tmp_path, job["job_id"]) == []

    job_row = contract.observe_job_status(job["job_id"])
    assert job_row["status"] == "failed"
    assert job_row["billing_status"] == "refunded"
    assert job_row["credits_charged"] == int(contract.settings.MICRO_DOLLARS_PER_PAGE)
    assert contract.count_job_results(job["job_id"]) == 0

    billing = contract.observe_user_billing(job["user_id"])
    expected_initial_balance = int(contract.settings.FREE_PLAN_INITIAL_CREDITS) * 1_000_000
    assert billing["balance"] == expected_initial_balance
    assert billing["transaction_types"] == ["initial_grant", "usage", "refund"]
    assert contract.observe_job_state_transitions(job["job_id"]) == [
        ("start_processing", "running"),
        ("mark_failed", "failed"),
    ]


def test_should_reject_pdf_when_page_count_exceeds_configured_limit(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=True)

    max_pdf_page_limit: int = 1
    actual_page_count: int = 2
    source_file_name: str = "oversized-contract.pdf"
    pdf_path = tmp_path / source_file_name
    _write_blank_pdf(pdf_path, actual_page_count)

    contract.use_pdf_page_limit(monkeypatch, max_pdf_page_limit)
    contract.use_oversized_pdf_shard_enabled(monkeypatch, enabled=False)
    job = contract.create_file_job(
        source_file_name=source_file_name,
        job_id_prefix="job_pdf_page_limit",
    )
    contract.upload_source_file(
        local_file_path=pdf_path,
        s3_key=job["s3_key"],
    )

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.failed()
    assert contract.find_task_workspaces(tmp_path, job["job_id"]) == []

    metadata = contract.get_job_metadata(job["job_id"])
    assert metadata["page_count"] == actual_page_count
    assert metadata["billing_status"] == "skipped"

    job_row = contract.observe_job_status(job["job_id"])

    assert job_row["status"] == "failed"
    assert job_row["billing_status"] == "skipped"
    assert job_row["page_count"] == actual_page_count
    assert job_row["credits_charged"] == 0
    assert job_row["error_code"] == "INVALID_ARGUMENT"
    assert (
        job_row["error_message"]
        == "Document too large: 2 pages exceeds the 1-page limit. Please split the document and upload it in smaller parts."
    )
    assert metadata["error_details"] == {
        "violations": [
            {
                "field": "page_count",
                "description": "PDF has 2 pages, limit is 1",
            }
        ]
    }

    billing = contract.observe_user_billing(job["user_id"])
    assert billing["balance"] is None
    assert billing["transaction_types"] == []
    assert billing["transaction_counts"] == {}
    assert billing["system_grant_payment_count"] == 0


def test_should_reject_pdf_when_page_count_exceeds_soft_limit(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = WorkerParseContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.use_billing(monkeypatch, is_enabled=True)

    max_pdf_page_limit = 1
    soft_limit = 2
    actual_page_count = 3
    source_file_name = "ultra-long-contract.pdf"
    pdf_path = tmp_path / source_file_name
    _write_blank_pdf(pdf_path, actual_page_count)

    contract.use_pdf_page_limit(monkeypatch, max_pdf_page_limit)
    contract.use_oversized_pdf_soft_limit(monkeypatch, soft_limit)
    contract.use_oversized_pdf_shard_enabled(monkeypatch, enabled=True)
    job = contract.create_file_job(
        source_file_name=source_file_name,
        job_id_prefix="job_pdf_soft_limit",
    )
    contract.upload_source_file(local_file_path=pdf_path, s3_key=job["s3_key"])

    celery_result = contract.enqueue_parse_task(
        job_id=job["job_id"],
        user_id=job["user_id"],
    )

    assert celery_result.failed()
    metadata = contract.get_job_metadata(job["job_id"])
    job_row = contract.observe_job_status(job["job_id"])

    assert job_row["status"] == "failed"
    assert job_row["billing_status"] == "skipped"
    assert job_row["credits_charged"] == 0
    assert job_row["error_code"] == "INVALID_ARGUMENT"
    assert (
        job_row["error_message"]
        == "This document has 3 pages. Processing ultra-long documents over 2 pages requires dedicated resources. Please contact support for assistance."
    )
    assert metadata["error_details"] == {
        "violations": [
            {
                "field": "page_count",
                "description": "PDF has 3 pages, soft limit is 2",
            }
        ]
    }


def test_oversized_pdf_shard_failure_preserves_processing_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("TMP_PATH", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET_NAME", "test-uploads")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_TEMP_PATH", str(tmp_path))

    from app.services.document_agent.manifest import (
        H1BoundaryResult,
        PageAnatomyMap,
        Shard,
        ShardPlan,
        TocResult,
    )
    from app.services.document_parser.formats.pdf import parser as pdf_parser
    from app.services.document_parser.profiling.profile_model import ParserDocumentProfile
    from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
    from shared.core.exceptions.domain_exceptions import PDFParsingException

    monkeypatch.setattr(pdf_parser.settings, "MAX_PDF_PAGE_LIMIT", 1)

    def _fail_oversized_parse(*args, **kwargs):
        raise RuntimeError("MinerU shard 0 failed")

    monkeypatch.setattr(pdf_parser, "_parse_pdf_via_shards", _fail_oversized_parse)

    with pytest.raises(PDFParsingException) as exc_info:
        pdf_parser.parse_pdfs(
            str(tmp_path / "source.pdf"),
            "source.pdf",
            str(tmp_path),
            {},
            profile=ParserDocumentProfile(
                file_type="pdf",
                category="generic document",
                routing_category=PdfRoutingCategory.GENERIC,
                page_count=2,
                anatomy=PageAnatomyMap(
                    job_id="job-oversized-fail",
                    file_path=str(tmp_path / "source.pdf"),
                    page_count=2,
                    page_features=[],
                    page_labels=[],
                    toc_result=TocResult(method="none"),
                    h1_result=H1BoundaryResult(method="none"),
                    shard_plan=ShardPlan(
                        enabled=True,
                        reason="too_large",
                        shards=[
                            Shard(
                                shard_index=0,
                                page_start=1,
                                page_end=2,
                                page_offset=0,
                                anchor_type="forced_max_size",
                                anchor_evidence="fixture",
                                confidence=1.0,
                            )
                        ],
                    ),
                ),
            ),
        )

    assert exc_info.value.details["reason"] == "OVERSIZED_SHARD_PIPELINE_FAILED"
    assert "MinerU shard 0 failed" in exc_info.value.user_message
    assert "exceeds the 1-page direct processing limit" in exc_info.value.user_message


def test_oversized_pdf_happy_path_uses_shard_pipeline_without_external_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("TMP_PATH", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET_NAME", "test-uploads")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_TEMP_PATH", str(tmp_path))

    from app.services.document_agent.manifest import (
        H1BoundaryResult,
        PageAnatomyMap,
        Shard,
        ShardPlan,
        TocResult,
    )
    from app.services.document_parser.formats.pdf import parser as pdf_parser
    from app.services.document_parser.profiling.profile_model import ParserDocumentProfile
    from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory

    pdf_path = tmp_path / "oversized.pdf"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _write_blank_pdf(pdf_path, page_count=3)

    calls: dict[str, object] = {}
    parse_s3_keys: list[str | None] = []
    deleted_s3_keys: list[str] = []

    class _FakeJobFileStorage:
        def delete_upload_file(self, storage_key: str) -> bool:
            deleted_s3_keys.append(storage_key)
            return True

    anatomy = PageAnatomyMap(
        job_id="job-oversized",
        file_path=str(pdf_path),
        page_count=3,
        page_features=[],
        page_labels=[],
        toc_result=TocResult(toc_pages=[1], method="vlm_batch"),
        h1_result=H1BoundaryResult(method="toc_grep"),
        shard_plan=ShardPlan(
            enabled=True,
            reason="too_large",
            shards=[
                Shard(
                    shard_index=0,
                    page_start=1,
                    page_end=2,
                    page_offset=0,
                    anchor_type="h1_boundary",
                    anchor_evidence="Chapter 1",
                    confidence=0.9,
                ),
                Shard(
                    shard_index=1,
                    page_start=3,
                    page_end=3,
                    page_offset=2,
                    anchor_type="h1_boundary",
                    anchor_evidence="Chapter 2",
                    confidence=0.9,
                ),
            ],
        ),
        toc_hierarchies=[{"toc_tree": {"Chapter 1": {}, "Chapter 2": {}}}],
    )

    def _fake_split_pdf(pdf_path_arg, shards, work_dir, exclude_pages=None):
        calls["exclude_pages"] = exclude_pages
        paths = []
        for shard in shards:
            shard_path = Path(work_dir) / f"shard_{shard.shard_index}.pdf"
            shard_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            paths.append(str(shard_path))
        return paths, None

    def _fake_parse_via_full(shard_pdf, shard_filename, shard_out, s3_key=None):
        parse_s3_keys.append(s3_key)
        shard_index = 0 if "shard0" in shard_filename else 1
        lines_by_shard = {
            0: ["# Chapter 1", "Shard one body."],
            1: ["# Chapter 2", "Shard two body."],
        }
        Path(shard_out).mkdir(parents=True, exist_ok=True)
        (Path(shard_out) / "full.md").write_text(
            "\n".join(lines_by_shard[shard_index]),
            encoding="utf-8",
        )

    def _identity_eval_md_headings(
        md_lines,
        source_type,
        toc_hierarchies=None,
        smart_parse=True,
        model_name=None,
        output_dir=None,
        layout_json_path=None,
        is_first_shard=True,
    ):
        calls.setdefault("heading_dirs", []).append(output_dir)
        return list(md_lines)

    monkeypatch.setattr(pdf_parser.settings, "MAX_PDF_PAGE_LIMIT", 2)
    monkeypatch.setattr(pdf_parser.settings, "MINERU_SHARD_CONCURRENCY", 1)
    monkeypatch.setattr(
        "app.services.document_parser.formats.pdf.shard_splitter.split_pdf",
        _fake_split_pdf,
    )
    monkeypatch.setattr(pdf_parser, "parse_via_full", _fake_parse_via_full)
    monkeypatch.setattr(
        "app.services.document_parser.formats.markdown.parser.eval_md_headings",
        _identity_eval_md_headings,
    )
    monkeypatch.setattr(pdf_parser, "JobFileStorage", _FakeJobFileStorage)

    df = pdf_parser.parse_pdfs(
        str(pdf_path),
        "oversized.pdf",
        str(output_dir),
        {
            "smart_title_parse": False,
            "summary_image": False,
            "summary_table": False,
            "summary_txt": False,
            "stopwords": [],
            "model_name": "mock-model",
            "hierarchy_model_name": "mock-model",
        },
        profile=ParserDocumentProfile(
            file_type="pdf",
            category="generic document",
            routing_category=PdfRoutingCategory.GENERIC,
            page_count=3,
            anatomy=anatomy,
        ),
        relative_root="oversized.pdf",
        s3_key="uploads/job-oversized.pdf",
        job_id="job-oversized",
    )

    assert calls["exclude_pages"] == {1}
    assert len(calls["heading_dirs"]) == 2
    expected_s3_keys = [
        "tmp/mineru-shards/job-oversized/shard_0.pdf",
        "tmp/mineru-shards/job-oversized/shard_1.pdf",
    ]
    assert parse_s3_keys == expected_s3_keys
    assert deleted_s3_keys == expected_s3_keys
    assert not (output_dir / "_shards").exists()
    assert list(df["type"]) == ["PTXT", "PTXT"]
    assert list(df["content"]) == ["Shard one body.", "Shard two body."]
    assert list(df["path"]) == [
        "oversized.pdf/Chapter 1",
        "oversized.pdf/Chapter 2",
    ]
