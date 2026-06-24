from __future__ import annotations

from typing import Any

from loguru import logger

from shared.services.retrieval.hydration.row_utils import MEDIA_CHUNK_TYPES, normalize_chunk_type
from shared.services.storage.result_storage import get_result_storage

AssetUrlValue = str | list[str]


def _normalize_artifact_ref(asset_ref: object) -> str | None:
    return get_result_storage().normalize_artifact_ref(
        None if asset_ref is None else str(asset_ref)
    )


def _is_retrieval_media_row(row: dict[str, Any]) -> bool:
    raw_chunk_type = row.get("chunk_type") or row.get("type")
    return normalize_chunk_type(raw_chunk_type) in MEDIA_CHUNK_TYPES


def _is_page_row(row: dict[str, Any]) -> bool:
    raw_chunk_type = row.get("chunk_type") or row.get("type")
    return normalize_chunk_type(raw_chunk_type) == "page"


def _resolve_asset_request(row: dict[str, Any]) -> tuple[str, str] | None:
    job_id = str(row.get("job_id") or "").strip()
    if not job_id or not _is_retrieval_media_row(row):
        return None

    artifact_ref = _normalize_artifact_ref(row.get("file_path"))
    if artifact_ref is None:
        return None

    return job_id, artifact_ref


def _resolve_page_asset_requests(row: dict[str, Any]) -> list[tuple[str, str]]:
    job_id = str(row.get("job_id") or "").strip()
    if not job_id or not _is_page_row(row):
        return []

    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []

    requests: list[tuple[str, str]] = []
    seen_refs: set[str] = set()
    raw_refs = metadata.get("page_image_uris") or []
    if not isinstance(raw_refs, list):
        return []
    for raw_ref in raw_refs:
        artifact_ref = _normalize_artifact_ref(raw_ref)
        if (
            artifact_ref is None
            or not artifact_ref.startswith("pages/")
            or artifact_ref in seen_refs
        ):
            continue
        seen_refs.add(artifact_ref)
        requests.append((job_id, artifact_ref))
    return requests


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


async def _generate_retrieval_asset_urls(
    *,
    row: dict[str, Any],
    log_context: str,
) -> list[str]:
    requests = _resolve_page_asset_requests(row)
    if not requests:
        return []

    urls: list[str] = []
    for job_id, artifact_ref in requests:
        try:
            url = get_result_storage().generate_artifact_url(
                job_id=job_id,
                artifact_ref=artifact_ref,
            )
        except Exception as exc:
            logger.warning(f"Failed to generate {log_context} page asset URL (ignored): {exc}")
            continue
        if url:
            urls.append(url)
    return urls


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
        asset_urls = await _generate_retrieval_asset_urls(
            row=row,
            log_context=log_context,
        )
        if asset_urls:
            enriched["asset_urls"] = asset_urls
        enriched_rows.append(enriched)
    return enriched_rows


async def build_retrieval_asset_url_map(
    rows: list[dict[str, Any]],
    *,
    log_context: str,
) -> dict[str, AssetUrlValue]:
    url_map: dict[str, AssetUrlValue] = {}
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "").strip()
        if not chunk_id:
            continue

        asset_urls = await _generate_retrieval_asset_urls(
            row=row,
            log_context=log_context,
        )
        if asset_urls:
            url_map[chunk_id] = asset_urls
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
