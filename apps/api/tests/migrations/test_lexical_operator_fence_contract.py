"""Durable fence reservations retain proof across replay and disconnection."""

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

_ROOT: Path = Path(__file__).resolve().parents[4]
_REVISION: str = "8f901a2b3c4d"


def _loadMigration() -> ModuleType:
    path: Path = _ROOT / "apps/api/alembic/versions/8f901a2b3c4d_add_lexical_operator_fences.py"
    assert path.is_file(), "Physical fence intent must have an additive durable schema"
    specification = importlib.util.spec_from_file_location("operator_fence_migration_contract", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_operator_fence_is_on_the_single_additive_chain() -> None:
    migration = _loadMigration()
    assert migration.revision == _REVISION and migration.down_revision == "7e8f901a2b3c"
    configuration: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    scripts: ScriptDirectory = ScriptDirectory.from_config(configuration)
    assert len(scripts.get_heads()) == 1 and scripts.get_revision(_REVISION) is not None


def test_fence_schema_replays_without_removing_proof_and_refuses_second_old_owner_intent(
    alembic_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _loadMigration()
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicaloperatorinvocation import LexicalOperatorInvocation
    from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
    owner: str = str(uuid4())
    token: str = str(uuid4())
    with alembic_engine.begin() as connection:
        LexicalBackendControl.__table__.create(connection)
        LexicalOperatorIssuer.__table__.create(connection)
        LexicalOperatorInvocation.__table__.create(connection)
        connection.execute(LexicalBackendControl.__table__.insert(), {"id": 1, "backend": "opensearch", "mode": "gated", "epoch": 7})
        connection.execute(LexicalOperatorIssuer.__table__.insert(), {"logical_cluster_id": "logical", "control_id": 1,
            "invocation_token": owner, "host_identity": "a" * 64, "operation_id": "operation", "state": "unknown",
            "source_hash": "b" * 64, "epoch": 7, "cluster_uuid": "native"})
        connection.execute(LexicalOperatorInvocation.__table__.insert(), {"invocation_token": owner, "logical_cluster_id": "logical"})
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        connection.execute(text("""INSERT INTO lexical_operator_fences (old_invocation_token,fence_token,
            old_binding_sha256,source_hash,epoch,cluster_uuid,control_backend,state,proof_sha256,proven_at)
            VALUES (:owner,:token,:binding,:source,7,'native','opensearch','proven',:proof,now())"""),
            {"owner": owner, "token": token, "binding": "c" * 64, "source": "d" * 64, "proof": "e" * 64})
        migration.upgrade()
        assert connection.execute(text("SELECT fence_token,state,proof_sha256 FROM lexical_operator_fences")).one() == (token, "proven", "e" * 64)
    with alembic_engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("""INSERT INTO lexical_operator_fences (old_invocation_token,fence_token,
            old_binding_sha256,source_hash,epoch,cluster_uuid,control_backend)
            VALUES (:owner,:token,:binding,:source,7,'native','opensearch')"""),
            {"owner": owner, "token": str(uuid4()), "binding": "c" * 64, "source": "d" * 64})
    with alembic_engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE lexical_operator_fences SET proof_sha256=NULL"))
    with pytest.raises(RuntimeError, match="Retain"):
        migration.downgrade()
