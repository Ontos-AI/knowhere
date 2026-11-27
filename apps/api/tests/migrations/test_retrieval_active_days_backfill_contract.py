"""Real PostgreSQL contract for backfilling active days from retrieval runs."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

_ROOT: Path = Path(__file__).resolve().parents[4]
_PARENT_REVISION: str = "809ee432a5f6"
_REVISION: str = "e71664cb1cee"


def _build_config() -> Config:
    config: Config = Config(str(_ROOT / "apps/api/alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "apps/api/alembic"))
    return config


def _active_days(engine: Engine) -> set[tuple[str, str, date]]:
    with engine.connect() as connection:
        return {
            (row[0], row[1], row[2])
            for row in connection.execute(
                text("SELECT user_id, namespace, day FROM retrieval_active_days")
            )
        }


def test_should_backfill_one_active_day_per_caller_namespace_and_utc_day(
    alembic_engine: Engine,
) -> None:
    config: Config = _build_config()
    command.upgrade(config, _PARENT_REVISION)
    runs: list[tuple[str, str, datetime]] = [
        ("reader", "brain", datetime(2026, 11, 20, 0, 5)),
        ("reader", "brain", datetime(2026, 11, 20, 23, 55)),
        ("reader", "brain", datetime(2026, 11, 22, 8)),
        ("reader", "other", datetime(2026, 11, 20, 8)),
        ("writer", "brain", datetime(2026, 11, 21, 8)),
    ]
    with alembic_engine.begin() as connection:
        for index, (user_id, namespace, created_at) in enumerate(runs):
            connection.execute(
                text(
                    "INSERT INTO retrieval_runs (run_id, user_id, namespace, query, query_hash, "
                    "top_k, policy_name, agentic_enabled, cache_hit, result_count, latency_ms, "
                    "created_at) VALUES (:run_id, :user_id, :namespace, 'q', '', 10, "
                    "'rule_based_v1', false, false, 0, 0, :created_at)"
                ),
                {
                    "run_id": f"aret_{index}",
                    "user_id": user_id,
                    "namespace": namespace,
                    "created_at": created_at,
                },
            )

    command.upgrade(config, _REVISION)
    assert _active_days(alembic_engine) == {
        ("reader", "brain", date(2026, 11, 20)),
        ("reader", "brain", date(2026, 11, 22)),
        ("reader", "other", date(2026, 11, 20)),
        ("writer", "brain", date(2026, 11, 21)),
    }

    command.downgrade(config, _PARENT_REVISION)
    assert "retrieval_active_days" not in inspect(alembic_engine).get_table_names()
