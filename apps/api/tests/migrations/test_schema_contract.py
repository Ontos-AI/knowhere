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

    # Let Alembic create its own connection.  This is required for migrations
    # that use PostgreSQL autocommit (for example CREATE INDEX CONCURRENTLY).
    command.upgrade(config, "heads")


def _upgrade_to_heads_with_external_connection(*, engine: Engine) -> None:
    config = _build_alembic_command_config(engine=engine)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "heads")


def _upgrade_to_snapshot_parents(*, engine: Engine) -> None:
    config = _build_alembic_command_config(engine=engine)
    command.upgrade(config, "f0d85d209e68")
    command.upgrade(config, "fbe1c2d3e4f5")


def _upgrade_to_channel_statistics(*, engine: Engine) -> None:
    config = _build_alembic_command_config(engine=engine)
    command.upgrade(config, "b1c2d3e4f5a6")


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
            "job_type": "document_ingestion",
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


def test_should_seed_v2_job_polling_system_limit(
    migrated_head_engine: Engine,
) -> None:
    with migrated_head_engine.begin() as connection:
        result = connection.execute(
            text(
                """
                SELECT priority, rpm, period, description
                FROM system_limits
                WHERE method = 'GET'
                  AND api_pattern = '/v2/jobs/*'
                """
            )
        ).mappings().one()

    assert result["priority"] == 200
    assert result["rpm"] == 200
    assert result["period"] == "minute"
    assert result["description"] == "Job queries - prevent polling"


def test_should_index_document_chunks_in_snapshot_pagination_order(
    migrated_head_engine: Engine,
) -> None:
    with migrated_head_engine.begin() as connection:
        index_definition = connection.execute(
            text(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'document_chunks'
                  AND indexname = 'idx_document_chunks_revision_snapshot_order'
                """
            )
        ).scalar_one()

    assert "(document_id, job_result_id, sort_order, chunk_id, id)" in str(
        index_definition
    )


def test_should_index_document_chunks_in_lazy_section_order(
    migrated_head_engine: Engine,
) -> None:
    with migrated_head_engine.begin() as connection:
        index_definition = connection.execute(
            text(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'document_chunks'
                  AND indexname = 'idx_document_chunks_revision_section_order'
                """
            )
        ).scalar_one()

    assert (
        "(document_id, job_result_id, section_id, sort_order, chunk_id, id)"
        in str(index_definition)
    )


def test_should_create_token_leading_map_unit_covering_index(
    migrated_head_engine: Engine,
) -> None:
    with migrated_head_engine.begin() as connection:
        index_row = connection.execute(
            text(
                """
                SELECT pg_get_indexdef(indexes.indexrelid),
                       indexes.indisvalid,
                       indexes.indisready
                FROM pg_index AS indexes
                JOIN pg_class AS classes ON classes.oid = indexes.indexrelid
                JOIN pg_namespace AS namespaces
                  ON namespaces.oid = classes.relnamespace
                WHERE namespaces.nspname = current_schema()
                  AND classes.relname = 'idx_document_map_unit_tokens_token_lookup'
                """
            )
        ).one()

    definition = str(index_row[0])
    assert "(channel, token_hash, map_unit_id)" in definition
    assert "INCLUDE (token, frequency)" in definition
    assert index_row[1] is True
    assert index_row[2] is True


def test_should_repair_a_missing_token_leading_map_unit_covering_index(
    alembic_engine: Engine,
) -> None:
    _upgrade_to_channel_statistics(engine=alembic_engine)
    with alembic_engine.begin() as connection:
        connection.execute(
            text("DROP INDEX idx_document_map_unit_tokens_token_lookup")
        )

    _upgrade_to_heads(engine=alembic_engine)

    with alembic_engine.begin() as connection:
        index_state = connection.execute(
            text(
                """
                SELECT indexes.indisvalid, indexes.indisready
                FROM pg_index AS indexes
                JOIN pg_class AS classes ON classes.oid = indexes.indexrelid
                JOIN pg_namespace AS namespaces
                  ON namespaces.oid = classes.relnamespace
                WHERE namespaces.nspname = current_schema()
                  AND classes.relname = 'idx_document_map_unit_tokens_token_lookup'
                """
            )
        ).one()

    assert index_state[0] is True
    assert index_state[1] is True


