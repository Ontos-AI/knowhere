"""Manifest projection for Knowhere ZIP result packages."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from shared.services.ai.token_costing import build_token_cost_estimate
from shared.utils.utc_now import utc_now_naive


def extract_manifest_token_usage(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return token usage embedded in a manifest, if present."""
    processing = manifest.get("processing")
    if not isinstance(processing, dict):
        return {}

    stages = processing.get("stages")
    if isinstance(stages, dict):
        usage = stages.get("token_usage")
        if isinstance(usage, dict):
            return usage

    usage = processing.get("token_usage")
    if isinstance(usage, dict):
        return usage
    return {}


def enrich_manifest_with_token_cost_estimate(manifest: dict[str, Any]) -> dict[str, Any]:
    """Add LLM cost_estimate to a local debug manifest copy."""
    enriched = deepcopy(manifest)
    processing = enriched.setdefault("processing", {})
    if not isinstance(processing, dict):
        return enriched
    token_usage = extract_manifest_token_usage(enriched)
    processing["cost_estimate"] = build_token_cost_estimate(token_usage)
    return enriched


def strip_manifest_cost_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    """Remove internal LLM cost fields before writing manifest into a ZIP."""
    stripped = deepcopy(manifest)
    processing = stripped.get("processing")
    if isinstance(processing, dict):
        processing.pop("cost_estimate", None)
    return stripped


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
        stages = job_metadata.get("stages", {})
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
                "stages": stages,
            },
            "statistics": statistics,
            "HIERARCHY": hierarchy or {},
        }
