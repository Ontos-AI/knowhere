"""Real PostgreSQL contract for resetting hit stats in Hipo namespaces."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

_ROOT: Path = Path(__file__).resolve().parents[4]
_PARENT_REVISION: str = "e108c52f6bf8"
_HIPO_NAMESPACE: str = "hipo-7d2f8a10-4c1b-4e5a-9f3d-2b6c8e0a1f47"
_OTHER_NAMESPACE: str = "customer-namespace"


def _build_config() -> Config:
    config: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    return config


def _namespaces(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            row[0]
            for row in connection.execute(text("SELECT namespace FROM retrieval_hit_stats"))
        }


def test_should_delete_only_hipo_namespace_hit_stats(alembic_engine: Engine) -> None:
    config: Config = _build_config()
    command.upgrade(config, _PARENT_REVISION)
    with alembic_engine.begin() as connection:
        # Hit stats reference documents; the rows here only need their namespace.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        for index, namespace in enumerate((_HIPO_NAMESPACE, _OTHER_NAMESPACE)):
            connection.execute(
                text(
                    "INSERT INTO retrieval_hit_stats (id, user_id, namespace, hit_kind, "
                    "document_id, hit_count, last_hit_at, created_at, updated_at) VALUES "
                    "(:id, 'reader', :namespace, 'document', :document_id, 5, now(), now(), now())"
                ),
                {"id": f"rhs_{index}", "namespace": namespace, "document_id": f"doc_{index}"},
            )
    assert _namespaces(alembic_engine) == {_HIPO_NAMESPACE, _OTHER_NAMESPACE}

    command.upgrade(config, "heads")
    assert _namespaces(alembic_engine) == {_OTHER_NAMESPACE}

    command.downgrade(config, _PARENT_REVISION)
    assert _namespaces(alembic_engine) == {_OTHER_NAMESPACE}