def test_should_repair_a_missing_covering_index_with_a_caller_owned_connection(
    alembic_engine: Engine,
) -> None:
    _upgrade_to_channel_statistics(engine=alembic_engine)
    with alembic_engine.begin() as connection:
        connection.execute(
            text("DROP INDEX idx_document_map_unit_tokens_token_lookup")
        )

    _upgrade_to_heads_with_external_connection(engine=alembic_engine)

    with alembic_engine.begin() as connection:
        index_state = connection.execute(
            text(
                """
                SELECT indexes.indisvalid, indexes.indisready
                FROM pg_index AS indexes
                JOIN pg_class AS classes ON classes.oid = indexes.indexrelid
                JOIN pg_namespace AS namespaces
                  ON namespaces.oid = classes.relnamespace
                WHERE namespaces.nspname = current_schema()
                  AND classes.relname = 'idx_document_map_unit_tokens_token_lookup'
                """
            )
        ).one()

    assert index_state[0] is True
    assert index_state[1] is True


def test_should_repair_an_invalid_token_leading_map_unit_covering_index(
    alembic_engine: Engine,
) -> None:
    _upgrade_to_channel_statistics(engine=alembic_engine)
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE pg_index
                SET indisvalid = FALSE,
                    indisready = FALSE
                WHERE indexrelid =
                    'idx_document_map_unit_tokens_token_lookup'::regclass
                """
            )
        )

    _upgrade_to_heads(engine=alembic_engine)

    with alembic_engine.begin() as connection:
        index_state = connection.execute(
            text(
                """
                SELECT indexes.indisvalid, indexes.indisready
                FROM pg_index AS indexes
                JOIN pg_class AS classes ON classes.oid = indexes.indexrelid
                JOIN pg_namespace AS namespaces
                  ON namespaces.oid = classes.relnamespace
                WHERE namespaces.nspname = current_schema()
                  AND classes.relname = 'idx_document_map_unit_tokens_token_lookup'
                """
            )
        ).one()

    assert index_state[0] is True
    assert index_state[1] is True


def test_should_add_per_channel_map_unit_bm25_statistics(
    migrated_head_engine: Engine,
) -> None:
    with migrated_head_engine.begin() as connection:
        columns = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                text(
                    """
                    SELECT column_name, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'document_map_unit_indexes'
                      AND column_name IN (
                        'path_document_count', 'path_total_length',
                        'content_document_count', 'content_total_length'
                      )
                    """
                )
            ).all()
        }

    assert columns == {
        "path_document_count": "YES",
        "path_total_length": "YES",
        "content_document_count": "YES",
        "content_total_length": "YES",
    }
def test_should_create_content_trigram_index_for_regex_search(
    migrated_head_engine: Engine,
) -> None:
    with migrated_head_engine.begin() as connection:
        index_definition = connection.execute(
            text(
                """
                SELECT pg_get_indexdef(indexes.indexrelid)
                FROM pg_index AS indexes
                JOIN pg_class AS classes ON classes.oid = indexes.indexrelid
                JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace
                WHERE namespaces.nspname = current_schema()
                  AND classes.relname = 'idx_document_chunks_content_trgm'
                """
            )
        ).scalar_one()

    definition = str(index_definition)
    assert "USING gin (content gin_trgm_ops)" in definition
    assert "WHERE (content IS NOT NULL)" in definition


def test_should_upgrade_with_a_caller_owned_connection(
    alembic_engine: Engine,
) -> None:
    _upgrade_to_heads_with_external_connection(engine=alembic_engine)


def test_standalone_upgrade_should_preserve_a_caller_owned_connection(
    standalone_alembic_engine: Engine,
) -> None:
    _upgrade_to_heads_with_external_connection(engine=standalone_alembic_engine)


def test_should_replace_a_same_named_index_with_the_wrong_definition(
    alembic_engine: Engine,
) -> None:
    _upgrade_to_snapshot_parents(engine=alembic_engine)
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE INDEX idx_document_chunks_revision_snapshot_order
                ON document_chunks (document_id)
                """
            )
        )

    _upgrade_to_heads(engine=alembic_engine)

    with alembic_engine.begin() as connection:
        index_definition = connection.execute(
            text(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'document_chunks'
                  AND indexname = 'idx_document_chunks_revision_snapshot_order'
                """
            )
        ).scalar_one()

    assert "(document_id, job_result_id, sort_order, chunk_id, id)" in str(
        index_definition
    )


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
