"""Offline verification capacity survives process loss and migration replay."""

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
    from shared.models.database.lexicalofflinereservation import (
        LexicalOfflineReservation,
    )

_ROOT: Path = Path(__file__).resolve().parents[4]
_REVISION: str = "7e8f901a2b3c"
_MIGRATION: Path = (
    _ROOT / "apps/api/alembic/versions/7e8f901a2b3c_add_lexical_offline_reservations.py"
)
_TOKEN: str = "00000000-0000-0000-0000-000000000001"


def _loadMigration() -> ModuleType:
    assert _MIGRATION.is_file(), (
        "Offline restore capacity needs a new additive durable reservation migration"
    )
    specification = importlib.util.spec_from_file_location(
        "offline_reservation_migration_contract", _MIGRATION
    )
    assert specification is not None and specification.loader is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    return migration


def _createSchema(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, type[LexicalOfflineReservation]]:
    migration = _loadMigration()
    models = importlib.import_module("shared.models.database")
    model = getattr(models, "LexicalOfflineReservation", None)
    assert model is not None, (
        "Durable offline reservations must be registered in shared metadata"
    )
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
    from shared.models.database.lexicaloperatorinvocation import (
        LexicalOperatorInvocation,
    )

    with engine.begin() as connection:
        LexicalBackendControl.__table__.create(connection)
        LexicalOperatorIssuer.__table__.create(connection)
        LexicalOperatorInvocation.__table__.create(connection)
        connection.execute(
            LexicalBackendControl.__table__.insert(),
            {"id": 1, "backend": "opensearch", "mode": "gated", "epoch": 1},
        )
        connection.execute(
            LexicalOperatorIssuer.__table__.insert(),
            {
                "logical_cluster_id": "source",
                "control_id": 1,
                "invocation_token": _TOKEN,
                "host_identity": "a" * 64,
                "operation_id": "approved-operation",
                "state": "active",
                "source_hash": "b" * 64,
                "epoch": 1,
                "cluster_uuid": "source",
            },
        )
        connection.execute(
            LexicalOperatorInvocation.__table__.insert(),
            {"invocation_token": _TOKEN, "logical_cluster_id": "source"},
        )
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection))
        )
        migration.upgrade()
    return migration, cast("type[LexicalOfflineReservation]", model)


def _values(**changes: object) -> dict[str, object]:
    return {
        "targetClusterUuid": "offline-target",
        "indexName": "verify-00000000000000000000000000000001-knowhere-scope",
        "targetIncarnation": "offline-node",
        "sourceClusterUuid": "source",
        "state": "uncertain",
        "operationId": str(uuid4()),
        "issuerToken": _TOKEN,
        "snapshotUuid": "snapshot",
        "batchReservationId": str(uuid4()),
        "inventorySha256": "c" * 64,
        "boundarySha256": "d" * 64,
        "indexUuid": None,
        **changes,
    }


def test_offline_reservation_follows_the_single_additive_migration_chain() -> None:
    migration = _loadMigration()
    assert migration.revision == _REVISION and migration.down_revision == "6d7e8f901a2b"
    configuration = Config(str(_ROOT / "apps/api/alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    scripts: ScriptDirectory = ScriptDirectory.from_config(configuration)
    assert len(scripts.get_heads()) == 1 and scripts.get_revision(_REVISION) is not None


def test_replayed_upgrade_retains_unknown_capacity_and_has_no_expiry(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration, model = _createSchema(alembic_engine, monkeypatch)
    values = _values()
    with Session(alembic_engine) as db:
        db.add(model(**values))
        db.commit()
    with alembic_engine.begin() as connection:
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection))
        )
        migration.upgrade()
    with Session(alembic_engine) as db:
        reservation = db.scalars(select(model)).one()
        assert reservation.state == "uncertain" and reservation.issuerToken == _TOKEN
        assert (
            reservation.indexUuid is None
            and reservation.operationId == values["operationId"]
        )
        assert (
            isinstance(reservation.createdAt, datetime)
            and reservation.createdAt.tzinfo is not None
        )
        assert (
            isinstance(reservation.updatedAt, datetime)
            and reservation.updatedAt.tzinfo is not None
        )
    assert set(
        inspect(alembic_engine).get_pk_constraint("lexical_offline_reservations")[
            "constrained_columns"
        ]
    ) == {"target_cluster_uuid", "index_name"}
    assert {
        column["name"]
        for column in inspect(alembic_engine).get_columns(
            "lexical_offline_reservations"
        )
    } == {
        "target_cluster_uuid",
        "index_name",
        "target_incarnation",
        "source_cluster_uuid",
        "state",
        "operation_id",
        "issuer_token",
        "snapshot_uuid",
        "batch_reservation_id",
        "inventory_sha256",
        "boundary_sha256",
        "index_uuid",
        "created_at",
        "updated_at",
    }
    with pytest.raises(RuntimeError, match="Retain"):
        migration.downgrade()


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"state": "expired"},
        {"issuerToken": "00000000-0000-0000-0000-000000000099"},
        {"targetClusterUuid": "source"},
    ],
)
def test_capacity_keys_and_issuer_identity_cannot_be_reused(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    _, model = _createSchema(alembic_engine, monkeypatch)
    if not changes:
        with Session(alembic_engine) as db:
            db.add(model(**_values()))
            db.commit()
    with Session(alembic_engine) as db:
        db.add(model(**_values(**changes)))
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize("state", ["opening", "open", "closing", "uncertain", "closed"])
def test_only_declared_offline_lifecycle_is_persisted(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _, model = _createSchema(alembic_engine, monkeypatch)
    with Session(alembic_engine) as db:
        db.add(model(**_values(state=state)))
        db.commit()
        assert db.scalars(select(model)).one().state == state
