from __future__ import annotations

import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pandas as pd
import pytest
from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.contract_database import insert_contract_job, insert_contract_user

_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_FIXTURES_ROOT: Path = _REPO_ROOT / "apps" / "worker" / "tests" / "fixtures"
_SAMPLE_PDF_PATH: Path = _FIXTURES_ROOT / "sample_3pages.pdf"


def _build_pending_file_job_metadata(source_file_name: str) -> dict[str, Any]:
    job_metadata: dict[str, Any] = {
        "namespace": "worker-contract",
        "source_type": "file",
        "source_file_name": source_file_name,
        "kb_dir": "Default_Root",
    }
    return job_metadata


def _load_parse_task_modules() -> tuple[Any, Any, Any, Engine, Any, Any, Any]:
    import app.core.tasks.kb_tasks as kb_tasks
    import app.services.document_ingestion.processing_run as parse_job_service
    import app.services.document_parser.parse_service as parse_service
    from shared.core.database_sync import get_sync_engine
    from shared.services.redis.redis_sync_service import (
        SyncJobInfoRedisService,
        SyncJobMetadataService,
        SyncRedisServiceFactory,
    )

    return (
        kb_tasks,
        parse_service,
        parse_job_service,
        get_sync_engine(),
        SyncJobInfoRedisService,
        SyncJobMetadataService,
        SyncRedisServiceFactory,
    )


def _load_worker_settings() -> Any:
    from shared.core.config import settings

    return settings


def _save_worker_task_cache(
    *,
    job_id: str,
    user_id: str,
    s3_key: str,
    metadata: dict[str, Any],
    sync_job_info_service_cls: Any,
    sync_job_metadata_service_cls: Any,
    sync_redis_service_factory: Any,
) -> Any:
    redis_service = sync_redis_service_factory.get_service()
    sync_job_info_service = sync_job_info_service_cls(redis_service)
    sync_job_metadata_service = sync_job_metadata_service_cls(redis_service)

    sync_job_info_service.save_job_info(
        job_id,
        {
            "job_id": job_id,
            "s3_key": s3_key,
            "user_id": user_id,
            "webhook_enabled": False,
            "job_type": "kb_management",
            "source_type": "file",
        },
    )
    sync_job_metadata_service.save_metadata(job_id, metadata)
    return redis_service


def _patch_verify_upload_exists(
    monkeypatch: MonkeyPatch,
    file_info_for_storage_key: Any,
) -> None:
    from shared.services.storage.job_file_storage import JobFileStorage

    monkeypatch.setattr(
        JobFileStorage,
        "verify_upload_exists",
        lambda self, storage_key: file_info_for_storage_key(storage_key),
    )


def _find_task_workspaces(root: Path, job_id: str) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith(f"kb_task_{job_id}_")
    )


def _bind_parse_task_to_current_module(
    monkeypatch: MonkeyPatch,
    *,
    kb_tasks: Any,
) -> None:
    monkeypatch.setitem(
        kb_tasks.parse_task._orig_run.__globals__,
        "_parse",
        kb_tasks._parse,
    )
    monkeypatch.setattr(kb_tasks.parse_task, "__trace__", None, raising=False)


