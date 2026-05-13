from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from tests.support.sync_contract_database import insert_contract_user

_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_API_ROOT: Path = _REPO_ROOT / "apps" / "api"
_ALEMBIC_ROOT: Path = _API_ROOT / "alembic"
_ALEMBIC_INI_PATH: Path = _API_ROOT / "alembic.ini"


def _build_alembic_command_config(*, engine: Engine) -> Config:
    config = Config(str(_ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(_ALEMBIC_ROOT))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    return config


def _upgrade_to_heads(*, engine: Engine) -> None:
    config = _build_alembic_command_config(engine=engine)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "heads")


def _insert_job(
    connection: Connection,
    *,
    job_id: str,
    user_id: str,
    document_id: str,
    status: str,
) -> None:
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    job_metadata = json.dumps(
        {
            "document_id": document_id,
            "namespace": "migration-contract",
            "source_type": "file",
        }
    )

    connection.execute(
        text(
            """
            INSERT INTO jobs (
                job_id,
                user_id,
                job_type,
                status,
                source_type,
                webhook_enabled,
                job_metadata,
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
                :webhook_enabled,
                CAST(:job_metadata AS JSON),
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
            "job_type": "kb_management",
            "status": status,
            "source_type": "file",
            "webhook_enabled": False,
            "job_metadata": job_metadata,
            "version": 0,
            "created_at": timestamp,
            "updated_at": timestamp,
            "credits_charged": 0,
            "billing_status": "pending",
        },
    )


@pytest.fixture
def migrated_head_engine(alembic_engine: Engine) -> Iterator[Engine]:
    _upgrade_to_heads(engine=alembic_engine)
    yield alembic_engine


def test_should_enforce_one_active_document_ingestion_job_per_user(
    migrated_head_engine: Engine,
) -> None:
    user_id = f"migration-user-{uuid4().hex[:12]}"
    document_id = f"doc_migration_{uuid4().hex[:12]}"

    with migrated_head_engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        _insert_job(
            connection,
            job_id=f"job_migration_{uuid4().hex[:12]}",
            user_id=user_id,
            document_id=document_id,
            status="running",
        )

    with pytest.raises(IntegrityError) as exc_info:
        with migrated_head_engine.begin() as connection:
            _insert_job(
                connection,
                job_id=f"job_migration_{uuid4().hex[:12]}",
                user_id=user_id,
                document_id=document_id,
                status="waiting-file",
            )

    assert "uq_jobs_user_active_document" in str(exc_info.value)


def test_should_allow_a_new_active_document_job_after_a_terminal_job(
    migrated_head_engine: Engine,
) -> None:
    user_id = f"migration-user-{uuid4().hex[:12]}"
    document_id = f"doc_migration_{uuid4().hex[:12]}"

    with migrated_head_engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        _insert_job(
            connection,
            job_id=f"job_migration_{uuid4().hex[:12]}",
            user_id=user_id,
            document_id=document_id,
            status="done",
        )
        _insert_job(
            connection,
            job_id=f"job_migration_{uuid4().hex[:12]}",
            user_id=user_id,
            document_id=document_id,
            status="running",
        )

        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM jobs
                WHERE user_id = :user_id
                  AND job_metadata ->> 'document_id' = :document_id
                """
            ),
            {
                "user_id": user_id,
                "document_id": document_id,
            },
        )

    assert int(result.scalar_one()) == 2


def test_api_standalone_mode_should_create_auth_user_table_before_migrations(
    standalone_alembic_engine: Engine,
) -> None:
    _upgrade_to_heads(engine=standalone_alembic_engine)

    with standalone_alembic_engine.begin() as connection:
        columns = set(
            connection.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'user'
                    """
                )
            )
            .scalars()
            .all()
        )
        email_unique_count = int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM pg_constraint
                    WHERE conrelid = '"user"'::regclass
                      AND contype = 'u'
                      AND conkey = ARRAY[
                          (
                              SELECT attnum::smallint
                              FROM pg_attribute
                              WHERE attrelid = '"user"'::regclass
                                AND attname = 'email'
                          )
                      ]
                    """
                )
            ).scalar_one()
        )
        insert_contract_user(
            connection,
            user_id=f"standalone-user-{uuid4().hex[:12]}",
        )

    assert {
        "id",
        "name",
        "email",
        "emailVerified",
        "image",
        "role",
        "createdAt",
        "updatedAt",
    }.issubset(columns)
    assert email_unique_count == 1


def test_agentic_retrieval_trace_schema_matches_orm(migrated_head_engine: Engine) -> None:
    with migrated_head_engine.begin() as connection:
        run_columns = set(
            connection.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'retrieval_runs'
                    """
                )
            )
            .scalars()
            .all()
        )

    assert {
        "parent_run_id",
        "workflow_step_id",
        "workflow_plan",
    }.issubset(run_columns)
