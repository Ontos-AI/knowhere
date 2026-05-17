from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.core.exceptions.domain_exceptions import NotFoundException
from shared.services.redis.redis_sync_service import (
    SyncJobInfoRedisService,
    SyncJobMetadataService,
)


@dataclass(frozen=True)
class UrlUploadContext:
    s3_key: str


def load_url_upload_context(job_id: str, redis_service: Any) -> UrlUploadContext:
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)

    if job_info:
        raw_s3_key = job_info.get("s3_key")
    else:
        metadata_service = SyncJobMetadataService(redis_service)
        job_metadata = metadata_service.get_metadata(job_id)
        if not job_metadata:
            raise NotFoundException(
                resource="JobInfo",
                resource_id=job_id,
                internal_message="Job info not found in Redis or Metadata",
            )
        raw_s3_key = job_metadata.get("s3_key")

    if not raw_s3_key:
        raise NotFoundException(
            resource="JobInfo",
            resource_id="s3_key",
            internal_message=f"Missing s3_key in Redis job info for job_id={job_id}",
        )

    return UrlUploadContext(s3_key=str(raw_s3_key))
