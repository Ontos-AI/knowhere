from __future__ import annotations

from typing import Any

from loguru import logger

from shared.services.retrieval.assets import generate_retrieval_asset_url, is_client_result_artifact_ref
from shared.services.retrieval.hydration import (
    MEDIA_CHUNK_TYPES,
    PUBLIC_RESULT_FIELDS,
    PUBLIC_SOURCE_FIELDS,
    is_media_chunk,
    normalize_chunk_type,
)


def attach_citation(row: dict[str, Any]) -> dict[str, Any]:
    citation = {
        'document_id': row.get('document_id'),
        'chunk_id': row.get('chunk_id'),
        'source_file_name': row.get('source_file_name'),
        'section_path': row.get('section_path'),
    }
    return {**row, 'citation': citation}


def to_public_source(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in PUBLIC_SOURCE_FIELDS}


async def enrich_referenced_chunks_with_asset_urls(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched_refs: list[dict[str, Any]] = []
    for ref in refs:
        enriched = dict(ref)
        chunk_type = normalize_chunk_type(ref.get('chunk_type'))
        artifact_ref = ref.get('file_path', '')
        job_id = ref.get('job_id', '')
        if chunk_type in MEDIA_CHUNK_TYPES and job_id and is_client_result_artifact_ref(artifact_ref):
            try:
                asset_url = await generate_retrieval_asset_url(
                    job_id=str(job_id),
                    artifact_ref=str(artifact_ref),
                )
                if asset_url:
                    enriched['asset_url'] = asset_url
            except Exception as exc:
                logger.warning(f'Failed to generate agentic asset URL (ignored): {exc}')
        enriched_refs.append(enriched)
    return enriched_refs


async def project_public_retrieval_response(response: dict[str, Any]) -> dict[str, Any]:
    public_response = {
        'namespace': response.get('namespace'),
        'query': response.get('query'),
        'router_used': response.get('router_used'),
        'results': [],
    }

    if response.get('answer_text') is not None:
        public_response['answer_text'] = response['answer_text']
    if response.get('referenced_chunks') is not None:
        public_response['referenced_chunks'] = response['referenced_chunks']

    public_results: list[dict[str, Any]] = []
    for row in response.get('results', []):
        artifact_ref = row.get('file_path')
        asset_url = None
        if is_media_chunk(row) and is_client_result_artifact_ref(artifact_ref) and row.get('job_id'):
            try:
                asset_url = await generate_retrieval_asset_url(
                    job_id=str(row['job_id']),
                    artifact_ref=str(artifact_ref),
                )
            except Exception as exc:
                logger.warning(f'Failed to generate retrieval asset URL (ignored): {exc}')

        public_row: dict[str, Any] = {}
        for field in PUBLIC_RESULT_FIELDS:
            if field == 'asset_url':
                if asset_url:
                    public_row['asset_url'] = asset_url
            elif field in row:
                public_row[field] = row[field]
        if 'source' in row:
            public_row['source'] = row['source']
        else:
            public_row['source'] = to_public_source(row)
        public_results.append(public_row)

    public_response['results'] = public_results
    return public_response
