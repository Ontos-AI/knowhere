from datetime import datetime, timedelta, timezone

from shared.core.config import settings


def get_remaining_source_upload_seconds(created_at: datetime) -> int:
    """Give guarded uploads one lifetime across initial signing, renewal and handoff."""
    created_utc = (
        created_at
        if created_at.tzinfo is not None
        else created_at.replace(tzinfo=timezone.utc)
    )
    deadline = created_utc + timedelta(seconds=settings.JOB_WAITING_EXPIRE_SECONDS)
    return max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
