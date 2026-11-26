"""Periodic Redis lock windows match the hourly Beat slot."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-periodic-lock-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")


def test_periodic_lock_windows_match_the_hourly_schedule() -> None:
    from app.core.tasks import stale_job_sweeper, webhook_tasks

    assert stale_job_sweeper._TASK_PERIOD_SECONDS == 3600
    assert webhook_tasks._WEBHOOK_RECOVERY_PERIOD_SECONDS == 3600
