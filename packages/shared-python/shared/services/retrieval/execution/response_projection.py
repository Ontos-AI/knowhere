from __future__ import annotations

from typing import Any

from shared.services.retrieval.hydration.assets import enrich_rows_with_retrieval_asset_urls
from shared.services.retrieval.hydration.row_utils import (
    PUBLIC_RESULT_FIELDS,
    PUBLIC_SOURCE_FIELDS,
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
    return await enrich_rows_with_retrieval_asset_urls(
        refs,
        log_context='agentic referenced chunk',
    )


async def project_public_retrieval_response(response: dict[str, Any]) -> dict[str, Any]:
    public_response = {
        'namespace': response.get('namespace'),
        'query': response.get('query'),
        'router_used': response.get('router_used'),
        'evidence_text': response.get('evidence_text') or '',
        'answer_text': '',
        'referenced_chunks': response.get('referenced_chunks') or [],
        'results': [],
    }

    if response.get('stop_reason') is not None:
        public_response['stop_reason'] = response['stop_reason']
    if response.get('failure_reason') is not None:
        public_response['failure_reason'] = response['failure_reason']
    if response.get('decision_trace') is not None:
        public_response['decision_trace'] = response['decision_trace']

    projected_rows = await enrich_rows_with_retrieval_asset_urls(
        response.get('results', []),
        log_context='retrieval result',
    )
    public_results: list[dict[str, Any]] = []
    for row in projected_rows:
        public_row: dict[str, Any] = {}
        for field in PUBLIC_RESULT_FIELDS:
            if field in row:
                public_row[field] = row[field]
        if 'source' in row:
            public_row['source'] = row['source']
        else:
            public_row['source'] = to_public_source(row)
        public_results.append(public_row)

    public_response['results'] = public_results
    return public_response
