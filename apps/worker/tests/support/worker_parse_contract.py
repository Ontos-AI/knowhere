from __future__ import annotations

import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.contract_database import insert_contract_job

_DOCUMENT_PARSE_JOB_TYPE = "document_ingestion"
_DOCUMENT_PARSE_TASK_NAME = "app.core.tasks.document_ingestion_tasks.parse_task"


@dataclass(frozen=True)
class WorkerParseContract:
    engine: Engine
    settings: Any
    storage: Any
    sync_redis_service_factory: Any

    @classmethod
    def create(cls) -> WorkerParseContract:
        from shared.core.config import settings
        from shared.core.database_sync import get_sync_engine
        from shared.services.redis.redis_sync_service import (
            SyncRedisServiceFactory,
        )
        from shared.services.storage.job_file_storage import JobFileStorage

        return cls(
            engine=get_sync_engine(),
            settings=settings,
            storage=JobFileStorage(),
            sync_redis_service_factory=SyncRedisServiceFactory,
        )

    def use_workspace_root(
        self,
        monkeypatch: MonkeyPatch,
        workspace_root: Path,
    ) -> None:
        monkeypatch.setattr(self.settings, "TMP_PATH", str(workspace_root))

    def use_billing(self, monkeypatch: MonkeyPatch, is_enabled: bool) -> None:
        monkeypatch.setenv("BILLING_ENABLED", "true" if is_enabled else "false")
        monkeypatch.setattr(self.settings, "BILLING_ENABLED", is_enabled)
        for loaded_module in list(sys.modules.values()):
            module_settings = getattr(loaded_module, "settings", None)
            if hasattr(module_settings, "BILLING_ENABLED"):
                monkeypatch.setattr(
                    module_settings,
                    "BILLING_ENABLED",
                    is_enabled,
                    raising=False,
                )
    def use_pdf_page_limit(self, monkeypatch: MonkeyPatch, page_limit: int) -> None:
        monkeypatch.setenv("MAX_PDF_PAGE_LIMIT", str(page_limit))
        monkeypatch.setattr(self.settings, "MAX_PDF_PAGE_LIMIT", page_limit)
        for loaded_module in list(sys.modules.values()):
            module_settings = getattr(loaded_module, "settings", None)
            if hasattr(module_settings, "MAX_PDF_PAGE_LIMIT"):
                monkeypatch.setattr(
                    module_settings,
                    "MAX_PDF_PAGE_LIMIT",
                    page_limit,
                    raising=False,
                )

    def use_oversized_pdf_shard_enabled(
        self,
        monkeypatch: MonkeyPatch,
        enabled: bool,
    ) -> None:
        monkeypatch.setenv("OVERSIZED_PDF_SHARD_ENABLED", "true" if enabled else "false")
        monkeypatch.setattr(self.settings, "OVERSIZED_PDF_SHARD_ENABLED", enabled)
        for loaded_module in list(sys.modules.values()):
            module_settings = getattr(loaded_module, "settings", None)
            if hasattr(module_settings, "OVERSIZED_PDF_SHARD_ENABLED"):
                monkeypatch.setattr(
                    module_settings,
                    "OVERSIZED_PDF_SHARD_ENABLED",
                    enabled,
                    raising=False,
                )

    def use_oversized_pdf_soft_limit(
        self,
        monkeypatch: MonkeyPatch,
        soft_limit: int,
    ) -> None:
        monkeypatch.setenv("OVERSIZED_PDF_SOFT_LIMIT", str(soft_limit))
        monkeypatch.setattr(self.settings, "OVERSIZED_PDF_SOFT_LIMIT", soft_limit)
        for loaded_module in list(sys.modules.values()):
            module_settings = getattr(loaded_module, "settings", None)
            if hasattr(module_settings, "OVERSIZED_PDF_SOFT_LIMIT"):
                monkeypatch.setattr(
                    module_settings,
                    "OVERSIZED_PDF_SOFT_LIMIT",
                    soft_limit,
                    raising=False,
                )

    def create_file_job(
        self,
        *,
        source_file_name: str,
        user_id: str | None = None,
        status: str = "pending",
        billing_status: str = "pending",
        job_id_prefix: str = "job_parse",
    ) -> dict[str, Any]:
        effective_user_id = user_id or f"worker-contract-user-{uuid4().hex[:12]}"
        job_id = f"{job_id_prefix}_{uuid4().hex[:12]}"
        s3_key = self.storage.build_upload_key(
            job_id=job_id,
            file_extension=Path(source_file_name).suffix,
        )
        with self.engine.begin() as connection:
            self.ensure_user(connection, user_id=effective_user_id)
            insert_contract_job(
                connection,
                job_id=job_id,
                user_id=effective_user_id,
                status=status,
                source_type="file",
                file_path=source_file_name,
                s3_key=s3_key,
                job_metadata={
                    "namespace": "worker-contract",
                    "source_type": "file",
                    "source_file_name": source_file_name,
                    "parsing_params": {
                        "doc_type": "auto",
                        "smart_title_parse": False,
                        "summary_image": False,
                        "summary_table": False,
                        "summary_txt": False,
                        "summary_use_llm": False,
                        "top_summary_use_llm": False,
                    },
                },
                billing_status=billing_status,
            )

        return {
            "job_id": job_id,
            "user_id": effective_user_id,
            "source_file_name": source_file_name,
            "s3_key": s3_key,
        }

    @staticmethod
    def ensure_user(connection: Any, *, user_id: str) -> None:
        connection.execute(
            text(
                """
                INSERT INTO "user" (id, name, email)
                VALUES (:user_id, :name, :email)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "user_id": user_id,
                "name": f"Worker Contract User {user_id}",
                "email": f"{user_id}@worker-contract.knowhere.local",
            },
        )

    def upload_source_file(self, *, local_file_path: Path, s3_key: str) -> None:
        upload_result = self.storage.upload_source_file(str(local_file_path), s3_key)
        assert upload_result["status"] == "success"
        assert self.storage.verify_upload_exists(s3_key)["exists"] is True

    def enqueue_parse_task(self, *, job_id: str, user_id: str) -> Any:
        from shared.core.celery_app import get_celery_app
        from shared.core.celery_router import task_router

        queue_name = task_router.get_queue_for_job(_DOCUMENT_PARSE_JOB_TYPE, user_id)
        task_signature = get_celery_app().signature(
            _DOCUMENT_PARSE_TASK_NAME,
            args=[job_id],
            kwargs={
                "user_id": user_id,
                "job_type": _DOCUMENT_PARSE_JOB_TYPE,
            },
        ).set(queue=queue_name)
        return task_signature.apply_async()

    def get_task_status(self, job_id: str) -> Any:
        redis_service = self.sync_redis_service_factory.get_service()
        return redis_service.get(f"task:{job_id}:status")

    def get_task_progress(self, job_id: str) -> dict[str, Any]:
        redis_service = self.sync_redis_service_factory.get_service()
        return redis_service.hgetall(f"task:{job_id}:progress")

    def observe_successful_job(self, job_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
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
            result_row = (
                connection.execute(
                    text(
                        """
                        SELECT
                            id,
                            document_id,
                            result_s3_key,
                            result_size,
                            document_metadata
                        FROM job_results
                        WHERE job_id = :job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
            job_chunks = (
                connection.execute(
                    text(
                        """
                        SELECT chunk_id, chunk_type, text, path, chunk_metadata
                        FROM job_chunks
                        WHERE job_result_id = :job_result_id
                        ORDER BY sort_order
                        """
                    ),
                    {"job_result_id": result_row["id"]},
                )
                .mappings()
                .all()
            )
            document_chunks = (
                connection.execute(
                    text(
                        """
                        SELECT chunk_id, chunk_type, content, source_chunk_path
                        FROM document_chunks
                        WHERE job_result_id = :job_result_id
                        ORDER BY sort_order
                        """
                    ),
                    {"job_result_id": result_row["id"]},
                )
                .mappings()
                .all()
            )
            document_sections_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM document_sections
                    WHERE job_result_id = :job_result_id
                    """
                ),
                {"job_result_id": result_row["id"]},
            ).scalar_one()

        return {
            "job": job_row,
            "result": result_row,
            "job_chunks": job_chunks,
            "document_chunks": document_chunks,
            "document_sections_count": document_sections_count,
        }

    def observe_job_status(self, job_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            return (
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

    def get_job_metadata(self, job_id: str) -> dict[str, Any]:
        from shared.services.redis.redis_sync_service import SyncJobMetadataService

        redis_service = self.sync_redis_service_factory.get_service()
        metadata = SyncJobMetadataService(redis_service).get_metadata(job_id)
        if isinstance(metadata, dict) and metadata:
            return dict(metadata)

        with self.engine.connect() as connection:
            stored_metadata = connection.execute(
                text("SELECT job_metadata FROM jobs WHERE job_id = :job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none()
        return dict(stored_metadata or {})

    def observe_user_billing(self, user_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            balance = connection.execute(
                text(
                    """
                    SELECT credits_balance
                    FROM user_balances
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
            transaction_types = list(
                connection.execute(
                    text(
                        """
                        SELECT transaction_type
                        FROM credits_transactions
                        WHERE user_id = :user_id
                        ORDER BY created_at ASC, id ASC
                        """
                    ),
                    {"user_id": user_id},
                )
                .scalars()
                .all()
            )
            transaction_count_rows = (
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
            system_grant_payment_count = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM payment_records
                        WHERE user_id = :user_id
                          AND payment_type = 'system_grant'
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one()
            )

        return {
            "balance": int(balance) if balance is not None else None,
            "transaction_types": transaction_types,
            "transaction_counts": {
                str(row["transaction_type"]): int(row["count"])
                for row in transaction_count_rows
            },
            "system_grant_payment_count": system_grant_payment_count,
        }

    def observe_job_state_transitions(self, job_id: str) -> list[tuple[str, str]]:
        with self.engine.connect() as connection:
            rows = (
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
        return [
            (str(row["transition_reason"]), str(row["to_state"])) for row in rows
        ]

    def count_job_results(self, job_id: str) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text("SELECT COUNT(*) FROM job_results WHERE job_id = :job_id"),
                    {"job_id": job_id},
                ).scalar_one()
            )

    def verify_result_zip_object(self, result_s3_key: str) -> dict[str, Any]:
        return self.storage.verify_exists(
            result_s3_key,
            bucket=self.settings.S3_RESULTS_BUCKET,
        )

    def read_result_zip(
        self,
        *,
        result_s3_key: str,
        tmp_path: Path,
    ) -> dict[str, Any]:
        downloaded_zip_path = tmp_path / "result.zip"
        self.storage.download_to_path(
            result_s3_key,
            str(downloaded_zip_path),
            bucket=self.settings.S3_RESULTS_BUCKET,
        )

        with zipfile.ZipFile(downloaded_zip_path) as archive:
            members = set(archive.namelist())
            with archive.open("chunks.json") as chunks_file:
                chunks_payload = json.loads(chunks_file.read().decode("utf-8"))
            with archive.open("manifest.json") as manifest_file:
                manifest_payload = json.loads(manifest_file.read().decode("utf-8"))

        return {
            "path": downloaded_zip_path,
            "members": members,
            "chunks": chunks_payload,
            "manifest": manifest_payload,
        }

    def find_task_workspaces(self, workspace_root: Path, job_id: str) -> list[Path]:
        return sorted(
            path
            for path in workspace_root.iterdir()
            if path.is_dir()
            and path.name.startswith(f"document_ingestion_task_{job_id}_")
        )


__all__ = ["WorkerParseContract"]
