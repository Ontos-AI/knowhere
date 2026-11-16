from __future__ import annotations

import math

from app.services.document_parser.formats.media.probe import (
    admit_media_source,
    measure_media_duration_ms,
)
from app.services.document_parser.orchestration.format_router import (
    MEDIA_SOURCE_EXTENSIONS,
)
from sqlalchemy import select

from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    NotFoundException,
    ValidationException,
)
from shared.models.database.user_balance import UserBalance

# Tiers that never paid; every `tier_N` reached through payment is paid.
FREE_MEDIA_TIERS: frozenset[str] = frozenset({"free", "guest"})
_MS_PER_MINUTE = 60_000


def build_media_admission_rejection(
    *,
    file_extension: str,
    local_file_path: str,
    job_user_id: str | None,
) -> ValidationException | None:
    """Reject unreadable, silent, or over-long media before the job is charged."""
    if file_extension.lower() not in MEDIA_SOURCE_EXTENSIONS:
        return None
    try:
        info = admit_media_source(local_file_path)
        duration_ms = measure_media_duration_ms(local_file_path, info)
    except ValidationException as exc:
        return exc
    return _duration_rejection(
        duration_ms=duration_ms,
        job_user_id=job_user_id,
        subject="This recording is",
        unit="file",
        trim_hint="trim the file and upload it again",
        split_hint="Split it into shorter parts and upload them again.",
    )


def build_embedded_media_admission_rejection(
    *,
    embedded_media_durations_ms: tuple[int, ...],
    job_user_id: str | None,
) -> ValidationException | None:
    """Reject a presentation whose longest embedded recording is over the tier cap."""
    if not embedded_media_durations_ms:
        return None
    return _duration_rejection(
        duration_ms=max(embedded_media_durations_ms),
        job_user_id=job_user_id,
        subject="This presentation contains a recording that is",
        unit="recording",
        trim_hint="trim the recording in the presentation and upload it again",
        split_hint="Shorten the recording in the presentation and upload it again.",
    )


def _duration_rejection(
    *,
    duration_ms: int,
    job_user_id: str | None,
    subject: str,
    unit: str,
    trim_hint: str,
    split_hint: str,
) -> ValidationException | None:
    is_free_tier = _load_user_tier(job_user_id) in FREE_MEDIA_TIERS
    limit_seconds = (
        settings.MEDIA_MAX_DURATION_FREE_SECONDS
        if is_free_tier
        else settings.MEDIA_MAX_DURATION_PAID_SECONDS
    )
    if duration_ms <= limit_seconds * 1000:
        return None
    duration_minutes = math.ceil(duration_ms / _MS_PER_MINUTE)
    limit_minutes = limit_seconds * 1000 // _MS_PER_MINUTE
    if is_free_tier:
        paid_limit_minutes = (
            settings.MEDIA_MAX_DURATION_PAID_SECONDS * 1000 // _MS_PER_MINUTE
        )
        user_message = (
            f"{subject} {duration_minutes} minutes long. The free plan "
            f"supports up to {limit_minutes} minutes per {unit}. Upgrade to "
            f"process recordings up to {paid_limit_minutes} minutes, or "
            f"{trim_hint}."
        )
    else:
        user_message = (
            f"{subject} {duration_minutes} minutes long. The maximum is "
            f"{limit_minutes} minutes per {unit}. {split_hint}"
        )
    return ValidationException(
        user_message=user_message,
        violations=[
            {
                "field": "media_duration",
                "description": (
                    f"Duration {duration_ms} ms exceeds limit of "
                    f"{limit_seconds * 1000} ms"
                ),
            }
        ],
        internal_message=(
            "media duration over tier cap: "
            f"duration_ms={duration_ms}, limit_seconds={limit_seconds}, "
            f"free_tier={is_free_tier}"
        ),
    )


def _load_user_tier(job_user_id: str | None) -> str:
    if not job_user_id:
        raise NotFoundException(
            resource="JobInfo",
            resource_id="user_id",
            internal_message="Missing user_id for media duration admission",
        )
    with get_sync_db_context() as db:
        user_tier = db.execute(
            select(UserBalance.user_tier)
            .where(UserBalance.user_id == job_user_id)
            .limit(1)
        ).scalar_one_or_none()
    if user_tier is None:
        raise NotFoundException(
            resource="UserBalance",
            resource_id=job_user_id,
            internal_message=f"User tier not found for user_id={job_user_id}",
        )
    return str(user_tier)
