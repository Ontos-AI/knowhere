from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.hydration.assets import sign_page_media_rows
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.evidence_compose import compose_evidence_parts
from shared.services.retrieval.hydration.page_media import page_media_rows
from shared.services.retrieval.hydration.row_utils import (
    coerce_page_nums,
    extract_page_nums,
    extract_time_ranges,
    filter_excluded_rows,
    is_unrefined_page,
    iter_connected_target_ids,
    normalize_chunk_type,
    page_summary,
    with_lit_pages,
)


@dataclass(frozen=True)
class RetrievalResultRows:
    primary: list[dict[str, Any]]
    connected: list[dict[str, Any]]


async def load_retrieval_result_rows(
    *,
    db: AsyncSession | None,
    rows: list[dict[str, Any]],
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    document_scope: DocumentScope,
    revision_pins: Mapping[str, str] | None,
) -> RetrievalResultRows:
    primary = filter_excluded_rows(
        rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        document_scope=document_scope,
    )
    connected = await hydrate_connected_target_rows(
        db=db,
        rows=primary,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        document_scope=document_scope,
        revision_pins=revision_pins,
    )
    return RetrievalResultRows(
        primary=primary, connected=await sign_page_media_rows(connected)
    )


async def assemble_retrieval_results(
    *,
    db: AsyncSession | None = None,
    rows: list[dict[str, Any]],
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    document_scope: DocumentScope = DocumentScope(),
    allowed_chunk_types: set[str] | None = None,
    revision_pins: Mapping[str, str] | None = None,
    queried_tables: Mapping[tuple[str, str], str] | None = None,
    skip_locked_chunk_final: bool = False,
    loaded_rows: RetrievalResultRows | None = None,
) -> list[dict[str, Any]]:
    if loaded_rows is None:
        loaded_rows = await load_retrieval_result_rows(
            db=db,
            rows=rows,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            document_scope=document_scope,
            revision_pins=revision_pins,
        )
    rows_by_build: dict[str | None, dict[str, dict[str, Any]]] = {}
    for row in [*loaded_rows.primary, *loaded_rows.connected]:
        if row.get('chunk_id'):
            rows_by_build.setdefault(row.get('build_id'), {})[str(row['chunk_id'])] = row
    filtered_rows = [
        with_lit_pages(row)
        for row in _filter_rows_by_allowed_chunk_types(
            loaded_rows.primary,
            allowed_chunk_types=allowed_chunk_types,
            rows_by_build=rows_by_build,
        )
    ]

    embedded_targets: set[tuple[str | None, str]] = set()
    for row in filtered_rows:
        build_id: str | None = row.get('build_id')
        for target_id in iter_connected_target_ids(row):
            if target_id in rows_by_build.get(build_id, {}):
                embedded_targets.add((build_id, target_id))

    selected = _select_unreplaced_hits([
        row for row in filtered_rows if (row.get("build_id"), row.get("chunk_id")) not in embedded_targets
    ])
    prepared: list[tuple[dict[str, Any], dict[str, Any], str, str | None]] = []
    for row in selected:
        build_id: str | None = row.get('build_id')
        assembled_row = dict(row)
        base_content = str(row.get('content') or '')
        chunk_type = normalize_chunk_type(row.get('chunk_type'))
        if chunk_type == 'page':
            if is_unrefined_page(row):
                assembled_row['content'] = base_content
                assembled_row['content_source'] = (
                    'raw' if base_content.strip() else 'page_image'
                )
            elif base_content.strip():
                assembled_row['content'] = base_content
                assembled_row['content_source'] = 'content'
            else:
                assembled_row['content'] = ''
                assembled_row['content_source'] = 'summary'
            page_nums = extract_page_nums(assembled_row)
            if page_nums is not None:
                assembled_row['page_nums'] = page_nums
        else:
            assembled_row['content'] = base_content
            assembled_row['content_source'] = 'content'
        time_ranges = extract_time_ranges(assembled_row)
        if time_ranges is not None:
            assembled_row['time_ranges'] = time_ranges
        prepared.append((assembled_row, row, chunk_type, build_id))

    # Composing evidence downloads table HTML and image bytes from object
    # storage with blocking calls; worker threads keep them off the event loop
    # so concurrent requests are not stalled, and let rows download together.
    composed_parts = await asyncio.gather(
        *(
            asyncio.to_thread(
                compose_evidence_parts,
                assembled_row,
                rows_by_build.get(build_id, {}),
                queried_tables=queried_tables,
            )
            for assembled_row, _row, _chunk_type, build_id in prepared
        )
    )

    assembled: list[dict[str, Any]] = []
    emitted_media: set[tuple[str | None, str]] = set()
    for (assembled_row, row, chunk_type, build_id), composed in zip(
        prepared, composed_parts, strict=True
    ):
        assembled_row['composed'] = composed
        if (
            chunk_type == 'page'
            and assembled_row.get('content_source') == 'summary'
            and not str(assembled_row.get('content') or '').strip()
        ):
            assembled_row['content'] = page_summary(row)
        assembled.append(assembled_row)
        if chunk_type == 'page':
            # The page's evidence already shows its media; results list them
            # after the page, scored as the page, with no evidence of their own.
            for media_row in page_media_rows(row, rows_by_build.get(build_id, {})):
                media_key = (build_id, str(media_row.get('chunk_id') or ''))
                if media_key in emitted_media:
                    continue
                emitted_media.add(media_key)
                assembled.append(_media_result_row(media_row, score=row.get('score')))
    if db is not None and assembled:
        from shared.services.jobs.lifecycle.scope_enrichments import (
            promote_scopes_on_read,
        )

        await promote_scopes_on_read(
            db, assembled, skip_locked_chunk_final=skip_locked_chunk_final
        )
    return assembled


