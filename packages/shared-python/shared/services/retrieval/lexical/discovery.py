"""Serve existing lexical discovery from the captured native published builds."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import partial
import time

from sqlalchemy import and_, false, func, or_, select, text, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.execution.postgresql_snapshot import PostgreSQLServingGenerationChanged
from shared.services.retrieval.hydration.build_snapshot import BuildSnapshot
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.row_utils import iter_connected_target_ids, normalize_chunk_type
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.manifest_filters import buildManifestFilters
from shared.services.retrieval.lexical.reader import PublishedLexicalRead
from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead
from shared.services.retrieval.scoring.knowhere_hybrid import tokenize_query_for_ranker
from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult, _hydrate_winning_units
from shared.services.retrieval.search.scoring import normalize_row_scores
from shared.services.retrieval.search.section_filters import is_excluded_section
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES


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
        scope: DocumentScope = documentScope.excluding(excludeDocumentIds)
        if reader.isClassic or reader.manifestId is not None:
            storage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
            if reader.scope.namespace != namespace or reader.scope.ownerId != storage.resolve_owner(userId):
                raise LexicalIndexError("Discovery request differs from its published read scope")
        tokens: tuple[str, ...] = tuple(tokenize_query_for_ranker(query))
        if isinstance(reader, AsyncPublishedLexicalRead) and reader.manifestId is not None:
            if revisionPins is not None:
                scope = scope.narrow(list(revisionPins))
            if sectionTargets:
                raise LexicalIndexError("Section-targeted discovery requires an inline published read")
            if db.info.get("lexical_read_owned") and db.in_transaction():
                await db.commit()
            hits: tuple[LexicalHit, ...] = await reader.searchManifestAsync(
                queryTokens=tokens, limit=topK, documentScope=scope, chunkTypes=chunkTypes,
                excludeSections=excludeSections,
            )
            # Current builds read live chunks; a build replaced after the search reads the
            # copy its publication captured before replacing those chunks.
            rows, _ = await _hydrateBuildHits(db, reader=reader, hits=hits, documentScope=scope,
                chunkTypes=chunkTypes, excludeSections=excludeSections, revisionPins=revisionPins,
                readCurrentBuilds=True)
            return _formatDiscovery(rows, startedAt=started, hasUnhydratedHits=False)
        if revisionPins is not None and any(reader.pins.get(identity) != revision for identity, revision in revisionPins.items()):
            raise LexicalIndexError("Discovery revision pins differ from its read session")
        selected: dict[str, str] = {identity: revision for identity, revision in reader.pins.items()
            if scope.allows(identity) and (revisionPins is None or identity in revisionPins)}
        buildIds: tuple[str, ...] = tuple(reader.builds[identity] for identity in selected)
        unitIds: tuple[str, ...] | None = None if selected else ()
        hasUnitFilters: bool = _hasUnitFilters(chunkTypes, signalPaths or [], excludeSections, sectionTargets)
        useNativeFilters: bool = (reader.isClassic and not sectionTargets and not signalPaths and hasUnitFilters
            and isinstance(reader, AsyncPublishedLexicalRead))
        if selected and hasUnitFilters and not useNativeFilters:
            operation = partial(_filterUnits, reader, selected, chunkTypes,
                signalPaths or [], filterMode, excludeSections, sectionTargets)
            unitIds = (await reader.database.run(operation) if isinstance(reader, AsyncPublishedLexicalRead)
                else await asyncio.to_thread(operation))
        if db.info.get("lexical_read_owned") and db.in_transaction():
            await db.commit()
        if isinstance(reader, AsyncPublishedLexicalRead):
            if useNativeFilters:
                # The inline build list already applies the request's document scope.
                filters, exclusions = buildManifestFilters(scope=DocumentScope(), chunkTypes=chunkTypes,
                    excludeSections=excludeSections)
                hits = await reader.searchAsync(buildIds=buildIds, queryTokens=tokens, limit=topK, unitIds=unitIds,
                    extraFilters=filters, exclusions=exclusions)
            else:
                hits = await reader.searchAsync(buildIds=buildIds, queryTokens=tokens, limit=topK, unitIds=unitIds)
        else:
            hits = await asyncio.to_thread(reader.search, buildIds=buildIds, queryTokens=tokens, limit=topK, unitIds=unitIds)
        if reader.isClassic:
            rows, hasUnhydratedHits = await _hydrateBuildHits(db, reader=reader, hits=hits, documentScope=scope,
                chunkTypes=chunkTypes, excludeSections=excludeSections, revisionPins=selected, allowCurrent=True)
            return _formatDiscovery(rows, startedAt=started, hasUnhydratedHits=hasUnhydratedHits)
        rowsById: dict[str, dict[str, object]] = {}
        scores: dict[str, float] = {}
        for hit in hits:
            identity: str = f"{hit.buildId}:{hit.unit.unitId}"
            rowsById[identity] = {"document_id": hit.unit.documentId,
                "job_result_id": hit.unit.revisionId, "section_id": hit.unit.sectionId}
            scores[identity] = hit.score
        rows, has_unhydrated_hits = await _hydrate_winning_units(
            db, user_id=userId, namespace=namespace, ranked_unit_ids=list(rowsById),
            rows_by_unit_id=rowsById, scores_by_unit=scores, chunk_types=chunkTypes,
            exclude_document_ids=excludeDocumentIds, exclude_sections=excludeSections,
            document_scope=scope, revision_pins=selected)
        if db.info.get("lexical_read_owned") and db.in_transaction():
            await db.commit()
        if isinstance(reader, AsyncPublishedLexicalRead):
            await reader.assertAliveAsync()
        else:
            await asyncio.to_thread(reader.assertAlive)
        return _formatDiscovery(rows, startedAt=started, hasUnhydratedHits=has_unhydrated_hits)
    except LexicalIndexError as error:
        unavailable = UnavailableException(internal_message=str(error), retry_after=1,
            user_message="Search is temporarily unavailable. Please retry.", original_exception=error)
        unavailable.details["reason"] = "LEXICAL_BACKEND_UNAVAILABLE"
        raise unavailable from error
    finally:
        if current is None:
            assert isinstance(reader, AsyncPublishedLexicalRead)
            await reader.closeAsync()


async def _hydrateBuildHits(
    db: AsyncSession, *, reader: PublishedLexicalRead, hits: tuple[LexicalHit, ...],
    documentScope: DocumentScope, chunkTypes: set[str] | None, excludeSections: list[dict[str, str]],
    revisionPins: Mapping[str, str] | None, allowCurrent: bool = False, readCurrentBuilds: bool = False,
) -> tuple[list[dict[str, object]], bool]:
    for hit in hits:
        if not documentScope.allows(hit.unit.documentId) or (
            revisionPins is not None and revisionPins.get(hit.unit.documentId) != hit.unit.revisionId
        ):
            raise LexicalIndexError("Native hit escaped its requested document or revision scope")
    sectionKeys: tuple[tuple[str, str], ...] = tuple(dict.fromkeys((hit.buildId, hit.unit.sectionId) for hit in hits))
    snapshots: list[dict[str, object]] = await BuildSnapshot.loadRows(
        db, scope=reader.scope, sectionKeys=sectionKeys, allowCurrent=allowCurrent,
        readCurrentBuilds=readCurrentBuilds,
    )
    # Match PG discovery's last chunk in sort order when a section has multiple rows.
    rowsBySection: dict[tuple[object, object], dict[str, object]] = {
        (row["build_id"], row["section_id"]): row for row in snapshots
    }
    primaries: list[dict[str, object]] = []
    for hit in hits:
        row: dict[str, object] | None = rowsBySection.get((hit.buildId, hit.unit.sectionId))
        if row is None:
            if allowCurrent:
                continue
            raise LexicalIndexError("Published native hit has no immutable hydration row", isRetryable=True)
        if row["document_id"] != hit.unit.documentId or row["job_result_id"] != hit.unit.revisionId:
            raise LexicalIndexError("Native hit does not match its captured build document and revision")
        primaries.append({**row, "score": hit.score})
    hasUnhydratedHits: bool = len(primaries) != len(hits)
    if chunkTypes is None or not chunkTypes.issubset(ASSET_CHUNK_TYPES):
        return primaries, hasUnhydratedHits

    connected = await hydrate_connected_target_rows(
        db=db, rows=primaries, exclude_document_ids=[], exclude_sections=excludeSections,
        document_scope=documentScope, lexicalScope=reader.scope,
    )
    rowsByChunk: dict[tuple[object, object], dict[str, object]] = {
        (row["build_id"], row["chunk_id"]): row for row in [*primaries, *connected]
    }
    assets: dict[tuple[object, object], dict[str, object]] = {}
    for row in primaries:
        for chunkId in (row["chunk_id"], *iter_connected_target_ids(row)):
            key: tuple[object, object] = (row["build_id"], chunkId)
            asset: dict[str, object] | None = rowsByChunk.get(key)
            if asset is not None and normalize_chunk_type(asset["chunk_type"]) in chunkTypes:
                assets.setdefault(key, {**asset, "score": row["score"]})
    return list(assets.values()), hasUnhydratedHits


def _formatDiscovery(
    rows: list[dict[str, object]], *, startedAt: float, hasUnhydratedHits: bool,
) -> DiscoveryResult:
    if rows:
        normalize_row_scores(rows, source_field="score", target_field="discovery_score", default=0.5)
    return DiscoveryResult(status="discovery_done", payload={"fused_rows": rows},
        latency_ms=int((time.monotonic() - startedAt) * 1000), has_unhydrated_hits=hasUnhydratedHits)


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
        if reader.isClassic:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            pinned = [(identity, revision, reader.builds[identity]) for identity, revision in revisions.items()]
            count = session.scalar(select(func.count()).select_from(documents).where(
                documents.user_id == reader.scope.ownerId,
                documents.namespace == reader.scope.namespace,
                documents.status == "active",
                tuple_(documents.document_id, documents.current_job_result_id,
                    documents.current_indexing_build_id).in_(pinned),
            ))
            if count != len(pinned):
                raise PostgreSQLServingGenerationChanged
        return tuple(row.unit_id for row in session.execute(statement)
            if not is_excluded_section(document_id=row.document_id,
                section_path=row.section_path, exclude_sections=excludeSections))
