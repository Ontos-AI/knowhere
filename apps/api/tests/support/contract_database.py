from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shared.testing.contract_runtime import get_contract_database_url
from shared.utils.api_keys import hash_api_key


async def _create_contract_engine() -> AsyncEngine:
    return create_async_engine(get_contract_database_url(), future=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContractDatabase:
    @classmethod
    async def execute(
        cls,
        statement: str,
        parameters: dict[str, object] | None = None,
    ) -> None:
        engine = await _create_contract_engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(statement), parameters or {})
        finally:
            await engine.dispose()

    @classmethod
    async def fetch_one(
        cls,
        statement: str,
        parameters: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        engine = await _create_contract_engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), parameters or {})
                row = result.mappings().first()
                return dict(row) if row is not None else None
        finally:
            await engine.dispose()

    @classmethod
    async def fetch_all(
        cls,
        statement: str,
        parameters: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        engine = await _create_contract_engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), parameters or {})
                return [dict(row) for row in result.mappings().all()]
        finally:
            await engine.dispose()

    @classmethod
    async def insert_user(
        cls,
        *,
        user_id: str,
        name: str | None = None,
        email: str | None = None,
    ) -> None:
        await cls.execute(
            """
            INSERT INTO "user" (id, name, email)
            VALUES (:user_id, :name, :email)
            """,
            {
                "user_id": user_id,
                "name": name or f"Contract User {user_id}",
                "email": email or f"{user_id}@contract.knowhere.local",
            },
        )

    @classmethod
    async def insert_user_balance(
        cls,
        *,
        user_id: str,
        credits_balance: int = 0,
        user_tier: str = "free",
        stripe_customer_id: str | None = None,
    ) -> None:
        timestamp = _utc_now()
        await cls.execute(
            """
            INSERT INTO user_balances (
                user_id,
                credits_balance,
                user_tier,
                stripe_customer_id,
                created_at,
                updated_at
            ) VALUES (
                :user_id,
                :credits_balance,
                :user_tier,
                :stripe_customer_id,
                :created_at,
                :updated_at
            )
            """,
            {
                "user_id": user_id,
                "credits_balance": credits_balance,
                "user_tier": user_tier,
                "stripe_customer_id": stripe_customer_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

    @classmethod
    async def insert_authenticated_user(
        cls,
        *,
        user_id: str,
        api_key: str,
        user_tier: str = "free",
        credits_balance: int = 0,
        enabled_modules: list[str] | None = None,
    ) -> None:
        await cls.insert_user(user_id=user_id)
        await cls.insert_user_balance(
            user_id=user_id,
            credits_balance=credits_balance,
            user_tier=user_tier,
        )

        timestamp = _utc_now()
        api_key_hash = hash_api_key(api_key)
        api_key_id = f"key_{uuid4().hex[:12]}"

        await cls.execute(
            """
            INSERT INTO api_keys (
                id,
                user_id,
                key_hash,
                key_mask,
                name,
                enabled_modules,
                is_active,
                created_at
            ) VALUES (
                :id,
                :user_id,
                :key_hash,
                :key_mask,
                :name,
                CAST(:enabled_modules AS JSON),
                :is_active,
                :created_at
            )
            """,
            {
                "id": api_key_id,
                "user_id": user_id,
                "key_hash": api_key_hash,
                "key_mask": f"{api_key[:8]}...{api_key[-4:]}",
                "name": f"Contract API Key {user_id}",
                "enabled_modules": json.dumps(enabled_modules or ["all"]),
                "is_active": True,
                "created_at": timestamp,
            },
        )

    @classmethod
    async def insert_credits_transaction(
        cls,
        *,
        transaction_id: str,
        user_id: str,
        credits_amount: int,
        transaction_type: str,
        description: str | None = None,
        stripe_payment_id: str | None = None,
        transaction_metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        await cls.execute(
            """
            INSERT INTO credits_transactions (
                id,
                user_id,
                credits_amount,
                transaction_type,
                stripe_payment_id,
                description,
                transaction_metadata,
                created_at
            ) VALUES (
                :id,
                :user_id,
                :credits_amount,
                :transaction_type,
                :stripe_payment_id,
                :description,
                CAST(:transaction_metadata AS JSON),
                :created_at
            )
            """,
            {
                "id": transaction_id,
                "user_id": user_id,
                "credits_amount": credits_amount,
                "transaction_type": transaction_type,
                "stripe_payment_id": stripe_payment_id,
                "description": description,
                "transaction_metadata": json.dumps(transaction_metadata or {}),
                "created_at": created_at or _utc_now(),
            },
        )

    @classmethod
    async def insert_price_config(
        cls,
        *,
        price_id: str,
        product_type: str,
        plan_id: str,
        credits_amount: int = 0,
        amount_cents: int = 0,
        currency: str = "CNY",
        is_active: bool = True,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        timestamp = _utc_now()
        await cls.execute(
            """
            INSERT INTO stripe_price_configs (
                id,
                price_id,
                product_type,
                plan_id,
                credits_amount,
                amount_cents,
                currency,
                is_active,
                metadata,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :price_id,
                :product_type,
                :plan_id,
                :credits_amount,
                :amount_cents,
                :currency,
                :is_active,
                CAST(:extra_metadata AS JSON),
                :created_at,
                :updated_at
            )
            """,
            {
                "id": str(uuid4()),
                "price_id": price_id,
                "product_type": product_type,
                "plan_id": plan_id,
                "credits_amount": credits_amount,
                "amount_cents": amount_cents,
                "currency": currency,
                "is_active": is_active,
                "extra_metadata": json.dumps(extra_metadata or {}),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

    @classmethod
    async def insert_document(
        cls,
        *,
        document_id: str,
        user_id: str,
        namespace: str,
        status: str = "active",
        current_job_result_id: str | None = None,
        source_file_name: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        archived_at: datetime | None = None,
    ) -> None:
        timestamp = created_at or _utc_now()
        await cls.execute(
            """
            INSERT INTO documents (
                document_id,
                user_id,
                namespace,
                status,
                current_job_result_id,
                source_file_name,
                created_at,
                updated_at,
                archived_at
            ) VALUES (
                :document_id,
                :user_id,
                :namespace,
                :status,
                :current_job_result_id,
                :source_file_name,
                :created_at,
                :updated_at,
                :archived_at
            )
            """,
            {
                "document_id": document_id,
                "user_id": user_id,
                "namespace": namespace,
                "status": status,
                "current_job_result_id": current_job_result_id,
                "source_file_name": source_file_name or f"{document_id}.pdf",
                "created_at": timestamp,
                "updated_at": updated_at or timestamp,
                "archived_at": archived_at,
            },
        )

    @classmethod
    async def fetch_document(cls, document_id: str) -> dict[str, Any] | None:
        return await cls.fetch_one(
            """
            SELECT
                document_id,
                user_id,
                namespace,
                status,
                current_job_result_id,
                source_file_name,
                archived_at
            FROM documents
            WHERE document_id = :document_id
            """,
            {"document_id": document_id},
        )

    @classmethod
    async def insert_job(
        cls,
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
        await cls.execute(
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
            """,
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

    @classmethod
    async def fetch_job(cls, job_id: str) -> dict[str, Any] | None:
        return await cls.fetch_one(
            """
            SELECT
                job_id,
                user_id,
                job_type,
                status,
                source_type,
                s3_key,
                webhook_url,
                webhook_enabled,
                job_metadata,
                error_message,
                error_code
            FROM jobs
            WHERE job_id = :job_id
            """,
            {"job_id": job_id},
        )

    @classmethod
    async def insert_job_result(
        cls,
        *,
        job_result_id: str,
        job_id: str,
        document_id: str | None = None,
        delivery_mode: str = "inline",
        document_metadata: dict[str, Any] | None = None,
        inline_payload: dict[str, Any] | None = None,
        result_s3_key: str | None = None,
        result_size: int | None = None,
    ) -> None:
        timestamp = _utc_now()
        await cls.execute(
            """
            INSERT INTO job_results (
                id,
                job_id,
                document_id,
                delivery_mode,
                document_metadata,
                inline_payload,
                result_s3_key,
                result_size,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :job_id,
                :document_id,
                :delivery_mode,
                CAST(:document_metadata AS JSON),
                CAST(:inline_payload AS JSON),
                :result_s3_key,
                :result_size,
                :created_at,
                :updated_at
            )
            """,
            {
                "id": job_result_id,
                "job_id": job_id,
                "document_id": document_id,
                "delivery_mode": delivery_mode,
                "document_metadata": json.dumps(document_metadata or {}),
                "inline_payload": json.dumps(inline_payload or {}),
                "result_s3_key": result_s3_key,
                "result_size": result_size,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

    @classmethod
    async def insert_document_section(
        cls,
        *,
        section_id: str,
        user_id: str,
        namespace: str,
        document_id: str,
        job_result_id: str,
        section_path: str,
        section_title: str,
        section_level: int = 0,
        parent_section_id: str | None = None,
        summary: str | None = None,
        section_metadata: dict[str, Any] | None = None,
        sort_order: int = 0,
    ) -> None:
        await cls.execute(
            """
            INSERT INTO document_sections (
                section_id,
                user_id,
                namespace,
                document_id,
                job_result_id,
                parent_section_id,
                section_path,
                section_title,
                section_level,
                summary,
                section_metadata,
                sort_order,
                created_at
            ) VALUES (
                :section_id,
                :user_id,
                :namespace,
                :document_id,
                :job_result_id,
                :parent_section_id,
                :section_path,
                :section_title,
                :section_level,
                :summary,
                CAST(:section_metadata AS JSON),
                :sort_order,
                :created_at
            )
            """,
            {
                "section_id": section_id,
                "user_id": user_id,
                "namespace": namespace,
                "document_id": document_id,
                "job_result_id": job_result_id,
                "parent_section_id": parent_section_id,
                "section_path": section_path,
                "section_title": section_title,
                "section_level": section_level,
                "summary": summary,
                "section_metadata": json.dumps(section_metadata or {}),
                "sort_order": sort_order,
                "created_at": _utc_now(),
            },
        )

    @classmethod
    async def insert_document_chunk(
        cls,
        *,
        chunk_id: str,
        user_id: str,
        namespace: str,
        document_id: str,
        job_result_id: str,
        section_id: str | None,
        chunk_type: str,
        content: str,
        section_path: str | None,
        sort_order: int = 0,
        file_path: str | None = None,
        chunk_metadata: dict[str, Any] | None = None,
    ) -> None:
        await cls.execute(
            """
            INSERT INTO document_chunks (
                id,
                chunk_id,
                user_id,
                namespace,
                document_id,
                job_result_id,
                section_id,
                chunk_type,
                content,
                content_lexical_text,
                path_lexical_text,
                content_search_text,
                path_search_text,
                term_search_text,
                source_chunk_path,
                file_path,
                chunk_metadata,
                sort_order,
                created_at
            ) VALUES (
                :id,
                :chunk_id,
                :user_id,
                :namespace,
                :document_id,
                :job_result_id,
                :section_id,
                :chunk_type,
                :content,
                :content_lexical_text,
                :path_lexical_text,
                :content_search_text,
                :path_search_text,
                :term_search_text,
                :source_chunk_path,
                :file_path,
                CAST(:chunk_metadata AS JSON),
                :sort_order,
                :created_at
            )
            """,
            {
                "id": f"dchk_{uuid4().hex[:12]}",
                "chunk_id": chunk_id,
                "user_id": user_id,
                "namespace": namespace,
                "document_id": document_id,
                "job_result_id": job_result_id,
                "section_id": section_id,
                "chunk_type": chunk_type,
                "content": content,
                "content_lexical_text": content,
                "path_lexical_text": section_path or "",
                "content_search_text": content,
                "path_search_text": section_path or "",
                "term_search_text": f"{content} {section_path or ''}".strip(),
                "source_chunk_path": section_path,
                "file_path": file_path,
                "chunk_metadata": json.dumps(chunk_metadata or {}),
                "sort_order": sort_order,
                "created_at": _utc_now(),
            },
        )

    @classmethod
    async def insert_webhook_event(
        cls,
        *,
        event_id: str,
        job_id: str,
        target_url: str,
        payload: dict[str, Any],
        status: str = "pending",
        attempts: int = 0,
        next_retry_at: datetime | None = None,
        qstash_message_id: str | None = None,
    ) -> None:
        timestamp = _utc_now()
        await cls.execute(
            """
            INSERT INTO webhook_events (
                id,
                job_id,
                target_url,
                payload,
                status,
                attempts,
                next_retry_at,
                qstash_message_id,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :job_id,
                :target_url,
                CAST(:payload AS JSON),
                :status,
                :attempts,
                :next_retry_at,
                :qstash_message_id,
                :created_at,
                :updated_at
            )
            """,
            {
                "id": event_id,
                "job_id": job_id,
                "target_url": target_url,
                "payload": json.dumps(payload),
                "status": status,
                "attempts": attempts,
                "next_retry_at": next_retry_at,
                "qstash_message_id": qstash_message_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

    @classmethod
    async def fetch_webhook_event(cls, event_id: str) -> dict[str, Any] | None:
        return await cls.fetch_one(
            """
            SELECT
                id,
                job_id,
                target_url,
                payload,
                status,
                attempts,
                qstash_message_id
            FROM webhook_events
            WHERE id = :event_id
            """,
            {"event_id": event_id},
        )

    @classmethod
    async def insert_webhook_log(
        cls,
        *,
        log_id: str,
        job_id: str,
        event_id: str | None,
        webhook_url: str,
        attempt_number: int,
        request_payload: dict[str, Any] | None = None,
        signature: str = "",
        idempotency_key: str | None = None,
        response_status_code: int | None = None,
        response_body: str | None = None,
        error_message: str | None = None,
        duration_ms: int = 0,
        qstash_message_id: str | None = None,
    ) -> None:
        await cls.execute(
            """
            INSERT INTO webhook_logs (
                id,
                job_id,
                event_id,
                webhook_url,
                attempt_number,
                request_payload,
                signature,
                idempotency_key,
                response_status_code,
                response_body,
                error_message,
                duration_ms,
                qstash_message_id,
                created_at
            ) VALUES (
                :id,
                :job_id,
                :event_id,
                :webhook_url,
                :attempt_number,
                CAST(:request_payload AS JSON),
                :signature,
                :idempotency_key,
                :response_status_code,
                :response_body,
                :error_message,
                :duration_ms,
                :qstash_message_id,
                :created_at
            )
            """,
            {
                "id": log_id,
                "job_id": job_id,
                "event_id": event_id,
                "webhook_url": webhook_url,
                "attempt_number": attempt_number,
                "request_payload": json.dumps(request_payload or {}),
                "signature": signature,
                "idempotency_key": idempotency_key or str(uuid4()),
                "response_status_code": response_status_code,
                "response_body": response_body,
                "error_message": error_message,
                "duration_ms": duration_ms,
                "qstash_message_id": qstash_message_id,
                "created_at": _utc_now(),
            },
        )