@pytest.mark.parametrize(
    ("billing_enabled", "expected_billing_status", "expected_transaction_types"),
    [
        (True, "charged", ["initial_grant", "usage"]),
        (False, "skipped", []),
    ],
)
def test_should_parse_a_pending_file_job_and_persist_the_published_result_state(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    billing_enabled: bool,
    expected_billing_status: str,
    expected_transaction_types: list[str],
) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "true" if billing_enabled else "false")
    (
        kb_tasks,
        parse_service,
        parse_job_service,
        engine,
        sync_job_info_service_cls,
        sync_job_metadata_service_cls,
        sync_redis_service_factory,
    ) = _load_parse_task_modules()
    settings = _load_worker_settings()

    user_id: str = f"worker-user-{uuid4().hex[:12]}"
    job_id: str = f"job_parse_success_{uuid4().hex[:12]}"
    source_file_name: str = "contract-parse.pdf"
    s3_key: str = f"uploads/{job_id}.pdf"
    text_content_with_refs: str = (
        "chunk-1 embeds [images/page-1.png] and [tables/table-1.html]"
    )
    captured_artifacts: dict[str, Any] = {}

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        job_metadata = _build_pending_file_job_metadata(source_file_name)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="pending",
            source_type="file",
            s3_key=s3_key,
            webhook_enabled=False,
            job_metadata=job_metadata,
            billing_status="pending",
        )

    redis_service = _save_worker_task_cache(
        job_id=job_id,
        user_id=user_id,
        s3_key=s3_key,
        metadata=job_metadata,
        sync_job_info_service_cls=sync_job_info_service_cls,
        sync_job_metadata_service_cls=sync_job_metadata_service_cls,
        sync_redis_service_factory=sync_redis_service_factory,
    )

    _bind_parse_task_to_current_module(monkeypatch, kb_tasks=kb_tasks)
    monkeypatch.setattr(settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "BILLING_ENABLED", billing_enabled)

    def fake_verify_s3_file_exists(storage_key: str) -> dict[str, Any]:
        return {
            "exists": storage_key == s3_key,
            "size": _SAMPLE_PDF_PATH.stat().st_size,
        }

    _patch_verify_upload_exists(monkeypatch, fake_verify_s3_file_exists)

    def fake_download_s3_file_to_temp(
        storage_key: str, file_ext: str, temp_dir: str
    ) -> str:
        assert storage_key == s3_key
        assert file_ext == ".pdf"
        downloaded_path = Path(temp_dir) / f"downloaded{file_ext}"
        shutil.copy2(_SAMPLE_PDF_PATH, downloaded_path)
        return str(downloaded_path)

    def fake_checkerboard_inject_parse(**kwargs: Any) -> tuple[str, pd.DataFrame]:
        captured_artifacts["parse_kwargs"] = kwargs
        output_dir = (
            Path(str(kwargs["output_dir"]))
            / str(kwargs["kb_dir"])
            / str(kwargs["internal_output_filename"])
        )
        images_dir = output_dir / "images"
        tables_dir = output_dir / "tables"
        images_dir.mkdir(parents=True, exist_ok=True)
        tables_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full.md").write_text("body", encoding="utf-8")
        (images_dir / "page-1.png").write_bytes(b"png")
        (tables_dir / "table-1.html").write_text("<table></table>", encoding="utf-8")

        file_root = str(kwargs["internal_output_filename"])
        parsed_rows: list[dict[str, Any]] = [
            {
                "content": text_content_with_refs,
                "path": f"Default_Root/{file_root}/公司研究/自主可控加强，寒武纪或迎来营收快速放量周期",
                "type": "text",
                "length": len(text_content_with_refs),
                "keywords": "",
                "summary": "",
                "know_id": "kid-1",
                "tokens": "",
                "connectto": json.dumps(
                    [
                        {
                            "target": "table-1",
                            "relation": "embeds",
                            "ref": "[tables/table-1.html]",
                        }
                    ]
                ),
                "addtime": "now",
                "page_nums": "1",
            },
            {
                "content": "chunk-2",
                "path": f"Default_Root/{file_root}/相关研报/要点",
                "type": "text",
                "length": 7,
                "keywords": "",
                "summary": "",
                "know_id": "kid-2",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "2",
            },
            {
                "content": "image caption",
                "path": f"Default_Root/{file_root}/images/page-1.png",
                "type": "image",
                "length": 13,
                "keywords": "",
                "summary": "",
                "know_id": "image-1",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "3",
            },
            {
                "content": "table content",
                "path": f"Default_Root/{file_root}/tables/table-1.html",
                "type": "table",
                "length": 13,
                "keywords": "",
                "summary": "",
                "know_id": "table-1",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "3",
            },
        ]
        return str(output_dir), pd.DataFrame(parsed_rows)

    class FakeResultStorage:
        def upload(self, *, job_id: str, result_dir: str, zip_file_path: str) -> Any:
            result_dir_path = Path(result_dir)
            zip_path = Path(zip_file_path)
            captured_artifacts["result_dir"] = result_dir
            captured_artifacts["zip_file_path"] = zip_file_path
            captured_artifacts["raw_entries"] = sorted(
                path.relative_to(result_dir_path).as_posix()
                for path in result_dir_path.rglob("*")
                if path.is_file()
            )
            captured_artifacts["doc_nav"] = json.loads(
                (result_dir_path / "doc_nav.json").read_text(encoding="utf-8")
            )

            with zipfile.ZipFile(zip_path) as zip_file:
                captured_artifacts["zip_entries"] = sorted(zip_file.namelist())
                captured_artifacts["zip_chunks"] = json.loads(
                    zip_file.read("chunks.json")
                )["chunks"]
                captured_artifacts["manifest"] = json.loads(
                    zip_file.read("manifest.json")
                )

            return SimpleNamespace(
                zip_key=f"results/{job_id}.zip",
                raw_prefix=f"results/{job_id}/",
                raw_files={},
            )

    monkeypatch.setattr(parse_job_service, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)
    monkeypatch.setattr(parse_job_service, "get_result_storage", lambda: FakeResultStorage())

    result = kb_tasks.parse_task.run(job_id, user_id, "kb_management")

    expected_summary = "This document includes: 公司研究, 相关研报"
    expected_connect_to = [
        {
            "target": "image-1",
            "relation": "embeds",
            "ref": "[images/page-1.png]",
            "position": {
                "start": text_content_with_refs.index("[images/page-1.png]"),
                "end": text_content_with_refs.index("[images/page-1.png]")
                + len("[images/page-1.png]"),
            },
        },
        {
            "target": "table-1",
            "relation": "embeds",
            "ref": "[tables/table-1.html]",
            "position": {
                "start": text_content_with_refs.index("[tables/table-1.html]"),
                "end": text_content_with_refs.index("[tables/table-1.html]")
                + len("[tables/table-1.html]"),
            },
        },
    ]
    expected_credits_charged = 3 * int(settings.MICRO_DOLLARS_PER_PAGE)
    expected_initial_balance = int(settings.FREE_PLAN_INITIAL_CREDITS) * 1_000_000

    assert result == {
        "status": "success",
        "job_id": job_id,
        "add_dir": None,
        "vectors_count": 0,
        "contents_count": 4,
        "stored_count": 0,
        "delivery_mode": "url",
        "result_s3_key": f"results/{job_id}.zip",
    }
    assert captured_artifacts["parse_kwargs"]["filename"] == source_file_name
    assert captured_artifacts["parse_kwargs"]["internal_output_filename"] == source_file_name
    assert Path(str(captured_artifacts["parse_kwargs"]["file_full_path"])).name == source_file_name
    assert captured_artifacts["result_dir"].endswith("Default_Root/contract-parse.pdf")
    assert captured_artifacts["doc_nav"]["file_name"] == source_file_name
    assert captured_artifacts["doc_nav"]["sections"][0]["title"] == "公司研究"
    assert captured_artifacts["manifest"]["HIERARCHY"] == {
        "公司研究": {
            "自主可控加强，寒武纪或迎来营收快速放量周期": {},
        },
        "相关研报": {
            "要点": {},
        },
    }
    assert "doc_nav.json" in captured_artifacts["raw_entries"]
    assert "hierarchy.json" not in captured_artifacts["raw_entries"]
    assert "hierarchy_slim.json" not in captured_artifacts["raw_entries"]
    assert "chunks.json" in captured_artifacts["zip_entries"]
    assert "full.md" in captured_artifacts["zip_entries"]
    assert "doc_nav.json" in captured_artifacts["zip_entries"]
    assert "chunks_slim.json" not in captured_artifacts["zip_entries"]
    assert "hierarchy.json" not in captured_artifacts["zip_entries"]
    assert "hierarchy_slim.json" not in captured_artifacts["zip_entries"]
    assert "images/page-1.png" in captured_artifacts["zip_entries"]
    assert "tables/table-1.html" in captured_artifacts["zip_entries"]
    assert captured_artifacts["zip_chunks"][0]["metadata"]["document_top_summary"] == expected_summary
    assert captured_artifacts["zip_chunks"][0]["metadata"]["connect_to"] == expected_connect_to
    assert captured_artifacts["zip_chunks"][2]["metadata"]["file_path"] == "images/page-1.png"
    assert captured_artifacts["zip_chunks"][3]["metadata"]["file_path"] == "tables/table-1.html"
    assert _find_task_workspaces(tmp_path, job_id) == []

    progress = redis_service.hgetall(f"task:{job_id}:progress")
    assert progress["progress"] == 100
    assert progress["message"] == "Task complete!"
    assert progress["timestamp"]

    metadata = sync_job_metadata_service_cls(redis_service).get_metadata(job_id)
    assert metadata is not None
    assert metadata["page_count"] == 3
    assert metadata["billing_status"] == expected_billing_status
    if billing_enabled:
        assert metadata["billing_amount_micro_dollars"] == expected_credits_charged
        assert metadata["billing_credits"] == expected_credits_charged / 1_000_000
    else:
        assert metadata["billing_amount_micro_dollars"] == 0
        assert metadata["billing_credits"] == 0.0
    assert metadata["processing_started_at"]
    assert metadata["processing_completed_at"]
    assert metadata["processing_duration_ms"] >= 0

    with engine.begin() as connection:
        job_row = (
            connection.execute(
                text(
                    """
                    SELECT
                        status,
                        billing_status,
                        page_count,
                        credits_charged,
                        error_code,
                        error_message
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )
        job_result_row = (
            connection.execute(
                text(
                    """
                    SELECT delivery_mode, result_s3_key, result_size, inline_payload
                    FROM job_results
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )
        document_row = (
            connection.execute(
                text(
                    """
                    SELECT document_id, namespace, status, current_job_result_id, source_file_name
                    FROM documents
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one()
        )
        document_chunks = list(
            connection.execute(
                text(
                    """
                    SELECT chunk_type, file_path, source_chunk_path, chunk_metadata
                    FROM document_chunks
                    WHERE document_id = :document_id
                    ORDER BY sort_order
                    """
                ),
                {"document_id": document_row["document_id"]},
            )
            .mappings()
            .all()
        )
        graph_node_row = (
            connection.execute(
                text(
                    """
                    SELECT properties
                    FROM graph_nodes
                    WHERE owner_document_id = :document_id
                    """
                ),
                {"document_id": document_row["document_id"]},
            )
            .mappings()
            .one()
        )
        balance_row = (
            connection.execute(
                text(
                    """
                    SELECT credits_balance
                    FROM user_balances
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one_or_none()
        )
        transaction_types = list(
            connection.execute(
                text(
                    """
                    SELECT transaction_type
                    FROM credits_transactions
                    WHERE user_id = :user_id
                    ORDER BY created_at ASC
                    """
                ),
                {"user_id": user_id},
            )
            .scalars()
            .all()
        )
        audit_transitions = list(
            connection.execute(
                text(
                    """
                    SELECT transition_reason, to_state
                    FROM job_state_audit_logs
                    WHERE job_id = :job_id
                    ORDER BY id ASC
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .all()
        )

    graph_properties = dict(graph_node_row["properties"])

    assert job_row["status"] == "done"
    assert job_row["billing_status"] == expected_billing_status
    assert job_row["page_count"] == 3
    if billing_enabled:
        assert job_row["credits_charged"] == expected_credits_charged
    else:
        assert job_row["credits_charged"] == 0
    assert job_row["error_code"] is None
    assert job_row["error_message"] is None
    assert job_result_row["delivery_mode"] == "url"
    assert job_result_row["result_s3_key"] == f"results/{job_id}.zip"
    assert job_result_row["result_size"] > 0
    assert dict(job_result_row["inline_payload"])["checksum"]
    assert document_row["namespace"] == "worker-contract"
    assert document_row["status"] == "active"
    assert document_row["source_file_name"] == source_file_name
    assert document_row["current_job_result_id"]
    assert len(document_chunks) == 4
    assert document_chunks[0]["chunk_type"] == "text"
    assert dict(document_chunks[0]["chunk_metadata"])["document_top_summary"] == expected_summary
    assert dict(document_chunks[0]["chunk_metadata"])["connect_to"] == expected_connect_to
    assert document_chunks[2]["chunk_type"] == "image"
    assert document_chunks[2]["file_path"] == "images/page-1.png"
    assert document_chunks[3]["chunk_type"] == "table"
    assert document_chunks[3]["file_path"] == "tables/table-1.html"
    assert graph_properties["chunks_count"] == 4
    assert graph_properties["top_summary"] == expected_summary
    if billing_enabled:
        assert balance_row is not None
        assert (
            balance_row["credits_balance"]
            == expected_initial_balance - expected_credits_charged
        )
    else:
        assert balance_row is None
    assert transaction_types == expected_transaction_types
    assert [(row["transition_reason"], row["to_state"]) for row in audit_transitions] == [
        ("start_processing", "running"),
        ("mark_completed", "done"),
    ]


def test_should_export_full_result_when_publication_deduplicates_existing_chunks(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "false")
    (
        kb_tasks,
        parse_service,
        parse_job_service,
        engine,
        sync_job_info_service_cls,
        sync_job_metadata_service_cls,
        sync_redis_service_factory,
    ) = _load_parse_task_modules()
    settings = _load_worker_settings()

    user_id: str = f"worker-user-{uuid4().hex[:12]}"
    existing_job_id: str = f"job_existing_{uuid4().hex[:12]}"
    existing_result_id: str = str(uuid4())
    existing_document_id: str = f"doc_{uuid4().hex[:12]}"
    job_id: str = f"job_parse_dedup_{uuid4().hex[:12]}"
    source_file_name: str = "dedup-export.pdf"
    s3_key: str = f"uploads/{job_id}.pdf"
    job_metadata = _build_pending_file_job_metadata(source_file_name)
    captured_artifacts: dict[str, Any] = {}

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=existing_job_id,
            user_id=user_id,
            status="done",
            source_type="file",
            s3_key=f"uploads/{existing_job_id}.pdf",
            webhook_enabled=False,
            job_metadata=_build_pending_file_job_metadata("existing.pdf"),
            billing_status="skipped",
        )
        connection.execute(
            text(
                """
                INSERT INTO job_results (
                    id,
                    job_id,
                    delivery_mode,
                    inline_payload,
                    result_s3_key,
                    result_size,
                    created_at,
                    updated_at
                ) VALUES (
                    :result_id,
                    :job_id,
                    'url',
                    CAST(:inline_payload AS JSON),
                    :result_s3_key,
                    :result_size,
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "result_id": existing_result_id,
                "job_id": existing_job_id,
                "inline_payload": json.dumps({"checksum": "existing"}),
                "result_s3_key": f"results/{existing_job_id}.zip",
                "result_size": 123,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO documents (
                    document_id,
                    user_id,
                    namespace,
                    status,
                    current_job_result_id,
                    source_file_name,
                    created_at,
                    updated_at
                ) VALUES (
                    :document_id,
                    :user_id,
                    'worker-contract',
                    'active',
                    :result_id,
                    'existing.pdf',
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "document_id": existing_document_id,
                "user_id": user_id,
                "result_id": existing_result_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE job_results
                SET document_id = :document_id
                WHERE id = :result_id
                """
            ),
            {
                "document_id": existing_document_id,
                "result_id": existing_result_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO document_chunks (
                    id,
                    chunk_id,
                    user_id,
                    namespace,
                    document_id,
                    job_result_id,
                    chunk_type,
                    content,
                    source_chunk_path,
                    file_path,
                    chunk_metadata,
                    sort_order,
                    created_at
                ) VALUES
                    (
                        :text_id,
                        'duplicate-text',
                        :user_id,
                        'worker-contract',
                        :document_id,
                        :result_id,
                        'text',
                        'already published text',
                        'Default_Root/existing.pdf/Section/Duplicate text',
                        NULL,
                        CAST(:text_metadata AS JSON),
                        0,
                        NOW()
                    ),
                    (
                        :image_id,
                        'duplicate-image',
                        :user_id,
                        'worker-contract',
                        :document_id,
                        :result_id,
                        'image',
                        'already published image',
                        'Default_Root/existing.pdf/images/duplicate.png',
                        'images/duplicate.png',
                        CAST(:image_metadata AS JSON),
                        1,
                        NOW()
                    )
                """
            ),
            {
                "text_id": f"dchk_{uuid4().hex[:12]}",
                "image_id": f"dchk_{uuid4().hex[:12]}",
                "user_id": user_id,
                "document_id": existing_document_id,
                "result_id": existing_result_id,
                "text_metadata": json.dumps({}),
                "image_metadata": json.dumps({"file_path": "images/duplicate.png"}),
            },
        )
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="pending",
            source_type="file",
            s3_key=s3_key,
            webhook_enabled=False,
            job_metadata=job_metadata,
            billing_status="pending",
        )

    _save_worker_task_cache(
        job_id=job_id,
        user_id=user_id,
        s3_key=s3_key,
        metadata=job_metadata,
        sync_job_info_service_cls=sync_job_info_service_cls,
        sync_job_metadata_service_cls=sync_job_metadata_service_cls,
        sync_redis_service_factory=sync_redis_service_factory,
    )

    _bind_parse_task_to_current_module(monkeypatch, kb_tasks=kb_tasks)
    monkeypatch.setattr(settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "BILLING_ENABLED", False)

    def fake_cleanup_task_workspace(workspace_dir: str | None) -> bool:
        captured_artifacts["workspace_dir"] = workspace_dir
        return True

    def fake_verify_s3_file_exists(storage_key: str) -> dict[str, Any]:
        return {
            "exists": storage_key == s3_key,
            "size": _SAMPLE_PDF_PATH.stat().st_size,
        }

    def fake_download_s3_file_to_temp(
        storage_key: str, file_ext: str, temp_dir: str
    ) -> str:
        assert storage_key == s3_key
        downloaded_path = Path(temp_dir) / f"downloaded{file_ext}"
        shutil.copy2(_SAMPLE_PDF_PATH, downloaded_path)
        return str(downloaded_path)

    def fake_checkerboard_inject_parse(**kwargs: Any) -> tuple[str, pd.DataFrame]:
        output_dir = (
            Path(str(kwargs["output_dir"]))
            / str(kwargs["kb_dir"])
            / str(kwargs["internal_output_filename"])
        )
        images_dir = output_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full.md").write_text("body", encoding="utf-8")
        (images_dir / "duplicate.png").write_bytes(b"png")

        file_root = str(kwargs["internal_output_filename"])
        parsed_rows: list[dict[str, Any]] = [
            {
                "content": "duplicate text",
                "path": f"Default_Root/{file_root}/Section/Duplicate text",
                "type": "text",
                "length": 14,
                "keywords": "",
                "summary": "",
                "know_id": "duplicate-text",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "1",
            },
            {
                "content": "duplicate image",
                "path": f"Default_Root/{file_root}/images/duplicate.png",
                "type": "image",
                "length": 15,
                "keywords": "",
                "summary": "",
                "know_id": "duplicate-image",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "2",
            },
            {
                "content": "new text",
                "path": f"Default_Root/{file_root}/Section/New text",
                "type": "text",
                "length": 8,
                "keywords": "",
                "summary": "",
                "know_id": "new-text",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "3",
            },
        ]
        return str(output_dir), pd.DataFrame(parsed_rows)

    class FakeResultStorage:
        def upload(self, *, job_id: str, result_dir: str, zip_file_path: str) -> Any:
            result_dir_path = Path(result_dir)
            zip_path = Path(zip_file_path)
            captured_artifacts["raw_entries"] = sorted(
                path.relative_to(result_dir_path).as_posix()
                for path in result_dir_path.rglob("*")
                if path.is_file()
            )
            with zipfile.ZipFile(zip_path) as zip_file:
                captured_artifacts["zip_entries"] = sorted(zip_file.namelist())
                captured_artifacts["zip_chunks"] = json.loads(
                    zip_file.read("chunks.json")
                )["chunks"]
                captured_artifacts["manifest"] = json.loads(
                    zip_file.read("manifest.json")
                )

            return SimpleNamespace(
                zip_key=f"results/{job_id}.zip",
                raw_prefix=f"results/{job_id}/",
                raw_files={},
            )

    _patch_verify_upload_exists(monkeypatch, fake_verify_s3_file_exists)
    monkeypatch.setattr(parse_job_service, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)
    monkeypatch.setattr(parse_job_service, "get_result_storage", lambda: FakeResultStorage())
    monkeypatch.setattr(parse_job_service, "cleanup_task_workspace", fake_cleanup_task_workspace)

    result = kb_tasks.parse_task.run(job_id, user_id, "kb_management")

    assert result["contents_count"] == 3
    assert "images/duplicate.png" in captured_artifacts["zip_entries"]
    assert "images/duplicate.png" in captured_artifacts["raw_entries"]
    assert [chunk["chunk_id"] for chunk in captured_artifacts["zip_chunks"]] == [
        "duplicate-text",
        "duplicate-image",
        "new-text",
    ]
    assert captured_artifacts["manifest"]["statistics"] == {
        "total_chunks": 3,
        "text_chunks": 2,
        "image_chunks": 1,
        "table_chunks": 0,
        "total_pages": None,
    }
    workspace_dir = Path(str(captured_artifacts["workspace_dir"]))
    assert list(workspace_dir.rglob("images/duplicate.png"))

    with engine.begin() as connection:
        job_result_row = (
            connection.execute(
                text(
                    """
                    SELECT id, document_metadata
                    FROM job_results
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )
        job_chunk_ids = list(
            connection.execute(
                text(
                    """
                    SELECT chunk_id
                    FROM job_chunks
                    WHERE job_result_id = :job_result_id
                    ORDER BY sort_order
                    """
                ),
                {"job_result_id": job_result_row["id"]},
            )
            .scalars()
            .all()
        )
        document_chunk_ids = list(
            connection.execute(
                text(
                    """
                    SELECT document_chunks.chunk_id
                    FROM document_chunks
                    JOIN documents
                        ON documents.document_id = document_chunks.document_id
                    WHERE documents.current_job_result_id = :job_result_id
                    ORDER BY document_chunks.sort_order
                    """
                ),
                {"job_result_id": job_result_row["id"]},
            )
            .scalars()
            .all()
        )

    assert job_chunk_ids == ["duplicate-text", "duplicate-image", "new-text"]
    assert document_chunk_ids == ["duplicate-text", "duplicate-image", "new-text"]
    assert "chunk_overlap" not in dict(job_result_row["document_metadata"] or {})


def test_should_initialize_billing_once_for_concurrent_parse_tasks(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        kb_tasks,
        parse_service,
        parse_job_service,
        engine,
        sync_job_info_service_cls,
        sync_job_metadata_service_cls,
        sync_redis_service_factory,
    ) = _load_parse_task_modules()
    settings = _load_worker_settings()

    user_id: str = f"worker-concurrent-user-{uuid4().hex[:12]}"
    job_ids: list[str] = [f"job_cb_{index}_{uuid4().hex[:12]}" for index in range(2)]
    source_file_name: str = "contract-concurrent.pdf"
    s3_keys: dict[str, str] = {
        job_id: f"uploads/{job_id}.pdf" for job_id in job_ids
    }
    job_metadata_by_id: dict[str, dict[str, Any]] = {
        job_id: _build_pending_file_job_metadata(source_file_name)
        for job_id in job_ids
    }

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        for job_id in job_ids:
            insert_contract_job(
                connection,
                job_id=job_id,
                user_id=user_id,
                status="pending",
                source_type="file",
                s3_key=s3_keys[job_id],
                webhook_enabled=False,
                job_metadata=job_metadata_by_id[job_id],
                billing_status="pending",
            )

    redis_service = sync_redis_service_factory.get_service()
    for job_id in job_ids:
        _save_worker_task_cache(
            job_id=job_id,
            user_id=user_id,
            s3_key=s3_keys[job_id],
            metadata=job_metadata_by_id[job_id],
            sync_job_info_service_cls=sync_job_info_service_cls,
            sync_job_metadata_service_cls=sync_job_metadata_service_cls,
            sync_redis_service_factory=sync_redis_service_factory,
        )

    _bind_parse_task_to_current_module(monkeypatch, kb_tasks=kb_tasks)
    monkeypatch.setattr(settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "BILLING_ENABLED", True)

    def fake_verify_s3_file_exists(storage_key: str) -> dict[str, Any]:
        return {
            "exists": storage_key in s3_keys.values(),
            "size": _SAMPLE_PDF_PATH.stat().st_size,
        }

    def fake_download_s3_file_to_temp(
        storage_key: str, file_ext: str, temp_dir: str
    ) -> str:
        assert storage_key in s3_keys.values()
        assert file_ext == ".pdf"
        downloaded_path = Path(temp_dir) / f"downloaded{file_ext}"
        shutil.copy2(_SAMPLE_PDF_PATH, downloaded_path)
        return str(downloaded_path)

    billing_start_barrier = Barrier(len(job_ids))

    def fake_estimate_page_count(file_path: str) -> int:
        billing_start_barrier.wait(timeout=10)
        return 1

    def fake_checkerboard_inject_parse(**kwargs: Any) -> tuple[str, pd.DataFrame]:
        output_dir = (
            Path(str(kwargs["output_dir"]))
            / str(kwargs["kb_dir"])
            / str(kwargs["internal_output_filename"])
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full.md").write_text("body", encoding="utf-8")

        file_root = str(kwargs["internal_output_filename"])
        parsed_rows: list[dict[str, Any]] = [
            {
                "content": "chunk body",
                "path": f"Default_Root/{file_root}/Section/Point",
                "type": "text",
                "length": 10,
                "keywords": "",
                "summary": "",
                "know_id": f"{kwargs['job_id']}-chunk-1",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "1",
            }
        ]
        return str(output_dir), pd.DataFrame(parsed_rows)

    class FakeResultStorage:
        def upload(self, *, job_id: str, result_dir: str, zip_file_path: str) -> Any:
            return SimpleNamespace(
                zip_key=f"results/{job_id}.zip",
                raw_prefix=f"results/{job_id}/",
                raw_files={},
            )

    _patch_verify_upload_exists(monkeypatch, fake_verify_s3_file_exists)
    monkeypatch.setattr(parse_job_service, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(parse_job_service.PageEstimator, "estimate", fake_estimate_page_count)
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)
    monkeypatch.setattr(parse_job_service, "get_result_storage", lambda: FakeResultStorage())

    def run_parse_task(job_id: str) -> dict[str, Any]:
        return dict(kb_tasks.parse_task.run(job_id, user_id, "kb_management"))

    with ThreadPoolExecutor(max_workers=len(job_ids)) as executor:
        results = list(executor.map(run_parse_task, job_ids))

    expected_credits_charged = int(settings.MICRO_DOLLARS_PER_PAGE)
    expected_initial_balance = (
        int(settings.FREE_PLAN_INITIAL_CREDITS) * 1_000_000
    )

    with engine.begin() as connection:
        job_rows = list(
            connection.execute(
                text(
                    """
                    SELECT job_id, status, billing_status, page_count, credits_charged
                    FROM jobs
                    WHERE job_id = ANY(:job_ids)
                    ORDER BY job_id
                    """
                ),
                {"job_ids": job_ids},
            )
            .mappings()
            .all()
        )
        balance_row = (
            connection.execute(
                text(
                    """
                    SELECT credits_balance
                    FROM user_balances
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one()
        )
        transaction_rows = list(
            connection.execute(
                text(
                    """
                    SELECT transaction_type, COUNT(*) AS count
                    FROM credits_transactions
                    WHERE user_id = :user_id
                    GROUP BY transaction_type
                    ORDER BY transaction_type
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .all()
        )
        payment_count_row = (
            connection.execute(
                text(
                    """
                    SELECT COUNT(*) AS count
                    FROM payment_records
                    WHERE user_id = :user_id
                      AND payment_type = 'system_grant'
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one()
        )

    metadata_by_job_id = {
        job_id: sync_job_metadata_service_cls(redis_service).get_metadata(job_id)
        for job_id in job_ids
    }
    result_job_ids = {str(result["job_id"]) for result in results}
    transaction_counts = {
        str(row["transaction_type"]): int(row["count"]) for row in transaction_rows
    }

    assert result_job_ids == set(job_ids)
    assert all(result["status"] == "success" for result in results)
    assert [
        {
            "job_id": row["job_id"],
            "status": row["status"],
            "billing_status": row["billing_status"],
            "page_count": row["page_count"],
            "credits_charged": row["credits_charged"],
        }
        for row in job_rows
    ] == [
        {
            "job_id": job_id,
            "status": "done",
            "billing_status": "charged",
            "page_count": 1,
            "credits_charged": expected_credits_charged,
        }
        for job_id in sorted(job_ids)
    ]
    assert all(
        metadata_by_job_id[job_id] is not None
        and metadata_by_job_id[job_id]["billing_status"] == "charged"
        and metadata_by_job_id[job_id]["billing_amount_micro_dollars"]
        == expected_credits_charged
        for job_id in job_ids
    )
    assert balance_row == {
        "credits_balance": expected_initial_balance
        - (expected_credits_charged * len(job_ids))
    }
    assert transaction_counts == {"initial_grant": 1, "usage": len(job_ids)}
    assert payment_count_row == {"count": 1}


def test_should_skip_parse_task_when_the_job_is_already_terminal(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        kb_tasks,
        parse_service,
        parse_job_service,
        engine,
        sync_job_info_service_cls,
        sync_job_metadata_service_cls,
        sync_redis_service_factory,
    ) = _load_parse_task_modules()
    settings = _load_worker_settings()

    user_id: str = f"worker-user-{uuid4().hex[:12]}"
    job_id: str = f"job_parse_skipped_{uuid4().hex[:12]}"
    source_file_name: str = "contract-skip.pdf"
    s3_key: str = f"uploads/{job_id}.pdf"

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        job_metadata = _build_pending_file_job_metadata(source_file_name)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            s3_key=s3_key,
            status="done",
            source_type="file",
            webhook_enabled=False,
            job_metadata=job_metadata,
            billing_status="charged",
        )

    redis_service = _save_worker_task_cache(
        job_id=job_id,
        user_id=user_id,
        s3_key=s3_key,
        metadata=job_metadata,
        sync_job_info_service_cls=sync_job_info_service_cls,
        sync_job_metadata_service_cls=sync_job_metadata_service_cls,
        sync_redis_service_factory=sync_redis_service_factory,
    )

    _bind_parse_task_to_current_module(monkeypatch, kb_tasks=kb_tasks)
    monkeypatch.setattr(settings, "TMP_PATH", str(tmp_path))
    def fake_verify_s3_file_exists(storage_key: str) -> dict[str, Any]:
        return {"exists": storage_key == s3_key, "size": 1024}

    _patch_verify_upload_exists(monkeypatch, fake_verify_s3_file_exists)
    monkeypatch.setattr(
        parse_service,
        "checkerboard_inject_parse",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal parse task should not invoke the parser")
        ),
    )

    result = kb_tasks.parse_task.run(job_id, user_id, "kb_management")

    assert result == {
        "status": "skipped",
        "job_id": job_id,
        "reason": "job_already_terminal",
    }
    assert redis_service.hgetall(f"task:{job_id}:progress") == {}
    assert _find_task_workspaces(tmp_path, job_id) == []

    with engine.begin() as connection:
        job_result_count = int(
            connection.execute(
                text("SELECT COUNT(*) FROM job_results WHERE job_id = :job_id"),
                {"job_id": job_id},
            ).scalar_one()
        )
        audit_transition_count = int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM job_state_audit_logs WHERE job_id = :job_id"
                ),
                {"job_id": job_id},
            ).scalar_one()
        )

    assert job_result_count == 0
    assert audit_transition_count == 0


def test_should_mark_the_job_failed_and_cleanup_the_workspace_when_parse_execution_raises(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        kb_tasks,
        parse_service,
        parse_job_service,
        engine,
        sync_job_info_service_cls,
        sync_job_metadata_service_cls,
        sync_redis_service_factory,
    ) = _load_parse_task_modules()
    settings = _load_worker_settings()

    user_id: str = f"worker-user-{uuid4().hex[:12]}"
    job_id: str = f"job_parse_failure_{uuid4().hex[:12]}"
    source_file_name: str = "contract-failure.pdf"
    s3_key: str = f"uploads/{job_id}.pdf"

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        job_metadata = _build_pending_file_job_metadata(source_file_name)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="pending",
            source_type="file",
            s3_key=s3_key,
            webhook_enabled=False,
            job_metadata=job_metadata,
            billing_status="pending",
        )

    _save_worker_task_cache(
        job_id=job_id,
        user_id=user_id,
        s3_key=s3_key,
        metadata=job_metadata,
        sync_job_info_service_cls=sync_job_info_service_cls,
        sync_job_metadata_service_cls=sync_job_metadata_service_cls,
        sync_redis_service_factory=sync_redis_service_factory,
    )

    _bind_parse_task_to_current_module(monkeypatch, kb_tasks=kb_tasks)
    monkeypatch.setattr(settings, "TMP_PATH", str(tmp_path))
    def fake_verify_s3_file_exists(storage_key: str) -> dict[str, Any]:
        return {
            "exists": storage_key == s3_key,
            "size": _SAMPLE_PDF_PATH.stat().st_size,
        }

    _patch_verify_upload_exists(monkeypatch, fake_verify_s3_file_exists)

    def fake_download_s3_file_to_temp(
        storage_key: str, file_ext: str, temp_dir: str
    ) -> str:
        assert storage_key == s3_key
        downloaded_path = Path(temp_dir) / f"downloaded{file_ext}"
        shutil.copy2(_SAMPLE_PDF_PATH, downloaded_path)
        return str(downloaded_path)

    monkeypatch.setattr(parse_job_service, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(
        parse_service,
        "checkerboard_inject_parse",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("parse failed")),
    )
    monkeypatch.setattr(
        parse_job_service,
        "get_result_storage",
        lambda: (_ for _ in ()).throw(
            AssertionError("result storage should not run after parser failure")
        ),
    )

    result = kb_tasks.parse_task.apply(
        args=[job_id, user_id, "kb_management"],
        throw=False,
    )

    assert result.status == "FAILURE"
    assert _find_task_workspaces(tmp_path, job_id) == []

    expected_credits_charged = 3 * int(settings.MICRO_DOLLARS_PER_PAGE)
    expected_initial_balance = int(settings.FREE_PLAN_INITIAL_CREDITS) * 1_000_000

    with engine.begin() as connection:
        job_row = (
            connection.execute(
                text(
                    """
                    SELECT status, billing_status, page_count, credits_charged, error_code, error_message
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )
        balance_row = (
            connection.execute(
                text(
                    """
                    SELECT credits_balance
                    FROM user_balances
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one()
        )
        transaction_types = list(
            connection.execute(
                text(
                    """
                    SELECT transaction_type
                    FROM credits_transactions
                    WHERE user_id = :user_id
                    ORDER BY created_at ASC
                    """
                ),
                {"user_id": user_id},
            )
            .scalars()
            .all()
        )
        audit_transitions = list(
            connection.execute(
                text(
                    """
                    SELECT transition_reason, to_state
                    FROM job_state_audit_logs
                    WHERE job_id = :job_id
                    ORDER BY id ASC
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .all()
        )

    assert job_row["status"] == "failed"
    assert job_row["billing_status"] == "refunded"
    assert job_row["page_count"] == 3
    assert job_row["credits_charged"] == expected_credits_charged
    assert job_row["error_code"] == "UNKNOWN"
    assert job_row["error_message"] == "An unexpected error occurred"
    assert balance_row["credits_balance"] == expected_initial_balance
    assert transaction_types == ["initial_grant", "usage", "refund"]
    assert [(row["transition_reason"], row["to_state"]) for row in audit_transitions] == [
        ("start_processing", "running"),
        ("mark_failed", "failed"),
    ]
