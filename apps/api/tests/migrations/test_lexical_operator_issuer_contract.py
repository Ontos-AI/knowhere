"""Issuer authority survives replay, duplicate hosts and compatible rollback."""

from __future__ import annotations

from datetime import datetime
import importlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer

_ROOT: Path = Path(__file__).resolve().parents[4]
_REVISION: str = "5c6d7e8f901a"
_MIGRATION: Path = _ROOT / "apps/api/alembic/versions/5c6d7e8f901a_add_lexical_operator_issuer.py"


def _loadMigration() -> ModuleType:
    assert _MIGRATION.is_file(), "A new additive operator-issuer migration must follow the frozen maintenance head"
    specification = importlib.util.spec_from_file_location("operator_issuer_migration_contract", _MIGRATION)
    assert specification is not None and specification.loader is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    return migration


def _createSchema(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, type[LexicalOperatorIssuer]]:
    migration = _loadMigration()
    models = importlib.import_module("shared.models.database")
    model = getattr(models, "LexicalOperatorIssuer", None)
    assert model is not None, "The operator-issuer model must be registered in shared metadata"
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    with engine.begin() as connection:
        LexicalBackendControl.__table__.create(connection, checkfirst=True)
        connection.execute(LexicalBackendControl.__table__.insert(), [
            {"id": number, "backend": "opensearch", "mode": "gated", "epoch": 9}
            for number in (1, 2)
        ])
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
    return migration, cast("type[LexicalOperatorIssuer]", model)


def _values(**changes: object) -> dict[str, object]:
    return {"logicalClusterId": "reviewed-logical-cluster", "controlId": 1,
        "invocationToken": str(uuid4()), "hostIdentity": "a" * 64,
        "operationId": "reviewed-workflow", "state": "active", "sourceHash": "b" * 64,
        "epoch": 9, "clusterUuid": "expected-native-cluster", **changes}


def test_operator_issuer_migration_remains_on_the_single_additive_chain() -> None:
    migration = _loadMigration()
    assert migration.revision == _REVISION and migration.down_revision == "4b5c6d7e8f90"
    configuration: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    scripts: ScriptDirectory = ScriptDirectory.from_config(configuration)
    assert len(scripts.get_heads()) == 1 and scripts.get_revision(_REVISION) is not None


def test_operator_issuer_upgrade_replay_preserves_active_physical_ownership(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, model = _createSchema(alembic_engine, monkeypatch)
    values = _values()
    with Session(alembic_engine) as db:
        db.add(model(**values))
        db.commit()
    with alembic_engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
    with Session(alembic_engine) as db:
        issuer = db.scalars(select(model)).one()
        assert issuer.invocationToken == values["invocationToken"] and issuer.state == "active"
        assert isinstance(issuer.acquiredAt, datetime) and issuer.acquiredAt.tzinfo is not None
        assert issuer.releasedAt is None and issuer.fenceReceiptSha256 is None
    columns: set[str] = {column["name"] for column in inspect(alembic_engine).get_columns("lexical_operator_issuers")}
    assert columns == {"logical_cluster_id", "control_id", "invocation_token", "host_identity", "operation_id",
        "state", "source_hash", "epoch", "cluster_uuid", "acquired_at", "released_at", "fence_receipt_sha256"}
    with pytest.raises(RuntimeError, match="Retain"):
        migration.downgrade()


@pytest.mark.parametrize("changes", [
    {"logicalClusterId": "another-key"},
    {"controlId": 2},
    {"logicalClusterId": "another-key", "controlId": 2, "invocationToken": "11111111-1111-4111-8111-111111111111"},
])
def test_operator_issuer_unique_keys_reject_alias_and_invocation_reuse(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    _, model = _createSchema(alembic_engine, monkeypatch)
    with Session(alembic_engine) as db:
        db.add(model(**_values(invocationToken="11111111-1111-4111-8111-111111111111")))
        db.commit()
    with Session(alembic_engine) as db:
        db.add(model(**_values(**changes)))
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize("changes", [{"state": "expired"}, {"epoch": 0}, {"controlId": 999}])
def test_operator_issuer_rejects_invalid_authority_state(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    _, model = _createSchema(alembic_engine, monkeypatch)
    with Session(alembic_engine) as db:
        db.add(model(**_values(**changes)))
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize("state", ["active", "unknown", "released"])
def test_operator_issuer_accepts_only_the_declared_lifecycle_without_expiry(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    _, model = _createSchema(alembic_engine, monkeypatch)
    with Session(alembic_engine) as db:
        db.add(model(**_values(state=state)))
        db.commit()
        assert db.scalars(select(model)).one().state == state
