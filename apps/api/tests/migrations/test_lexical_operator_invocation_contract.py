"""Every physical invocation remains consumed across ownership replacement."""

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

_ROOT: Path = Path(__file__).resolve().parents[4]
_REVISION: str = "6d7e8f901a2b"
_MIGRATION: Path = _ROOT / "apps/api/alembic/versions/6d7e8f901a2b_add_lexical_operator_invocations.py"


def _loadMigration() -> ModuleType:
    assert _MIGRATION.is_file(), "All-time invocation exclusion needs a new additive durable ledger migration"
    specification = importlib.util.spec_from_file_location("operator_invocation_migration_contract", _MIGRATION)
    assert specification is not None and specification.loader is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    return migration


def test_invocation_ledger_is_the_single_additive_head() -> None:
    migration = _loadMigration()
    assert migration.revision == _REVISION and migration.down_revision == "5c6d7e8f901a"
    configuration: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    scripts = ScriptDirectory.from_config(configuration)
    assert len(scripts.get_heads()) == 1 and scripts.get_revision(_REVISION) is not None


def test_ledger_backfills_current_owner_and_replay_never_erases_consumed_tokens(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _loadMigration()
    models = importlib.import_module("shared.models.database")
    model = getattr(models, "LexicalOperatorInvocation", None)
    assert model is not None, "The invocation ledger must be registered in shared metadata"
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
    token: str = str(uuid4())
    with alembic_engine.begin() as connection:
        LexicalBackendControl.__table__.create(connection)
        LexicalOperatorIssuer.__table__.create(connection)
        connection.execute(LexicalBackendControl.__table__.insert(), {"id": 1, "backend": "opensearch", "mode": "gated", "epoch": 3})
        connection.execute(LexicalOperatorIssuer.__table__.insert(), {"logical_cluster_id": "cluster", "control_id": 1,
            "invocation_token": token, "host_identity": "a" * 64, "operation_id": "operation", "state": "unknown",
            "source_hash": "b" * 64, "epoch": 3, "cluster_uuid": "native"})
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        rows = connection.execute(text("SELECT invocation_token, logical_cluster_id, recorded_at FROM lexical_operator_invocations")).all()
        assert len(rows) == 1 and rows[0][0:2] == (token, "cluster") and rows[0][2].tzinfo is not None
    assert {column["name"] for column in inspect(alembic_engine).get_columns("lexical_operator_invocations")} == {
        "invocation_token", "logical_cluster_id", "recorded_at"}
    for statement in ("UPDATE lexical_operator_invocations SET logical_cluster_id='changed'",
                      "DELETE FROM lexical_operator_invocations", "TRUNCATE lexical_operator_invocations"):
        with alembic_engine.begin() as connection, pytest.raises(DBAPIError, match="append-only"):
            connection.execute(text(statement))
    with alembic_engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("INSERT INTO lexical_operator_invocations (invocation_token,logical_cluster_id) VALUES (:token,'cluster')"), {"token": token})
    with pytest.raises(RuntimeError, match="Retain"):
        migration.downgrade()