def _select_unreplaced_hits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Omit a baseline only when finalized replacement evidence is being returned."""
    replacements: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("enrichment_publication_state") != "published" or not str(row.get("content") or "").strip():
            continue
        key = (str(row.get("document_id") or ""), str(row.get("job_result_id") or ""),
            str(row.get("enrichment_scope_id") or ""))
        replacements.setdefault(key, []).append(row)

    return [row for row in rows if not _is_baseline_replaced(row, replacements)]


def _is_baseline_replaced(
    row: dict[str, Any], replacements: dict[tuple[str, str, str], list[dict[str, Any]]],
) -> bool:
    if not row.get("uses_additive_visibility") or row.get("enrichment_attempt_id"):
        return False
    metadata = row.get("chunk_metadata") or {}
    chunk_type = normalize_chunk_type(row.get("chunk_type"))
    if chunk_type == "page" and not is_unrefined_page(row):
        return False
    scope = str(metadata.get("scope_id") or "*")
    key = (str(row.get("document_id") or ""), str(row.get("job_result_id") or ""), scope)
    candidates = replacements.get(key, ())
    if not candidates:
        return False
    if chunk_type == "page":
        pages = set(coerce_page_nums(metadata.get("page_nums")))
        covered: set[int] = set()
        for candidate in candidates:
            if normalize_chunk_type(candidate.get("chunk_type")) != "page":
                continue
            candidate_metadata = candidate.get("chunk_metadata") or {}
            coverage = candidate_metadata.get("searchable_page_nums")
            if coverage is not None:
                covered.update(coerce_page_nums(coverage))
            else:
                candidate_pages = coerce_page_nums(candidate_metadata.get("page_nums"))
                if len(candidate_pages) == 1 and not str(candidate["content"]).lstrip().startswith("[SAME-AS"):
                    covered.update(candidate_pages)
        return bool(pages) and pages <= covered

    source = " ".join(str(row.get("content") or "").split())
    return bool(source) and any(
        normalize_chunk_type(candidate.get("chunk_type")) == chunk_type
        and source in " ".join(str(candidate["content"]).split())
        for candidate in candidates
    )


def _media_result_row(media_row: dict[str, Any], *, score: object) -> dict[str, Any]:
    result_row = dict(media_row)
    result_row['score'] = score
    result_row['content_source'] = 'content'
    result_row['composed'] = []
    time_ranges = extract_time_ranges(result_row)
    if time_ranges is not None:
        result_row['time_ranges'] = time_ranges
    return result_row


def _filter_rows_by_allowed_chunk_types(
    rows: list[dict[str, Any]],
    *,
    allowed_chunk_types: set[str] | None,
    rows_by_build: Mapping[str | None, Mapping[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    if allowed_chunk_types is None:
        return rows

    return [
        row
        for row in rows
        if normalize_chunk_type(row.get('chunk_type')) in allowed_chunk_types
        or any(
            normalize_chunk_type(rows_by_build.get(row.get('build_id'), {}).get(target_id, {}).get('chunk_type'))
            in allowed_chunk_types
            for target_id in iter_connected_target_ids(row)
        )
    ]


def _image_display_content(row: dict[str, Any]) -> str:
    display_ref = (
        str(row.get('asset_url') or '').strip()
        or str(row.get('file_path') or '').strip()
    )
    description = str(row.get('content') or '').strip()
    lines: list[str] = []
    if display_ref:
        lines.append(f'[Image: {display_ref}]')
    elif description:
        lines.append('[Image description]')
    if description:
        lines.extend(line for line in description.split('\n') if line.strip())
    return '\n'.join(lines)
