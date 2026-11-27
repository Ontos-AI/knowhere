"""Documents age by the namespace's active days, not by calendar days."""

import os
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("S3_BUCKET_NAME", "contract-bucket")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")

from shared.services.retrieval.stats.active_days import ActiveDays, utc_day

TODAY = date(2026, 11, 27)


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour)


def test_a_moment_on_the_latest_active_day_has_age_zero() -> None:
    days = ActiveDays.of([], TODAY)
    assert days.age_after(_at(TODAY)) == 0


def test_today_counts_without_a_record() -> None:
    yesterday = TODAY - timedelta(days=1)
    assert ActiveDays.of([], TODAY).age_after(_at(yesterday)) == 1
    assert ActiveDays.of([TODAY], TODAY).days == (TODAY,)


def test_days_nobody_searched_do_not_add_age() -> None:
    uploaded = TODAY - timedelta(days=100)
    recorded = [TODAY - timedelta(days=60), TODAY - timedelta(days=10)]
    assert ActiveDays.of(recorded, TODAY).age_after(_at(uploaded)) == 3


def test_activity_on_the_moment_s_own_day_does_not_count() -> None:
    uploaded = TODAY - timedelta(days=5)
    recorded = [uploaded, TODAY - timedelta(days=2)]
    assert ActiveDays.of(recorded, TODAY).age_after(_at(uploaded, hour=0)) == 2


def test_aware_and_naive_moments_use_the_same_utc_day() -> None:
    aware = datetime(2026, 11, 27, 1, tzinfo=timezone(timedelta(hours=8)))
    assert utc_day(aware) == date(2026, 11, 26)
    assert utc_day(datetime(2026, 11, 26, 17)) == date(2026, 11, 26)
