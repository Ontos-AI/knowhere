"""Real PostgreSQL contracts for gated and replayable token index maintenance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from shared.services.retrieval.maintenance.legacy_token_lookup import (
    LegacyTokenLookupMaintenance,
)

_ROOT: Path = Path(__file__).resolve().parents[4]
_PARENT_REVISION: str = "2b3c4d5e6f7a"
_REVISION: str = "3c4d5e6f7a8b"
_LEGACY_INDEX: str = "idx_document_map_unit_tokens_lookup"
_BINARY_INDEX: str = "idx_document_map_unit_tokens_token_lookup_binary"
_UNIT_LOOKUP_INDEX: str = "idx_document_map_unit_tokens_unit_lookup"


def _build_config() -> Config:
    config: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    return config


def _inspect_indexes(engine: Engine) -> dict[str, dict[str, str | bool]]:
    with engine.connect() as connection:
        return LegacyTokenLookupMaintenance(connection).inspect_indexes()


def _run_operator(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts/retire_legacy_token_lookup.py"), *arguments],
        env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=False,
    )


def test_should_keep_default_indexes_then_apply_and_restore_at_same_head(
    alembic_engine: Engine,
) -> None:
    config: Config = _build_config()
    command.upgrade(config, "heads")
    with alembic_engine.connect() as connection:
        initialRevision: str = str(
            connection.scalar(text("SELECT version_num FROM alembic_version"))
        )
    initial: dict[str, dict[str, str | bool]] = _inspect_indexes(alembic_engine)
    assert len(initial) == 5
    assert _LEGACY_INDEX in initial
    inspection: subprocess.CompletedProcess[str] = _run_operator()
    assert inspection.returncode == 0, inspection.stderr
    assert json.loads(inspection.stdout) == initial

    for _ in range(2):
        result: subprocess.CompletedProcess[str] = _run_operator("--apply")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            name: state for name, state in initial.items() if name != _LEGACY_INDEX
        }
    with alembic_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == initialRevision
    for _ in range(2):
        result = _run_operator("--restore")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == initial


@pytest.mark.parametrize("is_external_transaction", (False, True))
def test_should_gate_upgrade_and_restore_on_downgrade(
    alembic_engine: Engine, is_external_transaction: bool,
) -> None:
    config: Config = _build_config()
    command.upgrade(config, _PARENT_REVISION)
    initial: dict[str, dict[str, str | bool]] = _inspect_indexes(alembic_engine)
    config.cmd_opts = argparse.Namespace(x=["retire_legacy_token_lookup=true"])
    if is_external_transaction:
        with alembic_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, _REVISION)
        config.attributes.pop("connection")
    else:
        command.upgrade(config, _REVISION)
    assert _inspect_indexes(alembic_engine) == {
        name: state for name, state in initial.items() if name != _LEGACY_INDEX
    }
    if is_external_transaction:
        with alembic_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, _PARENT_REVISION)
    else:
        command.downgrade(config, _PARENT_REVISION)
    assert _inspect_indexes(alembic_engine) == initial


@pytest.mark.parametrize(
    ("index_name", "damage"),
    (
        (_BINARY_INDEX, "missing"),
        (_UNIT_LOOKUP_INDEX, "incompatible"),
        (_BINARY_INDEX, "invalid"),
        (_UNIT_LOOKUP_INDEX, "not_ready"),
    ),
)
def test_should_refuse_retirement_without_compatible_replacements(
    alembic_engine: Engine, index_name: str, damage: str,
) -> None:
    command.upgrade(_build_config(), "heads")
    with alembic_engine.begin() as connection:
        if damage in {"missing", "incompatible"}:
            connection.execute(text(f"DROP INDEX {index_name}"))
            if damage == "incompatible":
                connection.execute(text(
                    f"CREATE INDEX {index_name} ON document_map_unit_tokens (channel)"
                ))
        else:
            column: str = "indisvalid" if damage == "invalid" else "indisready"
            connection.execute(text(
                f"UPDATE pg_index SET {column} = false "
                "WHERE indexrelid = to_regclass(:name)"
            ), {"name": index_name})
    before: dict[str, dict[str, str | bool]] = _inspect_indexes(alembic_engine)
    result: subprocess.CompletedProcess[str] = _run_operator("--apply")
    assert result.returncode == 1
    assert index_name in result.stderr
    assert _inspect_indexes(alembic_engine) == before


def test_should_refuse_to_replace_incompatible_legacy_index(
    alembic_engine: Engine,
) -> None:
    command.upgrade(_build_config(), "heads")
    with alembic_engine.begin() as connection:
        connection.execute(text(f"DROP INDEX {_LEGACY_INDEX}"))
        connection.execute(text(
            f"CREATE INDEX {_LEGACY_INDEX} ON document_map_unit_tokens (channel)"
        ))
    before: dict[str, dict[str, str | bool]] = _inspect_indexes(alembic_engine)
    for argument in ("--restore", "--apply"):
        result: subprocess.CompletedProcess[str] = _run_operator(argument)
        assert result.returncode == 1
        assert _LEGACY_INDEX in result.stderr
        assert _inspect_indexes(alembic_engine) == before
