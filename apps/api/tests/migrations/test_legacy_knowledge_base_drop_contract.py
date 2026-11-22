"""Real PostgreSQL contract for dropping the legacy knowledge_base tables."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

_ROOT: Path = Path(__file__).resolve().parents[4]
_PARENT_REVISION: str = "b2354e6f7081"
_LEGACY_TABLES: set[str] = {"knowledge_base", "path_base"}


def _build_config() -> Config:
    config: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    return config


def _legacy_tables(engine: Engine) -> set[str]:
    return _LEGACY_TABLES & set(inspect(engine).get_table_names())


def test_should_drop_legacy_tables_and_restore_them_on_downgrade(
    alembic_engine: Engine,
) -> None:
    config: Config = _build_config()

    command.upgrade(config, _PARENT_REVISION)
    assert _legacy_tables(alembic_engine) == _LEGACY_TABLES

    command.upgrade(config, "heads")
    assert _legacy_tables(alembic_engine) == set()

    command.downgrade(config, _PARENT_REVISION)
    assert _legacy_tables(alembic_engine) == _LEGACY_TABLES

    command.upgrade(config, "heads")
    assert _legacy_tables(alembic_engine) == set()
