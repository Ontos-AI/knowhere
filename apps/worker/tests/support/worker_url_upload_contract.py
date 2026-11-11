from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.contract_database import insert_contract_job, insert_contract_user

_DOCUMENT_INGESTION_JOB_TYPE = "document_ingestion"
_URL_UPLOAD_TASK_NAME = "app.core.tasks.document_ingestion_tasks.upload_url_file_task"


@dataclass(frozen=True)
class WorkerUrlUploadContract:
    engine: Engine
    settings: Any
    storage: Any
    sync_redis_service_factory: Any

    @classmethod
    def create(cls) -> WorkerUrlUploadContract:
        from shared.core.config import settings
        from shared.core.database_sync import get_sync_engine
        from shared.services.redis.redis_sync_service import SyncRedisServiceFactory
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

    def allow_private_url_sources(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setattr(self.settings, "ENVIRONMENT", "development")

    def create_url_job(
        self, *, source_url: str, source_file_name: str = "contract-source.pdf"
    ) -> dict[str, str]:
        user_id = f"worker-url-contract-user-{uuid4().hex[:12]}"
        job_id = f"job_url_upload_{uuid4().hex[:12]}"
        s3_key = self.storage.build_upload_key(
            job_id=job_id, file_extension=Path(source_file_name).suffix
        )

        with self.engine.begin() as connection:
            insert_contract_user(connection, user_id=user_id)
            insert_contract_job(
                connection,
                job_id=job_id,
                user_id=user_id,
                status="waiting-file",
                source_type="url",
                s3_key=s3_key,
                job_metadata={
                    "namespace": "worker-contract",
                    "source_type": "url",
                    "source_url": source_url,
                    "source_file_name": source_file_name,
                },
            )

        return {
            "job_id": job_id,
            "user_id": user_id,
            "source_url": source_url,
            "s3_key": s3_key,
        }

    def enqueue_upload_url_task(
        self,
        *,
        job_id: str,
        source_url: str,
        user_id: str,
    ) -> Any:
        from shared.core.celery_app import get_celery_app

        task_signature = get_celery_app().signature(_URL_UPLOAD_TASK_NAME)
        return task_signature.apply_async(
            args=[job_id, source_url, user_id],
            kwargs={"job_type": _DOCUMENT_INGESTION_JOB_TYPE},
        )

    def get_task_progress(self, job_id: str) -> dict[str, Any]:
        redis_service = self.sync_redis_service_factory.get_service()
        return redis_service.hgetall(f"task:{job_id}:progress")

    def observe_job(self, job_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        """
                        SELECT status, source_type, s3_key
                        FROM jobs
                        WHERE job_id = :job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )

    def verify_uploaded_source_object(self, s3_key: str) -> dict[str, Any]:
        return self.storage.verify_upload_exists(s3_key)

    def read_uploaded_source_object(self, s3_key: str) -> bytes:
        return self.storage.storage_adapter.download_fileobj(
            s3_key,
            bucket=self.settings.S3_BUCKET_NAME,
        )


__all__ = ["WorkerUrlUploadContract"]
