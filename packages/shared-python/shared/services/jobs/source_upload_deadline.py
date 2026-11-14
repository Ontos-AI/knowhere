from datetime import datetime, timedelta, timezone
from typing import Literal

from shared.core.config import settings
from shared.core.config.job import DEFAULT_JOB_WAITING_EXPIRE_SECONDS
from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.storage.source_upload_verification import SourceUploadVerification

UPLOAD_EXPIRED_CODE = "UPLOAD_EXPIRED"
UPLOAD_EXPIRED_MESSAGE = "Upload expired: file was not uploaded within the allowed time window"
UPLOAD_INTEGRITY_MISMATCH_CODE = "UPLOAD_INTEGRITY_MISMATCH"
UPLOAD_INTEGRITY_MISMATCH_MESSAGE = "The uploaded file does not match the expected original"


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get_guarded_source_upload_lifetime_seconds() -> int:
    """Preserve the legacy sweeper sentinel while keeping guarded PUTs time-limited."""
    waiting_lifetime = settings.JOB_WAITING_EXPIRE_SECONDS
    return waiting_lifetime if waiting_lifetime > 0 else DEFAULT_JOB_WAITING_EXPIRE_SECONDS


def get_source_upload_deadline(created_at: datetime) -> datetime:
    return _as_utc(created_at) + timedelta(seconds=get_guarded_source_upload_lifetime_seconds())


def get_remaining_source_upload_seconds(created_at: datetime) -> int:
    deadline = get_source_upload_deadline(created_at)
    return max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))


def classify_guarded_source_upload(
    created_at: datetime,
    verification: SourceUploadVerification,
) -> Literal["matched", "missing", "mismatch", "expired"]:
    deadline = get_source_upload_deadline(created_at)
    if verification.status == "missing":
        # A HEAD that started before the cutoff cannot establish absence at it.
        return "expired" if _as_utc(verification.checked_at) >= deadline else "missing"
    if verification.last_modified is not None and _as_utc(verification.last_modified) > deadline:
        return "expired"
    if verification.status == "mismatch":
        return "mismatch"
    if verification.last_modified is None:
        raise UnavailableException(
            internal_message="S3 did not provide the source object's modification timestamp",
            user_message="Could not verify when the file was uploaded. Retry shortly.",
            retry_after=1,
        )
    return "matched"
