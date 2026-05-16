from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.utc_now import utc_now_naive


@dataclass(frozen=True)
class JobResultDelivery:
    result: dict[str, Any] | None
    result_url: str | None
    result_url_expires_at: datetime | None


class JobResultDeliveryResolver:
    """Resolve the public delivery fields exposed for a terminal Job Result."""

    def __init__(self, *, storage: JobFileStorage | None = None) -> None:
        self._storage = storage or JobFileStorage()

    def resolve(
        self,
        job_result: Any | None,
        *,
        default_expires_at: datetime | None = None,
    ) -> JobResultDelivery:
        result = None
        result_url = None
        result_url_expires_at = default_expires_at

        if not job_result:
            return JobResultDelivery(
                result=result,
                result_url=result_url,
                result_url_expires_at=result_url_expires_at,
            )

        inline_payload = getattr(job_result, "inline_payload", None)
        if inline_payload:
            result = inline_payload

        result_s3_key = getattr(job_result, "result_s3_key", None)
        if result_s3_key:
            url_info = self._storage.generate_download_url(
                result_s3_key,
                bucket=self._storage.results_bucket,
            )
            result_url = url_info["download_url"]
            expires_in = int(url_info.get("expires_in", 3600))
            result_url_expires_at = utc_now_naive() + timedelta(seconds=expires_in)

        return JobResultDelivery(
            result=result,
            result_url=result_url,
            result_url_expires_at=result_url_expires_at,
        )

    def enrich_payload(
        self,
        payload: dict[str, Any],
        *,
        job_result: Any | None,
    ) -> dict[str, Any]:
        if payload.get("event") != "job.completed":
            return payload

        delivery = self.resolve(job_result)
        enriched = dict(payload)
        if delivery.result_url:
            enriched["result_url"] = delivery.result_url
        if delivery.result:
            enriched["result"] = delivery.result
        return enriched
