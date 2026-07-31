from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from shared.services.retrieval.hydration.row_utils import MEDIA_CHUNK_TYPES, normalize_chunk_type
from shared.services.storage.page_pdf_crop import crop_source_pdf_pages
from shared.services.storage.result_storage import get_result_storage

AssetUrlValue = str
PagePdfRequestKey = tuple[str, tuple[int, ...]]


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


def _metadata_for_row(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _coerce_page_nums(value: object) -> list[int]:
    if isinstance(value, list):
        raw_values = value
    elif value is None:
        raw_values = []
    else:
        raw_values = str(value).split(",")

    pages: list[int] = []
    for item in raw_values:
        try:
            pages.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return pages


def _resolve_page_pdf_request(row: dict[str, Any]) -> PagePdfRequestKey | None:
    job_id = str(row.get("job_id") or "").strip()
    if not job_id or not _is_page_row(row):
        return None

    metadata = _metadata_for_row(row)
    pages = _coerce_page_nums(metadata.get("page_nums") or row.get("page_nums"))
    if not pages:
        return None
    normalized_pages = tuple(sorted({page for page in pages if page > 0}))
    if not normalized_pages:
        return None
    return job_id, normalized_pages


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


async def _generate_page_pdf_asset_url_for_request(
    request: PagePdfRequestKey,
    *,
    log_context: str,
) -> str | None:
    job_id, pages = request
    try:
        return await asyncio.to_thread(
            crop_source_pdf_pages,
            job_id=job_id,
            pages=list(pages),
        )
    except Exception as exc:
        logger.warning(f"Failed to generate {log_context} page PDF URL (ignored): {exc}")
        return None


async def _build_page_pdf_url_lookup(
    rows: list[dict[str, Any]],
    *,
    log_context: str,
) -> dict[PagePdfRequestKey, str]:
    requests = {
        request
        for row in rows
        if (request := _resolve_page_pdf_request(row)) is not None
    }
    if not requests:
        return {}
    sorted_requests = sorted(requests)
    urls = await asyncio.gather(
        *(
            _generate_page_pdf_asset_url_for_request(
                request,
                log_context=log_context,
            )
            for request in sorted_requests
        )
    )
    return {
        request: url
        for request, url in zip(sorted_requests, urls, strict=True)
        if url
    }


async def enrich_rows_with_retrieval_asset_url(
    rows: list[dict[str, Any]],
    *,
    log_context: str,
) -> list[dict[str, Any]]:
    page_pdf_urls = await _build_page_pdf_url_lookup(rows, log_context=log_context)
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        metadata = dict(_metadata_for_row(row))
        metadata.pop("page_assets", None)
        if metadata or "chunk_metadata" in row or "metadata" in row:
            enriched["chunk_metadata"] = metadata
            enriched["metadata"] = metadata
        asset_url = await _generate_retrieval_asset_url(
            row=row,
            log_context=log_context,
        )
        if asset_url:
            enriched["asset_url"] = asset_url
        if page_request := _resolve_page_pdf_request(row):
            page_pdf_url = page_pdf_urls.get(page_request)
            if page_pdf_url:
                enriched["asset_url"] = page_pdf_url
        enriched_rows.append(enriched)
    return enriched_rows


async def build_retrieval_asset_url_map(
    rows: list[dict[str, Any]],
    *,
    log_context: str,
) -> dict[str, AssetUrlValue]:
    page_pdf_urls = await _build_page_pdf_url_lookup(rows, log_context=log_context)
    url_map: dict[str, AssetUrlValue] = {}
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "").strip()
        if not chunk_id:
            continue

        if page_request := _resolve_page_pdf_request(row):
            page_pdf_url = page_pdf_urls.get(page_request)
            if page_pdf_url:
                url_map[chunk_id] = page_pdf_url
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
