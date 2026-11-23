"""Document importance: capped usage heat plus upload freshness, 30-day half-life."""

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("S3_BUCKET_NAME", "contract-bucket")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")

from shared.services.retrieval.stats.service import (
    compute_importance_multiplier,
    compute_importance_score,
)


def _days_ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_unused_document_importance_is_upload_freshness() -> None:
    assert compute_importance_score(0, None, _days_ago(0)) == pytest.approx(0.3, abs=1e-3)
    assert compute_importance_score(0, None, _days_ago(30)) == pytest.approx(0.15, abs=1e-3)


@pytest.mark.parametrize(
    ("uses", "usage_heat"),
    [(1, 1 / 3), (3, 0.6), (10, 10 / 12)],
)
def test_use_count_is_capped_by_n_over_n_plus_two(uses: int, usage_heat: float) -> None:
    expected = 0.7 * usage_heat + 0.3
    assert compute_importance_score(uses, _days_ago(0), _days_ago(0)) == pytest.approx(
        expected, abs=1e-3
    )


def test_usage_heat_halves_after_thirty_days_without_use() -> None:
    uploaded = _days_ago(400)
    recent = compute_importance_score(3, _days_ago(0), uploaded)
    month_old = compute_importance_score(3, _days_ago(30), uploaded)
    assert month_old == pytest.approx(recent / 2, abs=1e-3)


def test_importance_stays_within_zero_and_one() -> None:
    assert 0.0 <= compute_importance_score(0, None, _days_ago(10_000)) <= 1.0
    assert 0.0 <= compute_importance_score(10_000, _days_ago(0), _days_ago(0)) <= 1.0
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert compute_importance_score(1, naive_now, naive_now) == pytest.approx(
        0.7 / 3 + 0.3, abs=1e-3
    )


@pytest.mark.parametrize(
    ("importance", "multiplier"),
    [(0.0, 0.7), (0.3, 0.94), (1.0, 1.5), (-1.0, 0.7), (2.0, 1.5)],
)
def test_multiplier_is_fixed_between_point_seven_and_one_point_five(
    importance: float, multiplier: float
) -> None:
    assert compute_importance_multiplier(importance) == multiplier
