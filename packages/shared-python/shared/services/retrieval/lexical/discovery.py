"""Serve existing lexical discovery from the captured native published builds."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import partial
import time

from sqlalchemy import and_, false, func, or_, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.reader import PublishedLexicalRead
from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead
from shared.services.retrieval.scoring.knowhere_hybrid import tokenize_query_for_ranker
from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult, _hydrate_winning_units
from shared.services.retrieval.search.scoring import normalize_row_scores
from shared.services.retrieval.search.section_filters import is_excluded_section


async def discoverOpenSearch(
    db: AsyncSession, *, userId: str, namespace: str, query: str, topK: int,
    documentScope: DocumentScope, excludeDocumentIds: list[str],
    excludeSections: list[dict[str, str]], chunkTypes: set[str] | None,
    signalPaths: list[str] | None, filterMode: str,
    revisionPins: Mapping[str, str] | None,
    sectionTargets: list[tuple[str, str | None]] | None,
) -> DiscoveryResult:
    started: float = time.monotonic()
    current: PublishedLexicalRead | None = PublishedLexicalRead.current()
    reader: PublishedLexicalRead
    if current is not None:
        reader = current
    elif db.bind is not None:
        reader = AsyncPublishedLexicalRead(userId=userId, namespace=namespace, bind=db.bind)
    else:
        raise LexicalIndexError("API discovery requires an asynchronous database bind")
    try:
        if current is None:
            assert isinstance(reader, AsyncPublishedLexicalRead)
            await reader.startAsync()
        if revisionPins is not None and any(reader.pins.get(identity) != revision for identity, revision in revisionPins.items()):
            raise LexicalIndexError("Discovery revision pins differ from its read session")
        scope: DocumentScope = documentScope.excluding(excludeDocumentIds)
        selected: dict[str, str] = {identity: revision for identity, revision in reader.pins.items()
            if scope.allows(identity) and (revisionPins is None or identity in revisionPins)}
        buildIds: tuple[str, ...] = tuple(reader.builds[identity] for identity in selected)
        unitIds: tuple[str, ...] | None = None if selected else ()
        if selected and _hasUnitFilters(chunkTypes, signalPaths or [], excludeSections, sectionTargets):
            operation = partial(_filterUnits, reader, selected, chunkTypes,
                signalPaths or [], filterMode, excludeSections, sectionTargets)
            unitIds = (await reader.database.run(operation) if isinstance(reader, AsyncPublishedLexicalRead)
                else await asyncio.to_thread(operation))
        if db.info.get("lexical_read_owned") and db.in_transaction():
            await db.commit()
        tokens: tuple[str, ...] = tuple(tokenize_query_for_ranker(query))
        hits = (await reader.searchAsync(buildIds=buildIds, queryTokens=tokens, limit=topK, unitIds=unitIds)
            if isinstance(reader, AsyncPublishedLexicalRead)
            else await asyncio.to_thread(reader.search, buildIds=buildIds, queryTokens=tokens, limit=topK, unitIds=unitIds))
        rowsById: dict[str, dict[str, object]] = {}
        scores: dict[str, float] = {}
        for hit in hits:
            identity: str = f"{hit.buildId}:{hit.unit.unitId}"
            rowsById[identity] = {"document_id": hit.unit.documentId,
                "job_result_id": hit.unit.revisionId, "section_id": hit.unit.sectionId}
            scores[identity] = hit.score
        rows = await _hydrate_winning_units(db, ranked_unit_ids=list(rowsById),
            rows_by_unit_id=rowsById, scores_by_unit=scores, chunk_types=chunkTypes,
            exclude_document_ids=excludeDocumentIds, exclude_sections=excludeSections,
            document_scope=scope, revision_pins=selected)
        if not reader.isClassic and db.info.get("lexical_read_owned") and db.in_transaction():
            await db.commit()
        if not reader.isClassic:
            if isinstance(reader, AsyncPublishedLexicalRead):
                await reader.assertAliveAsync()
            else:
                await asyncio.to_thread(reader.assertAlive)
        if rows:
            normalize_row_scores(rows, source_field="score", target_field="discovery_score", default=0.5)
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": rows},
            latency_ms=int((time.monotonic() - started) * 1000))
    except LexicalIndexError as error:
        unavailable = UnavailableException(internal_message=str(error), retry_after=1,
            user_message="Search is temporarily unavailable. Please retry.", original_exception=error)
        unavailable.details["reason"] = "LEXICAL_BACKEND_UNAVAILABLE"
        raise unavailable from error
    finally:
        if current is None:
            assert isinstance(reader, AsyncPublishedLexicalRead)
            await reader.closeAsync()


def _hasUnitFilters(chunkTypes: set[str] | None, signalPaths: list[str],
                    excludeSections: list[dict[str, str]],
                    sectionTargets: list[tuple[str, str | None]] | None) -> bool:
    hasAssetFilter: bool = chunkTypes is not None and chunkTypes.issubset({"image", "table"})
    return bool(hasAssetFilter or signalPaths or excludeSections or sectionTargets)


def _filterUnits(
    reader: PublishedLexicalRead, revisions: Mapping[str, str],
    chunkTypes: set[str] | None, signalPaths: list[str], filterMode: str,
    excludeSections: list[dict[str, str]], sectionTargets: list[tuple[str, str | None]] | None,
) -> tuple[str, ...] | None:
    if not revisions:
        return ()
    hasAssetFilter: bool = chunkTypes is not None and chunkTypes.issubset({"image", "table"})
    if not _hasUnitFilters(chunkTypes, signalPaths, excludeSections, sectionTargets):
        return None
    storage: CorpusStorage = CorpusStorage.resolve_namespace(reader.scope.namespace)
    units = storage.DocumentMapUnit
    sections = storage.DocumentSection
    documents = storage.Document
    statement = select(units.unit_id, units.document_id, sections.section_path).join(
        sections, sections.section_id == units.section_id).join(documents,
        documents.document_id == units.document_id).where(
            documents.user_id == reader.scope.ownerId,
            tuple_(units.document_id, units.job_result_id).in_(list(revisions.items())))
    if hasAssetFilter:
        conditions = []
        if chunkTypes and "image" in chunkTypes:
            conditions.append(units.has_image)
        if chunkTypes and "table" in chunkTypes:
            conditions.append(units.has_table)
        statement = statement.where(or_(*conditions) if conditions else false())
    if signalPaths:
        signal = or_(*(func.lower(func.coalesce(sections.section_path, "")).like(f"%{path.lower()}%") for path in signalPaths))
        statement = statement.where(signal if filterMode == "keep" else ~signal)
    if sectionTargets and any(path for _, path in sectionTargets):
        statement = statement.where(or_(*(and_(units.document_id == identity,
            or_(sections.section_path == path, sections.section_path.like(f"{path} / %")) if path else true())
            for identity, path in sectionTargets)))
    with reader.sessions.begin() as session:
        return tuple(row.unit_id for row in session.execute(statement)
            if not is_excluded_section(document_id=row.document_id,
                section_path=row.section_path, exclude_sections=excludeSections))
