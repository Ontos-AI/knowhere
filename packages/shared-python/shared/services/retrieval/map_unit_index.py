"""Publication-time materialization of persisted map-unit lexical units."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Table, delete, insert, select
from sqlalchemy.orm import Session

from shared.models.database.document import (
    DocumentChunk,
    DocumentMapUnit,
    DocumentMapUnitIndex,
    DocumentMapUnitToken,
    DocumentSection,
)
from shared.services.retrieval.scoring.hierarchy import ProviderToolSpace
from shared.services.retrieval.scoring.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
)
from shared.services.retrieval.scoring.knowhere_provider import (
    KnowhereProvider,
    SectionRow,
    UnitRow,
)
from shared.services.retrieval.scoring.persisted_score_load import (
    average_idf_from_unit_dfs,
)
from shared.services.retrieval.scoring.score_units import build_score_units
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_strategy import resolve_publication_strategy
from shared.services.retrieval.publication_token_copy import (
    TokenCopyRow,
    insert_token_records_with_copy,
)
from shared.services.retrieval.publication_trace_stage import trace_publication_stage

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace

__all__ = ["MAP_UNIT_INDEX_FORMAT_VERSION", "replace_document_map_units"]

_BULK_INSERT_BATCH_SIZE = 5_000
_TOKEN_ID_PREFIX_LENGTH = 12
_TOKEN_ID_SEQUENCE_WIDTH = 19


def replace_document_map_units(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    trace: PublicationTrace | None = None,
    clear_existing: bool = True,
) -> None:
    """Build the derived index through the authoritative map-unit constructor."""
    if clear_existing:
        with trace_publication_stage(trace, "tokens_persist"):
            db.execute(
                delete(DocumentMapUnitToken).where(
                    DocumentMapUnitToken.map_unit_id.in_(
                        select(DocumentMapUnit.id)
                        .where(DocumentMapUnit.document_id == scope.document_id)
                        .where(DocumentMapUnit.job_result_id == scope.job_result_id)
                    )
                )
            )
        with trace_publication_stage(trace, "map_units_persist"):
            db.execute(
                delete(DocumentMapUnit)
                .where(DocumentMapUnit.document_id == scope.document_id)
                .where(DocumentMapUnit.job_result_id == scope.job_result_id)
            )
            db.execute(
                delete(DocumentMapUnitIndex)
                .where(DocumentMapUnitIndex.document_id == scope.document_id)
                .where(DocumentMapUnitIndex.job_result_id == scope.job_result_id)
            )
    with trace_publication_stage(trace, "serving_index_prepare"):
        section_models = list(
            db.scalars(
                select(DocumentSection)
                .where(DocumentSection.document_id == scope.document_id)
                .where(DocumentSection.job_result_id == scope.job_result_id)
                .order_by(DocumentSection.sort_order, DocumentSection.section_id)
            )
        )
        chunk_models = list(
            db.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == scope.document_id)
                .where(DocumentChunk.job_result_id == scope.job_result_id)
                .order_by(
                    DocumentChunk.sort_order,
                    DocumentChunk.chunk_id,
                    DocumentChunk.id,
                )
            )
        )
    with trace_publication_stage(trace, "serving_index_prepare"):
        provider = KnowhereProvider(
            doc_id=scope.document_id,
            sections=[_to_section_row(section) for section in section_models],
            units=[_to_unit_row(chunk) for chunk in chunk_models],
        )
        score_units = build_score_units(
            ProviderToolSpace(provider),
            scope.document_id,
        )
    persisted_count = 0
    token_count = 0
    map_unit_rows: list[dict[str, object]] = []
    token_rows: list[dict[str, object]] = []
    use_candidate_strategy = resolve_publication_strategy() == "candidate"
    token_hash_cache: dict[str, str] = {}
    candidate_token_rows: list[TokenCopyRow] = []
    path_unit_df: Counter[str] = Counter()
    content_unit_df: Counter[str] = Counter()
    path_document_count: int = 0
    path_total_length: int = 0
    content_document_count: int = 0
    content_total_length: int = 0
    chunk_types_by_section: dict[str, frozenset[str]] = {}
    with trace_publication_stage(trace, "serving_index_prepare"):
        for sort_order, unit in enumerate(score_units):
            unit_id = str(unit.get("chunk_id") or "").strip()
            section_id = str(unit.get("section_id") or "").strip()
            if not unit_id or not section_id:
                continue
            map_unit_id = f"dmu_{uuid4().hex}"
            path_tokens = str(unit.get("path_search_text") or "").split()
            content_tokens = str(unit.get("content_search_text") or "").split()
            if path_tokens:
                path_document_count += 1
                path_total_length += len(path_tokens)
            if content_tokens:
                content_document_count += 1
                content_total_length += len(content_tokens)
            path_frequencies = Counter(path_tokens)
            content_frequencies = Counter(content_tokens)
            path_unit_df.update(path_frequencies.keys())
            content_unit_df.update(content_frequencies.keys())
            # ``provider.self_units`` already reflects root-asset remount (assets
            # referenced via ``connect_to`` are moved onto the text section that
            # embeds them), so this is the same ownership the query-time scorer
            # sees, not a new computation.
            section_types = chunk_types_by_section.get(section_id)
            if section_types is None:
                section_types = frozenset(
                    u.chunk_type for u in provider.self_units(section_id)
                )
                chunk_types_by_section[section_id] = section_types
            map_unit_rows.append(
                {
                    "id": map_unit_id,
                    "document_id": scope.document_id,
                    "job_result_id": scope.job_result_id,
                    "unit_id": unit_id,
                    "section_id": section_id,
                    "unit_kind": str(unit.get("kind") or "leaf"),
                    "path_token_count": len(path_tokens),
                    "content_token_count": len(content_tokens),
                    "term_search_text_lower": str(
                        unit.get("term_search_text") or ""
                    ).lower(),
                    "has_image": "image" in section_types,
                    "has_table": "table" in section_types,
                    "sort_order": sort_order,
                }
            )
            for channel, frequencies in (
                ("path", path_frequencies),
                ("content", content_frequencies),
            ):
                for token, frequency in frequencies.items():
                    token_hash = token_hash_cache.get(token)
                    if token_hash is None:
                        token_hash = sha256(token.encode("utf-8")).hexdigest()
                        token_hash_cache[token] = token_hash
                    if use_candidate_strategy:
                        # Keep candidate rows as compact mutable records. Their
                        # IDs are assigned after locality sorting, so creating a
                        # dictionary here would only add memory and GC pressure.
                        candidate_token_rows.append(
                            [
                                "dmut_candidate",
                                map_unit_id,
                                channel,
                                token,
                                token_hash,
                                frequency,
                            ]
                        )
                    else:
                        token_rows.append(
                            {
                                "id": f"dmut_{uuid4().hex[:31]}",
                                "map_unit_id": map_unit_id,
                                "channel": channel,
                                "token": token,
                                "token_hash": token_hash,
                                "frequency": frequency,
                            }
                        )
                token_count += len(frequencies)
            persisted_count += 1
    if trace is not None:
        trace.record_count("map_units", persisted_count)
        trace.record_count("tokens", token_count)
    with trace_publication_stage(trace, "map_units_persist"):
        _execute_bulk_insert(db, DocumentMapUnit, map_unit_rows)
    with trace_publication_stage(trace, "tokens_persist"):
        if use_candidate_strategy:
            _prepare_candidate_token_rows(
                candidate_token_rows,
                job_result_id=scope.job_result_id,
            )
            insert_token_records_with_copy(db, candidate_token_rows)
        else:
            _execute_bulk_insert(db, DocumentMapUnitToken, token_rows)
    with trace_publication_stage(trace, "statistics_prepare"):
        index = DocumentMapUnitIndex(
            id=f"dmui_{uuid4().hex}",
            document_id=scope.document_id,
            job_result_id=scope.job_result_id,
            format_version=MAP_UNIT_INDEX_FORMAT_VERSION,
            unit_count=persisted_count,
            token_count=token_count,
            average_idf_path=average_idf_from_unit_dfs(
                unit_count=persisted_count,
                token_document_frequency=path_unit_df,
            ),
            average_idf_content=average_idf_from_unit_dfs(
                unit_count=persisted_count,
                token_document_frequency=content_unit_df,
            ),
            path_document_count=path_document_count,
            path_total_length=path_total_length,
            content_document_count=content_document_count,
            content_total_length=content_total_length,
        )
    with trace_publication_stage(trace, "statistics_persist"):
        db.add(index)
        db.flush()


def _execute_bulk_insert(
    db: Session,
    model: type[DocumentMapUnit] | type[DocumentMapUnitToken],
    rows: list[dict[str, object]],
) -> None:
    """Insert derived index rows in bounded Core batches."""
    if not rows:
        return
    table: Table = model.__table__
    for start in range(0, len(rows), _BULK_INSERT_BATCH_SIZE):
        db.execute(insert(table), rows[start : start + _BULK_INSERT_BATCH_SIZE])


def _prepare_candidate_token_rows(
    rows: list[TokenCopyRow],
    *,
    job_result_id: str,
) -> None:
    """Order COPY rows for locality in the token lookup and primary indexes.

    Token rows used to receive unrelated random UUIDs and were emitted in
    score-unit order. That makes one COPY update several large B-tree indexes
    at random locations. The candidate path can give one publication a stable
    key range and emit rows in lookup-key order, preserving semantic state
    while reducing random index page writes.
    """
    rows.sort(
        key=lambda row: (
            str(row[2]),
            str(row[4]),
            str(row[1]),
            str(row[3]),
        )
    )
    prefix = sha256(job_result_id.encode("utf-8")).hexdigest()[
        :_TOKEN_ID_PREFIX_LENGTH
    ]
    for sequence, row in enumerate(rows):
        row[0] = f"dmut_{prefix}{sequence:0{_TOKEN_ID_SEQUENCE_WIDTH}d}"


def _to_section_row(section: DocumentSection) -> SectionRow:
    return SectionRow(
        section_id=section.section_id,
        parent_section_id=section.parent_section_id,
        section_path=section.section_path,
        section_title=str(section.section_title or ""),
        section_level=section.section_level,
        summary=str(section.summary or ""),
        sort_order=section.sort_order,
    )


def _to_unit_row(chunk: DocumentChunk) -> UnitRow:
    raw_metadata = chunk.chunk_metadata
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    return UnitRow(
        chunk_id=chunk.chunk_id,
        section_id=chunk.section_id,
        chunk_type=chunk.chunk_type,
        content=str(chunk.content or ""),
        sort_order=chunk.sort_order,
        source_chunk_path=str(chunk.source_chunk_path or ""),
        file_path=str(chunk.file_path or ""),
        metadata=metadata,
    )
