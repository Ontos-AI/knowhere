from __future__ import annotations

from typing import Any

from loguru import logger

from shared.services.retrieval.row_utils import MEDIA_CHUNK_TYPES, normalize_chunk_type
from shared.services.storage.result_storage import get_result_storage


def _normalize_artifact_ref(asset_ref: object) -> str | None:
    return get_result_storage().normalize_artifact_ref(
        None if asset_ref is None else str(asset_ref)
    )


def _is_retrieval_media_row(row: dict[str, Any]) -> bool:
    raw_chunk_type = row.get("chunk_type") or row.get("type")
    return normalize_chunk_type(raw_chunk_type) in MEDIA_CHUNK_TYPES


def _resolve_asset_request(row: dict[str, Any]) -> tuple[str, str] | None:
    job_id = str(row.get("job_id") or "").strip()
    if not job_id or not _is_retrieval_media_row(row):
        return None

    artifact_ref = _normalize_artifact_ref(row.get("file_path"))
    if artifact_ref is None:
        return None

    return job_id, artifact_ref


async def _generate_retrieval_asset_url(
    *,
    row: dict[str, Any],
    log_context: str,
) -> str | None:
    request = _resolve_asset_request(row)
    if request is None:
        return None

    job_id, artifact_ref = request
    try:
        return get_result_storage().generate_artifact_url(
            job_id=job_id,
            artifact_ref=artifact_ref,
        )
    except Exception as exc:
        logger.warning(f"Failed to generate {log_context} asset URL (ignored): {exc}")
        return None


async def enrich_rows_with_retrieval_asset_urls(
    rows: list[dict[str, Any]],
    *,
    log_context: str,
) -> list[dict[str, Any]]:
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        asset_url = await _generate_retrieval_asset_url(
            row=row,
            log_context=log_context,
        )
        if asset_url:
            enriched["asset_url"] = asset_url
        enriched_rows.append(enriched)
    return enriched_rows


async def build_retrieval_asset_url_map(
    rows: list[dict[str, Any]],
    *,
    log_context: str,
) -> dict[str, str]:
    url_map: dict[str, str] = {}
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "").strip()
        if not chunk_id:
            continue

        asset_url = await _generate_retrieval_asset_url(
            row=row,
            log_context=log_context,
        )
        if asset_url:
            url_map[chunk_id] = asset_url
    return url_map


def is_client_result_artifact_ref(asset_ref: str | None) -> bool:
    return _normalize_artifact_ref(asset_ref) is not None
