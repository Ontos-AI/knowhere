"""Publication-time materialization of exact map-nav lexical units."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models.database.document import (
    DocumentChunk,
    DocumentMapUnit,
    DocumentMapUnitIndex,
    DocumentMapUnitToken,
    DocumentSection,
)
from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    SectionRow,
    UnitRow,
)
from shared.services.retrieval.nav.nav_map_scores import build_score_units
from shared.services.retrieval.publication_models import DocumentPublicationScope


MAP_UNIT_INDEX_FORMAT_VERSION = 1


def replace_document_map_units(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> None:
    """Build the derived index through the authoritative map-unit constructor."""
    db.execute(
        delete(DocumentMapUnitToken).where(
            DocumentMapUnitToken.map_unit_id.in_(
                select(DocumentMapUnit.id)
                .where(DocumentMapUnit.document_id == scope.document_id)
                .where(DocumentMapUnit.job_result_id == scope.job_result_id)
            )
        )
    )
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
    for sort_order, unit in enumerate(score_units):
        unit_id = str(unit.get("chunk_id") or "").strip()
        section_id = str(unit.get("section_id") or "").strip()
        if not unit_id or not section_id:
            continue
        map_unit_id = f"dmu_{uuid4().hex}"
        path_tokens = str(unit.get("path_search_text") or "").split()
        content_tokens = str(unit.get("content_search_text") or "").split()
        db.add(
            DocumentMapUnit(
                id=map_unit_id,
                document_id=scope.document_id,
                job_result_id=scope.job_result_id,
                unit_id=unit_id,
                section_id=section_id,
                unit_kind=str(unit.get("kind") or "leaf"),
                path_token_count=len(path_tokens),
                content_token_count=len(content_tokens),
                term_search_text_lower=str(unit.get("term_search_text") or "").lower(),
                sort_order=sort_order,
            )
        )
        for channel, frequencies in (
            ("path", Counter(path_tokens)),
            ("content", Counter(content_tokens)),
        ):
            for token, frequency in frequencies.items():
                db.add(
                    DocumentMapUnitToken(
                        id=f"dmut_{uuid4().hex[:31]}",
                        map_unit_id=map_unit_id,
                        channel=channel,
                        token=token,
                        token_hash=sha256(token.encode("utf-8")).hexdigest(),
                        frequency=frequency,
                    )
                )
            token_count += len(frequencies)
        persisted_count += 1
    db.add(
        DocumentMapUnitIndex(
            id=f"dmui_{uuid4().hex}",
            document_id=scope.document_id,
            job_result_id=scope.job_result_id,
            format_version=MAP_UNIT_INDEX_FORMAT_VERSION,
            unit_count=persisted_count,
            token_count=token_count,
        )
    )


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
