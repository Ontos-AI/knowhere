from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


def insert_contract_user(
    connection: Connection,
    *,
    user_id: str,
    name: str | None = None,
    email: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO "user" (id, name, email)
            VALUES (:user_id, :name, :email)
            """
        ),
        {
            "user_id": user_id,
            "name": name or f"Worker Contract User {user_id}",
            "email": email or f"{user_id}@worker-contract.knowhere.local",
        },
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def insert_contract_job(
    connection: Connection,
    *,
    job_id: str,
    user_id: str,
    job_type: str = "document_ingestion",
    status: str = "pending",
    source_type: str = "file",
    file_path: str | None = None,
    s3_key: str | None = None,
    webhook_url: str | None = None,
    webhook_enabled: bool | None = None,
    job_metadata: dict[str, Any] | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    credits_charged: int = 0,
    billing_status: str = "pending",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> None:
    timestamp = created_at or _utc_now()
    connection.execute(
        text(
            """
            INSERT INTO jobs (
                job_id,
                user_id,
                job_type,
                status,
                source_type,
                file_path,
                s3_key,
                webhook_url,
                webhook_enabled,
                job_metadata,
                error_message,
                error_code,
                version,
                created_at,
                updated_at,
                credits_charged,
                billing_status
            ) VALUES (
                :job_id,
                :user_id,
                :job_type,
                :status,
                :source_type,
                :file_path,
                :s3_key,
                :webhook_url,
                :webhook_enabled,
                CAST(:job_metadata AS JSON),
                :error_message,
                :error_code,
                :version,
                :created_at,
                :updated_at,
                :credits_charged,
                :billing_status
            )
            """
        ),
        {
            "job_id": job_id,
            "user_id": user_id,
            "job_type": job_type,
            "status": status,
            "source_type": source_type,
            "file_path": file_path,
            "s3_key": s3_key,
            "webhook_url": webhook_url,
            "webhook_enabled": (
                webhook_enabled if webhook_enabled is not None else bool(webhook_url)
            ),
            "job_metadata": json.dumps(job_metadata or {}),
            "error_message": error_message,
            "error_code": error_code,
            "version": 0,
            "created_at": timestamp,
            "updated_at": updated_at or timestamp,
            "credits_charged": credits_charged,
            "billing_status": billing_status,
        },
    )
