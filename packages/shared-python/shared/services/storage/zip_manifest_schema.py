"""Manifest projection for Knowhere ZIP result packages."""

from __future__ import annotations

from typing import Any

from shared.utils.utc_now import utc_now_naive


class ZipManifestBuilder:
    def generate_manifest(
        self,
        *,
        job_id: str,
        data_id: str | None,
        source_file_name: str,
        statistics: dict[str, Any],
        job_metadata: dict[str, Any],
        hierarchy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "version": "2.0",
            "job_id": job_id,
            "data_id": data_id,
            "source_file_name": source_file_name,
            "processing_date": utc_now_naive().isoformat() + "Z",
            "processing": {
                "page_count": job_metadata.get("page_count"),
                "billing_status": job_metadata.get("billing_status"),
                "cost": {
                    "micro_dollars": job_metadata.get("billing_amount_micro_dollars"),
                    "credits": job_metadata.get("billing_credits"),
                },
                "timing": {
                    "started_at": job_metadata.get("processing_started_at"),
                    "completed_at": job_metadata.get("processing_completed_at"),
                    "duration_ms": job_metadata.get("processing_duration_ms"),
                },
                "stages": job_metadata.get("stages", {}),
            },
            "statistics": statistics,
            "HIERARCHY": hierarchy or {},
        }
