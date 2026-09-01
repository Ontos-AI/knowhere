"""Async namespace snapshot loader for map-nav.

Loads current-revision sections + chunks for ``(user_id, namespace)``, builds a
synchronous ``NamespaceKnowhereProvider``, and a separate ``chunk_ref_index``
that keeps **DB-original** ``section_path`` / ``job_id`` (never remounted).
"""

from __future__ import annotations

import json
import logging
import os
import time
import zlib
from dataclasses import dataclass
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol

from sqlalchemy import (
    ARRAY,
    Executable,
    String,
    bindparam,
    cast,
    func,
    literal,
    select,
    tuple_,
)
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError

from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentSection,
    RetrievalNamespaceMapSnapshot,
    RetrievalNamespaceGeneration,
    RetrievalServingRevisionManifest,
)
from shared.models.database.job_result import JobResult
from shared.services.retrieval.nav.nav_knowhere import (
    LazyKnowhereProvider,
    KnowhereProvider,
    NamespaceKnowhereProvider,
    ReadOnlyChunkStore,
    SectionRow,
    UnitRow,
    knowhere_database_url,
)
from shared.services.retrieval.search.section_filters import is_excluded_section
from shared.services.retrieval.serving_manifest import (
    decode_namespace_map_snapshot,
    decode_serving_manifest,
)
from shared.services.retrieval.manifest_cache import get_cached_manifest_payloads
from shared.services.retrieval.namespace_map_snapshot_redis import (
    get_snapshot_blob,
    set_snapshot_blob,
)
from shared.core.exceptions.redis_exceptions import RedisOperationError


# Keep each payload query bounded under the API's 30-second statement timeout.
# Ten-thousand-row keyset pages avoid OFFSET scans while keeping the reference
# payload bounded under the API's 30-second asyncpg command timeout.
_CHUNK_BATCH_SIZE = 10_000
# Keep revision predicates bounded while reducing round trips for large
# namespaces. Keyset paging still caps each payload query at 10,000 rows.
_REVISION_GROUP_SIZE = 64
# Compressed manifests are efficient for small namespaces, but transferring a
# large set of binary payloads can exceed the asyncpg statement timeout before
# PostgreSQL has done meaningful work. For larger snapshots, the normalized
# section/chunk index loaders below transfer only the fields map-nav needs and
# page them by revision.
_MANIFEST_MAX_REVISION_COUNT = 32
_logger = logging.getLogger(__name__)


def _is_snapshot_timing_enabled() -> bool:
    """Enable detailed snapshot timings for local performance diagnosis."""
    return os.environ.get("RETRIEVAL_SNAPSHOT_TIMING", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class SnapshotSession(Protocol):
    """Minimal database interface required by the snapshot loader."""

    async def execute(self, statement: Executable) -> Result[tuple[object, ...]]:
        raise NotImplementedError

    async def rollback(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class NavSnapshot:
    """In-memory corpus for one map-nav episode."""

    provider: NamespaceKnowhereProvider
    chunk_ref_index: Mapping[str, dict[str, Any]]
    document_ids: list[str]
    document_titles: dict[str, str]
    document_revisions: Mapping[str, str] | None

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()


def build_nav_snapshot(
    *,
    document_titles: dict[str, str],
    sections_by_doc: dict[str, list[SectionRow]],
    units_by_doc: dict[str, list[UnitRow]],
    chunk_ref_index: dict[str, dict[str, Any]],
    document_revisions: Mapping[str, str] | None = None,
) -> NavSnapshot:
    """Assemble provider + index from already-fetched rows (also used by tests)."""
    doc_ids = [
        did for did in document_titles if did in sections_by_doc or did in units_by_doc
    ]
    if not doc_ids:
        raise ValueError("nav snapshot requires at least one active document")

    providers: list[KnowhereProvider] = []
    for doc_id in doc_ids:
        providers.append(
            KnowhereProvider(
                doc_id=doc_id,
                sections=sections_by_doc.get(doc_id, ()),
                units=units_by_doc.get(doc_id, ()),
            )
        )
    provider = NamespaceKnowhereProvider(providers, titles=document_titles)
    return NavSnapshot(
        provider=provider,
        chunk_ref_index=dict(chunk_ref_index),
        document_ids=list(provider.document_ids()),
        document_titles={
            did: document_titles.get(did, did) for did in provider.document_ids()
        },
        document_revisions=(
            {
                did: str(document_revisions.get(did, ""))
                for did in provider.document_ids()
                if document_revisions.get(did)
            }
            if document_revisions is not None
            else None
        ),
    )


async def load_nav_snapshot(
    db: SnapshotSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str] | None = None,
    exclude_sections: list[dict[str, str]] | None = None,
    lazy: bool = False,
    revision_pins: Mapping[str, str] | None = None,
    generation: int | None = None,
) -> NavSnapshot:
    """Preload namespace current revision into a sync map-nav snapshot."""
    excluded_docs = [
        str(x).strip() for x in (exclude_document_ids or ()) if str(x).strip()
    ]
    excluded_secs = list(exclude_sections or ())
    snapshot_started = time.perf_counter()

    if revision_pins is None:
        doc_stmt = (
            select(
                Document.document_id,
                Document.source_file_name,
                Document.current_job_result_id,
            )
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == "active")
            .where(Document.current_job_result_id.is_not(None))
            .order_by(Document.document_id)
        )
    else:
        pinned_document_ids = [str(document_id) for document_id in revision_pins]
        if not pinned_document_ids:
            raise ValueError("revision pins must include at least one document")
        doc_stmt = (
            select(Document.document_id, Document.source_file_name)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.document_id.in_(pinned_document_ids))
            .order_by(Document.document_id)
        )
    if excluded_docs:
        doc_stmt = doc_stmt.where(Document.document_id.notin_(excluded_docs))
    doc_query_started = time.perf_counter()
    doc_rows = list((await db.execute(doc_stmt)).all())
    doc_query_seconds = time.perf_counter() - doc_query_started
    if not doc_rows:
        raise ValueError(
            f"no active documents with current revision for "
            f"user_id={user_id!r} namespace={namespace!r}"
        )

    document_titles: dict[str, str] = {}
    current_job_result_ids: set[str] = set()
    document_revisions: list[tuple[str, str]] = []
    for doc_row in doc_rows:
        document_id = doc_row[0]
        source_file_name = doc_row[1]
        did = str(document_id)
        title = str(source_file_name or "").strip() or did
        document_titles[did] = title
        job_result_id = (
            str(revision_pins.get(did, ""))
            if revision_pins is not None
            else str(doc_row[2] or "")
        )
        if job_result_id:
            current_job_result_ids.add(job_result_id)
            document_revisions.append((did, job_result_id))

    job_query_started = time.perf_counter()
    job_result_rows = await db.execute(
        select(JobResult.id, JobResult.job_id).where(
            JobResult.id.in_(list(current_job_result_ids))
        )
    )
    job_query_seconds = time.perf_counter() - job_query_started
    job_id_by_result_id = {
        str(job_result_id): str(job_id)
        for job_result_id, job_id in job_result_rows.all()
        if job_result_id and job_id
    }

    snapshot_entries = await _resolve_namespace_snapshot_entries(
        db,
        user_id=user_id,
        namespace=namespace,
        document_revisions=document_revisions,
        expected_generation=generation,
    )
    if snapshot_entries is not None:
        parse_started = time.perf_counter()
        manifest_sections = _parse_manifest_entries(
            snapshot_entries,
            exclude_sections=excluded_secs,
            job_id_by_result_id=job_id_by_result_id,
        )
        parse_seconds = time.perf_counter() - parse_started
    else:
        _logger.warning(
            "retrieval snapshot fallback=manifest_merge user_id=%s namespace=%s documents=%d",
            user_id,
            namespace,
            len(document_revisions),
        )
        manifest_sections = None
        if len(document_revisions) <= _MANIFEST_MAX_REVISION_COUNT:
            manifest_sections = await _load_manifest_sections(
                db,
                document_revisions=document_revisions,
                exclude_sections=excluded_secs,
                job_id_by_result_id=job_id_by_result_id,
            )
        parse_seconds = 0.0
    if manifest_sections is None:
        _logger.warning(
            "retrieval snapshot fallback=table_scan user_id=%s namespace=%s documents=%d",
            user_id,
            namespace,
            len(document_revisions),
        )
        sections_by_doc, section_path_by_id = await _load_sections(
            db,
            document_revisions=document_revisions,
            exclude_sections=excluded_secs,
        )
        manifest_chunk_index = None
    else:
        sections_by_doc, section_path_by_id, manifest_chunk_index = manifest_sections
    if lazy:
        if manifest_chunk_index is None:
            chunk_ids_by_doc, chunk_ref_index, remounted_assets = await _load_chunk_index(
                db,
                document_revisions=document_revisions,
                exclude_sections=excluded_secs,
                section_path_by_id=section_path_by_id,
                job_id_by_result_id=job_id_by_result_id,
            )
        else:
            chunk_ids_by_doc, chunk_ref_index, remounted_assets = manifest_chunk_index
        kept_titles = {
            did: title
            for did, title in document_titles.items()
            if sections_by_doc.get(did)
        }
        if not kept_titles:
            raise ValueError(
                f"nav snapshot empty after excludes for "
                f"user_id={user_id!r} namespace={namespace!r}"
            )
        revisions = {
            did: result_id
            for did, result_id in document_revisions
            if did in kept_titles
        }
        store = ReadOnlyChunkStore(
            dsn=knowhere_database_url(),
            revisions=revisions,
            excluded_sections={
                (
                    str(item.get("document_id") or "").strip(),
                    section_id,
                )
                for section_id, section_path in section_path_by_id.items()
                for item in excluded_secs
                if isinstance(item, dict)
                and str(item.get("document_id") or "").strip()
                and str(item.get("section_path") or "").strip() == section_path
            },
        )
        try:
            providers = [
                LazyKnowhereProvider(
                    doc_id=did,
                    sections=sections_by_doc.get(did, ()),
                    chunk_store=store,
                    known_chunk_ids=chunk_ids_by_doc.get(did, ()),
                    root_asset_ids=remounted_assets.get(did, {}).get("root", ()),
                    remounted_assets_by_section=remounted_assets.get(did, {}).get(
                        "owners", {}
                    ),
                )
                for did in kept_titles
            ]
            provider = NamespaceKnowhereProvider(
                providers,
                titles=kept_titles,
                chunk_owner_by_id={
                    chunk_id: document_id
                    for document_id, chunk_ids in chunk_ids_by_doc.items()
                    for chunk_id in chunk_ids
                },
            )
            ref_index_started = time.perf_counter()
            lazy_ref_index = LazyChunkRefIndex(
                chunk_ref_index,
                resolver=store.load_chunk_reference_metadata,
            )
            ref_index_seconds = time.perf_counter() - ref_index_started
        except Exception:
            store.close()
            raise
        snapshot = NavSnapshot(
            provider=provider,
            chunk_ref_index=lazy_ref_index,
            document_ids=list(provider.document_ids()),
            document_titles={
                did: kept_titles.get(did, did) for did in provider.document_ids()
            },
            document_revisions=dict(revisions),
        )
        if _is_snapshot_timing_enabled():
            _logger.info(
                "retrieval snapshot timing total_seconds=%.3f parse_seconds=%.3f "
                "ref_index_copy_seconds=%.3f doc_query_seconds=%.3f "
                "job_query_seconds=%.3f documents=%d sections=%d refs=%d mode=lazy",
                time.perf_counter() - snapshot_started,
                parse_seconds,
                ref_index_seconds,
                doc_query_seconds,
                job_query_seconds,
                len(snapshot.document_ids),
                sum(len(rows) for rows in sections_by_doc.values()),
                len(snapshot.chunk_ref_index),
            )
        return snapshot

    units_by_doc, chunk_ref_index = await _load_chunks(
        db,
        document_revisions=document_revisions,
        exclude_sections=excluded_secs,
        section_path_by_id=section_path_by_id,
        job_id_by_result_id=job_id_by_result_id,
    )

    # Keep only documents that still have sections after exclude filters.
    kept_titles = {
        did: title for did, title in document_titles.items() if sections_by_doc.get(did)
    }
    if not kept_titles:
        raise ValueError(
            f"nav snapshot empty after excludes for "
            f"user_id={user_id!r} namespace={namespace!r}"
        )

    snapshot = build_nav_snapshot(
        document_titles=kept_titles,
        sections_by_doc={did: sections_by_doc.get(did, []) for did in kept_titles},
        units_by_doc={did: units_by_doc.get(did, []) for did in kept_titles},
        chunk_ref_index=chunk_ref_index,
        document_revisions={
            document_id: job_result_id
            for document_id, job_result_id in document_revisions
            if document_id in kept_titles
        },
    )
    if _is_snapshot_timing_enabled():
        _logger.info(
            "retrieval snapshot timing total_seconds=%.3f parse_seconds=%.3f "
            "doc_query_seconds=%.3f job_query_seconds=%.3f documents=%d "
            "sections=%d refs=%d mode=eager",
            time.perf_counter() - snapshot_started,
            parse_seconds,
            doc_query_seconds,
            job_query_seconds,
            len(snapshot.document_ids),
            sum(len(rows) for rows in sections_by_doc.values()),
            len(snapshot.chunk_ref_index),
        )
    return snapshot


async def _resolve_namespace_snapshot_entries(
    db: SnapshotSession,
    *,
    user_id: str,
    namespace: str,
    document_revisions: list[tuple[str, str]],
    expected_generation: int | None = None,
) -> list[tuple[object, ...]] | None:
    """Return manifest-shaped entries from the persisted namespace snapshot.

    Returns ``None`` (triggering the exact per-revision fallback) when the
    snapshot row is missing, corrupt, or stale for any requested revision.
    """
    generation_statement = select(RetrievalNamespaceGeneration.generation).where(
        RetrievalNamespaceGeneration.user_id == user_id,
        RetrievalNamespaceGeneration.namespace == namespace,
    )
    try:
        generation_result = await db.execute(generation_statement)
        _current_generation = generation_result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        await db.rollback()
        _logger.warning("retrieval snapshot generation lookup failed error=%s", exc)
        _current_generation = None
    if _is_snapshot_timing_enabled():
        _logger.info(
            "retrieval snapshot generation generation=%s",
            _current_generation,
        )
    generation_value = int(expected_generation) if expected_generation is not None else None
    statement = select(
        RetrievalNamespaceMapSnapshot.generation,
        RetrievalNamespaceMapSnapshot.checksum,
        RetrievalNamespaceMapSnapshot.format_version,
    ).where(
        RetrievalNamespaceMapSnapshot.user_id == user_id,
        RetrievalNamespaceMapSnapshot.namespace == namespace,
    )
    lookup_started = time.perf_counter()
    try:
        row = (await db.execute(statement)).first()
    except SQLAlchemyError as exc:
        await db.rollback()
        _logger.warning(
            "retrieval snapshot namespace lookup failed user_id=%s namespace=%s error=%s",
            user_id,
            namespace,
            exc,
        )
        return None
    lookup_seconds = time.perf_counter() - lookup_started
    if _is_snapshot_timing_enabled():
        _logger.info(
            "retrieval snapshot lookup seconds=%.3f found=%s",
            lookup_seconds,
            row is not None,
        )
    if row is None:
        return None
    generation, checksum, format_version = row
    row_generation = int(generation)
    if generation_value is not None and row_generation != generation_value:
        _logger.info(
            "retrieval snapshot generation mismatch row=%d expected=%d",
            row_generation,
            generation_value,
        )
        return None
    if (
        generation_value is None
        and _current_generation is not None
        and int(_current_generation) > 0
        and row_generation != int(_current_generation)
    ):
        _logger.info(
            "retrieval snapshot generation mismatch row=%d current=%d",
            row_generation,
            int(_current_generation),
        )
        return None
    cached_blob: bytes | None = None
    try:
        cached_blob = await get_snapshot_blob(
            user_id=user_id, namespace=namespace, generation=row_generation
        )
    except RedisOperationError as exc:
        _logger.warning("retrieval snapshot redis get failed error=%s", exc)
    database_blob: bytes | None = None
    if cached_blob is None:
        payload_result = await db.execute(
            select(RetrievalNamespaceMapSnapshot.payload_zlib).where(
                RetrievalNamespaceMapSnapshot.user_id == user_id,
                RetrievalNamespaceMapSnapshot.namespace == namespace,
            )
        )
        payload_row = payload_result.first()
        if payload_row is None or payload_row[0] is None:
            return None
        database_blob = bytes(payload_row[0])
        blob: bytes = database_blob
    else:
        blob = cached_blob
    is_timing_enabled = _is_snapshot_timing_enabled()
    decode_timings: dict[str, float] | None = {} if is_timing_enabled else None
    decode_started = time.perf_counter()
    try:
        payload = decode_namespace_map_snapshot(
            blob,
            checksum=str(checksum),
            format_version=int(format_version),
            timings=decode_timings,
        )
    except (ValueError, TypeError, zlib.error):
        if cached_blob is not None:
            # A stale/corrupt cache entry must never shadow the PostgreSQL source.
            try:
                if database_blob is None:
                    fallback_result = await db.execute(
                        select(RetrievalNamespaceMapSnapshot.payload_zlib).where(
                            RetrievalNamespaceMapSnapshot.user_id == user_id,
                            RetrievalNamespaceMapSnapshot.namespace == namespace,
                        )
                    )
                    fallback_row = fallback_result.first()
                    if fallback_row is None or fallback_row[0] is None:
                        return None
                    database_blob = bytes(fallback_row[0])
                assert database_blob is not None
                payload = decode_namespace_map_snapshot(
                    database_blob,
                    checksum=str(checksum),
                    format_version=int(format_version),
                    timings=decode_timings,
                )
                blob = database_blob
                cached_blob = None
            except (ValueError, TypeError, zlib.error):
                return None
        else:
            return None
    decoded_documents = payload.get("documents")
    if not isinstance(decoded_documents, dict):
        return None
    documents = decoded_documents
    if cached_blob is None:
        try:
            await set_snapshot_blob(
                user_id=user_id,
                namespace=namespace,
                generation=row_generation,
                payload_zlib=blob,
            )
        except RedisOperationError as exc:
            _logger.warning("retrieval snapshot redis set failed error=%s", exc)
        if is_timing_enabled:
            _logger.info(
                "retrieval snapshot decode cache_hit=false seconds=%.3f "
                "compressed_bytes=%d decompressed_bytes=%d "
                "decompress_seconds=%.3f checksum_seconds=%.3f "
                "json_decode_seconds=%.3f documents=%d",
                time.perf_counter() - decode_started,
                int((decode_timings or {}).get("compressed_bytes", 0.0)),
                int((decode_timings or {}).get("decompressed_bytes", 0.0)),
                (decode_timings or {}).get("decompress_seconds", 0.0),
                (decode_timings or {}).get("checksum_seconds", 0.0),
                (decode_timings or {}).get("json_decode_seconds", 0.0),
                len(documents),
            )
    elif is_timing_enabled:
        _logger.info(
            "retrieval snapshot decode cache_hit=true seconds=%.3f "
            "compressed_bytes=%d decompressed_bytes=%d decompress_seconds=%.3f "
            "checksum_seconds=%.3f json_decode_seconds=%.3f documents=%d",
            time.perf_counter() - decode_started,
            int((decode_timings or {}).get("compressed_bytes", 0.0)),
            int((decode_timings or {}).get("decompressed_bytes", 0.0)),
            (decode_timings or {}).get("decompress_seconds", 0.0),
            (decode_timings or {}).get("checksum_seconds", 0.0),
            (decode_timings or {}).get("json_decode_seconds", 0.0),
            len(documents),
        )
    entries: list[tuple[object, ...]] = []
    for document_id, job_result_id in document_revisions:
        entry = documents.get(document_id)
        if not isinstance(entry, dict) or str(entry.get("job_result_id") or "") != job_result_id:
            return None
        entries.append((document_id, job_result_id, entry, None, None))
    return entries


async def _resolve_manifest_entries(
    db: SnapshotSession,
    *,
    document_revisions: list[tuple[str, str]],
) -> list[tuple[object, ...]] | None:
    """Resolve per-revision manifest rows from the request cache or table."""
    cached_payloads = get_cached_manifest_payloads(
        db,
        revisions=dict(document_revisions),
    )
    if cached_payloads is not None and len(cached_payloads) == len(document_revisions):
        manifest_entries: list[tuple[object, ...]] = [
            (document_id, job_result_id, payload, None, None)
            for document_id, job_result_id in document_revisions
            for payload in [cached_payloads.get((document_id, job_result_id))]
            if payload is not None
        ]
        if len(manifest_entries) != len(document_revisions):
            return None
        return manifest_entries

    manifest_entries = []
    for group_start in range(0, len(document_revisions), _REVISION_GROUP_SIZE):
        revision_group = document_revisions[
            group_start : group_start + _REVISION_GROUP_SIZE
        ]
        statement = select(
            RetrievalServingRevisionManifest.document_id,
            RetrievalServingRevisionManifest.job_result_id,
            RetrievalServingRevisionManifest.payload_zlib,
            RetrievalServingRevisionManifest.checksum,
            RetrievalServingRevisionManifest.format_version,
        ).where(
            tuple_(
                RetrievalServingRevisionManifest.document_id,
                RetrievalServingRevisionManifest.job_result_id,
            ).in_(revision_group)
        )
        try:
            rows = (await db.execute(statement)).all()
        except SQLAlchemyError as exc:
            await db.rollback()
            _logger.warning(
                "retrieval manifest lookup failed revisions=%s error=%s",
                revision_group,
                exc,
            )
            return None
        if len(rows) != len(revision_group):
            return None
        manifest_entries.extend(tuple(row) for row in rows)
    return manifest_entries


async def _load_manifest_sections(
    db: SnapshotSession,
    *,
    document_revisions: list[tuple[str, str]],
    exclude_sections: list[dict[str, str]],
    job_id_by_result_id: dict[str, str],
) -> tuple[
    dict[str, list[SectionRow]],
    dict[str, str],
    tuple[dict[str, list[str]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
] | None:
    """Load section metadata from complete serving manifests when available."""
    manifest_entries = await _resolve_manifest_entries(
        db, document_revisions=document_revisions
    )
    if manifest_entries is None:
        return None
    return _parse_manifest_entries(
        manifest_entries,
        exclude_sections=exclude_sections,
        job_id_by_result_id=job_id_by_result_id,
    )


def _parse_manifest_entries(
    manifest_entries: list[tuple[object, ...]],
    *,
    exclude_sections: list[dict[str, str]],
    job_id_by_result_id: dict[str, str],
) -> tuple[
    dict[str, list[SectionRow]],
    dict[str, str],
    tuple[dict[str, list[str]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
] | None:
    """Parse manifest-shaped entries (decoded dict or raw compressed row) into sections."""
    by_doc: dict[str, list[SectionRow]] = {}
    path_by_id: dict[str, str] = {}
    ids_by_doc: dict[str, list[str]] = {}
    ref_index: dict[str, dict[str, Any]] = {}
    root_assets_by_doc: dict[str, set[str]] = {}
    text_connections_by_doc: dict[str, list[tuple[str, str]]] = {}
    section_seconds = 0.0
    chunk_seconds = 0.0
    section_count = 0
    chunk_count = 0
    connection_count = 0
    try:
        for document_id, _job_result_id, payload_zlib, checksum, format_version in manifest_entries:
            if isinstance(payload_zlib, dict):
                payload = payload_zlib
            else:
                if not isinstance(payload_zlib, (bytes, bytearray, memoryview)):
                    return None
                if checksum is None or format_version is None:
                    return None
                payload = decode_serving_manifest(
                    bytes(payload_zlib),
                    checksum=str(checksum),
                    format_version=int(str(format_version)),
                )
            section_started = time.perf_counter()
            raw_sections = payload.get("sections")
            if not isinstance(raw_sections, list):
                return None
            for raw_section in raw_sections:
                if not isinstance(raw_section, dict):
                    return None
                section_path = str(raw_section.get("section_path") or "")
                if is_excluded_section(
                    document_id=str(document_id),
                    section_path=section_path,
                    exclude_sections=exclude_sections,
                ):
                    continue
                section_id = str(raw_section.get("section_id") or "")
                if not section_id or not section_path:
                    return None
                section = SectionRow(
                    section_id=section_id,
                    parent_section_id=(
                        str(raw_section["parent_section_id"])
                        if raw_section.get("parent_section_id")
                        else None
                    ),
                    section_path=section_path,
                    section_title=str(raw_section.get("section_title") or "").strip(),
                    section_level=int(raw_section.get("section_level") or 0),
                    summary=str(raw_section.get("summary") or "").strip(),
                    sort_order=int(raw_section.get("sort_order") or 0),
                )
                by_doc.setdefault(str(document_id), []).append(section)
                path_by_id[section_id] = section_path
                section_count += 1
            section_seconds += time.perf_counter() - section_started
            chunk_started = time.perf_counter()
            raw_chunks = payload.get("chunks")
            if not isinstance(raw_chunks, list):
                return None
            for raw_chunk in raw_chunks:
                if not isinstance(raw_chunk, dict):
                    return None
                chunk_id = str(raw_chunk.get("chunk_id") or "").strip()
                if not chunk_id:
                    return None
                section_id = (
                    str(raw_chunk["section_id"])
                    if raw_chunk.get("section_id")
                    else None
                )
                section_path = path_by_id.get(section_id) if section_id else None
                if is_excluded_section(
                    document_id=str(document_id),
                    section_path=section_path,
                    exclude_sections=exclude_sections,
                ) or (section_id and section_id not in path_by_id):
                    continue
                chunk_type = str(raw_chunk.get("chunk_type") or "text")
                meta = {
                    "document_id": str(document_id),
                    "section_path": section_path,
                    "chunk_type": chunk_type,
                    "file_path": None,
                    "job_id": job_id_by_result_id.get(str(_job_result_id)),
                }
                document_key = str(document_id)
                ids_by_doc.setdefault(document_key, []).append(chunk_id)
                ref_index[chunk_id] = meta
                ref_index[f"{document_key}:{chunk_id}"] = meta
                if (
                    chunk_type in {"image", "table"}
                    and section_id
                    and section_path == "Root"
                ):
                    root_assets_by_doc.setdefault(document_key, set()).add(chunk_id)
                connections = raw_chunk.get("connect_to")
                if chunk_type == "text" and isinstance(connections, list):
                    for connection in connections:
                        target = (
                            str(connection.get("target") or "").strip()
                            if isinstance(connection, dict)
                            else str(connection or "").strip()
                        )
                        if target:
                            text_connections_by_doc.setdefault(document_key, []).append(
                                (section_id or "", target)
                            )
                            connection_count += 1
                chunk_count += 1
            chunk_seconds += time.perf_counter() - chunk_started
    except (TypeError, ValueError, KeyError):
        return None
    remounted: dict[str, dict[str, Any]] = {}
    remount_started = time.perf_counter()
    for document_id, asset_ids in root_assets_by_doc.items():
        owners: dict[str, list[str]] = {}
        for section_id, target in text_connections_by_doc.get(document_id, ()):
            if target in asset_ids:
                owners.setdefault(section_id, []).append(target)
        remounted[document_id] = {"root": sorted(asset_ids), "owners": owners}
    if _is_snapshot_timing_enabled():
        _logger.info(
            "retrieval snapshot parse sections=%d chunks=%d connections=%d "
            "section_seconds=%.3f chunk_seconds=%.3f remount_seconds=%.3f "
            "ref_index_keys=%d",
            section_count,
            chunk_count,
            connection_count,
            section_seconds,
            chunk_seconds,
            time.perf_counter() - remount_started,
            len(ref_index),
        )
    return by_doc, path_by_id, (ids_by_doc, ref_index, remounted)


async def _load_chunk_index(
    db: SnapshotSession,
    *,
    document_revisions: list[tuple[str, str]],
    exclude_sections: list[dict[str, str]],
    section_path_by_id: dict[str, str],
    job_id_by_result_id: dict[str, str],
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load only IDs/reference metadata; content and asset paths remain lazy."""
    ids_by_doc: dict[str, list[str]] = {}
    ref_index: dict[str, dict[str, Any]] = {}
    root_assets_by_doc: dict[str, set[str]] = {}
    text_connections_by_doc: dict[str, list[tuple[str, str]]] = {}
    query_seconds = 0.0
    assembly_seconds = 0.0
    query_count = 0
    for group_start in range(0, len(document_revisions), _REVISION_GROUP_SIZE):
        revision_group = document_revisions[group_start : group_start + _REVISION_GROUP_SIZE]
        last_key: tuple[str, str, int, str, str] | None = None
        while True:
            stmt = (
                select(
                    DocumentChunk.document_id,
                    DocumentChunk.job_result_id,
                    DocumentChunk.chunk_id,
                    DocumentChunk.section_id,
                    DocumentChunk.chunk_type,
                    # Asset paths are only needed for selected references and
                    # are resolved by ``LazyChunkRefIndex`` at bridge time.
                    DocumentChunk.chunk_metadata["connect_to"].label("connect_to"),
                    DocumentChunk.sort_order,
                    DocumentChunk.id,
                )
                .where(tuple_(DocumentChunk.document_id, DocumentChunk.job_result_id).in_(revision_group))
                .order_by(
                    DocumentChunk.document_id,
                    DocumentChunk.job_result_id,
                    DocumentChunk.sort_order,
                    DocumentChunk.chunk_id,
                    DocumentChunk.id,
                )
                .limit(_CHUNK_BATCH_SIZE)
            )
            if last_key is not None:
                stmt = stmt.where(
                    tuple_(
                        DocumentChunk.document_id,
                        DocumentChunk.job_result_id,
                        DocumentChunk.sort_order,
                        DocumentChunk.chunk_id,
                        DocumentChunk.id,
                    )
                    > tuple_(*[literal(value) for value in last_key])
                )
            query_started = time.perf_counter()
            rows = (await db.execute(stmt)).all()
            query_seconds += time.perf_counter() - query_started
            query_count += 1
            if not rows:
                break
            assembly_started = time.perf_counter()
            for row in rows:
                document_id = str(row[0])
                job_result_id = str(row[1])
                chunk_id = str(row[2] or "").strip()
                section_id = str(row[3]) if row[3] else None
                section_path = section_path_by_id.get(section_id) if section_id else None
                if is_excluded_section(
                    document_id=document_id,
                    section_path=section_path,
                    exclude_sections=exclude_sections,
                ) or (section_id and section_id not in section_path_by_id):
                    continue
                if not chunk_id:
                    continue
                chunk_type = str(row[4] or "text")
                meta = {
                    "document_id": document_id,
                    "section_path": section_path,
                    "chunk_type": chunk_type,
                    "file_path": None,
                    "job_id": job_id_by_result_id.get(job_result_id),
                }
                ids_by_doc.setdefault(document_id, []).append(chunk_id)
                ref_index[chunk_id] = meta
                ref_index[f"{document_id}:{chunk_id}"] = meta
                if (
                    chunk_type in {"image", "table"}
                    and section_id
                    and section_path == "Root"
                ):
                    root_assets_by_doc.setdefault(document_id, set()).add(chunk_id)
                connections = row[5]
                if isinstance(connections, str) and connections.strip():
                    try:
                        connections = json.loads(connections)
                    except json.JSONDecodeError:
                        connections = None
                if chunk_type == "text" and isinstance(connections, list):
                    for connection in connections:
                        if not isinstance(connection, dict):
                            continue
                        target = str(connection.get("target") or "").strip()
                        if not target:
                            continue
                        text_connections_by_doc.setdefault(document_id, []).append(
                            (section_id or "", target)
                        )
            assembly_seconds += time.perf_counter() - assembly_started
            last = rows[-1]
            last_key = (
                str(last[0]),
                str(last[1]),
                int(last[6] or 0),
                str(last[2] or ""),
                str(last[7]),
            )
            if len(rows) < _CHUNK_BATCH_SIZE:
                break
    remounted: dict[str, dict[str, Any]] = {}
    for document_id, asset_ids in root_assets_by_doc.items():
        owners: dict[str, list[str]] = {}
        for section_id, target in text_connections_by_doc.get(document_id, ()):
            if target in asset_ids:
                owners.setdefault(section_id, []).append(target)
        remounted[document_id] = {"root": sorted(asset_ids), "owners": owners}
    _logger.info(
        "retrieval snapshot phase=chunk_index rows=%d queries=%d query_seconds=%.3f assembly_seconds=%.3f",
        sum(len(chunk_ids) for chunk_ids in ids_by_doc.values()),
        query_count,
        query_seconds,
        assembly_seconds,
    )
    return ids_by_doc, ref_index, remounted


class LazyChunkRefIndex(Mapping[str, dict[str, Any]]):
    """Reference metadata map that resolves selected asset paths on demand.

    Snapshot construction still records every chunk identity, section path,
    type, and job id so navigation ownership is unchanged. ``file_path`` is
    fetched only when the exit bridge asks for a selected reference.
    """

    def __init__(
        self,
        base: Mapping[str, dict[str, Any]],
        *,
        resolver: Callable[[str, str], Mapping[str, Any] | None],
    ) -> None:
        self._base = {str(key): dict(value) for key, value in base.items()}
        self._resolver = resolver

    def __getitem__(self, key: str) -> dict[str, Any]:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)

    def get(self, key: str, default: Any = None) -> dict[str, Any] | Any:
        value = self._base.get(str(key))
        if value is None:
            return default
        if value.get("file_path") is None:
            document_id = str(value.get("document_id") or "").strip()
            chunk_id = str(key).split(":", 1)[-1].strip()
            resolved = self._resolver(document_id, chunk_id)
            if resolved is not None:
                value.update({"file_path": resolved.get("file_path") or None})
        return value


async def _load_sections(
    db: SnapshotSession,
    *,
    document_revisions: list[tuple[str, str]],
    exclude_sections: list[dict[str, str]],
) -> tuple[dict[str, list[SectionRow]], dict[str, str]]:
    # Captured pairs replace DocumentSection.job_result_id == Document.current_job_result_id.
    by_doc: dict[str, list[SectionRow]] = {}
    path_by_id: dict[str, str] = {}
    document_ids = [document_id for document_id, _ in document_revisions]
    job_result_ids = [job_result_id for _, job_result_id in document_revisions]
    revision_rows = (
        func.unnest(
            cast(
                bindparam("section_document_ids", value=document_ids), ARRAY(String())
            ),
            cast(
                bindparam("section_job_result_ids", value=job_result_ids),
                ARRAY(String()),
            ),
        )
        .table_valued("document_id", "job_result_id")
        .render_derived(name="revisions")
    )
    last_key: tuple[str, str, int, str] | None = None
    query_seconds = 0.0
    assembly_seconds = 0.0
    query_count = 0
    row_count = 0
    while True:
        stmt = (
            select(
                DocumentSection.document_id,
                DocumentSection.section_id,
                DocumentSection.parent_section_id,
                DocumentSection.section_path,
                DocumentSection.section_title,
                DocumentSection.section_level,
                DocumentSection.summary,
                DocumentSection.sort_order,
                DocumentSection.job_result_id,
            )
            .join(
                revision_rows,
                (DocumentSection.document_id == revision_rows.c.document_id)
                & (DocumentSection.job_result_id == revision_rows.c.job_result_id),
            )
            .order_by(
                DocumentSection.document_id,
                DocumentSection.job_result_id,
                DocumentSection.sort_order,
                DocumentSection.section_id,
            )
            .limit(_CHUNK_BATCH_SIZE)
        )
        if last_key is not None:
            stmt = stmt.where(
                tuple_(
                    DocumentSection.document_id,
                    DocumentSection.job_result_id,
                    DocumentSection.sort_order,
                    DocumentSection.section_id,
                )
                > tuple_(*[literal(value) for value in last_key])
            )
        query_started = time.perf_counter()
        rows = (await db.execute(stmt)).all()
        query_seconds += time.perf_counter() - query_started
        query_count += 1
        if not rows:
            break
        row_count += len(rows)
        assembly_started = time.perf_counter()
        for row in rows:
            document_id = str(row[0])
            section_path = str(row[3] or "")
            if is_excluded_section(
                document_id=document_id,
                section_path=section_path,
                exclude_sections=exclude_sections,
            ):
                continue
            section_id = str(row[1])
            section = SectionRow(
                section_id=section_id,
                parent_section_id=str(row[2]) if row[2] else None,
                section_path=section_path,
                section_title=str(row[4] or "").strip(),
                section_level=int(row[5] or 0),
                summary=str(row[6] or "").strip(),
                sort_order=int(row[7] or 0),
            )
            by_doc.setdefault(document_id, []).append(section)
            path_by_id[section_id] = section_path
        assembly_seconds += time.perf_counter() - assembly_started
        last = rows[-1]
        last_key = (
            str(last[0]),
            str(last[8]),
            int(last[7] or 0),
            str(last[1]),
        )
        if len(rows) < _CHUNK_BATCH_SIZE:
            break
    _logger.info(
        "retrieval snapshot phase=sections rows=%d queries=%d query_seconds=%.3f assembly_seconds=%.3f",
        row_count,
        query_count,
        query_seconds,
        assembly_seconds,
    )
    return by_doc, path_by_id


async def _load_chunks(
    db: SnapshotSession,
    *,
    document_revisions: list[tuple[str, str]],
    exclude_sections: list[dict[str, str]],
    section_path_by_id: dict[str, str],
    job_id_by_result_id: dict[str, str],
) -> tuple[dict[str, list[UnitRow]], dict[str, dict[str, Any]]]:
    # Captured pairs replace DocumentChunk.document_id == document_id and
    # DocumentChunk.job_result_id == job_result_id.
    by_doc: dict[str, list[UnitRow]] = {}
    ref_index: dict[str, dict[str, Any]] = {}
    for group_start in range(0, len(document_revisions), _REVISION_GROUP_SIZE):
        revision_group = document_revisions[
            group_start : group_start + _REVISION_GROUP_SIZE
        ]
        last_key: tuple[str, str, int, str, str] | None = None
        while True:
            stmt = (
                select(
                    DocumentChunk.document_id,
                    DocumentChunk.job_result_id,
                    DocumentChunk.chunk_id,
                    DocumentChunk.section_id,
                    DocumentChunk.chunk_type,
                    DocumentChunk.content,
                    DocumentChunk.sort_order,
                    DocumentChunk.source_chunk_path,
                    DocumentChunk.file_path,
                    DocumentChunk.chunk_metadata,
                    DocumentChunk.id,
                )
                .where(
                    tuple_(
                        DocumentChunk.document_id,
                        DocumentChunk.job_result_id,
                    ).in_(revision_group)
                )
                .order_by(
                    DocumentChunk.document_id,
                    DocumentChunk.job_result_id,
                    DocumentChunk.sort_order,
                    DocumentChunk.chunk_id,
                    DocumentChunk.id,
                )
                .limit(_CHUNK_BATCH_SIZE)
            )
            if last_key is not None:
                stmt = stmt.where(
                    tuple_(
                        DocumentChunk.document_id,
                        DocumentChunk.job_result_id,
                        DocumentChunk.sort_order,
                        DocumentChunk.chunk_id,
                        DocumentChunk.id,
                    )
                    > tuple_(
                        literal(last_key[0]),
                        literal(last_key[1]),
                        literal(last_key[2]),
                        literal(last_key[3]),
                        literal(last_key[4]),
                    )
                )

            rows = (await db.execute(stmt)).all()
            if not rows:
                break
            for row in rows:
                document_id = str(row[0])
                job_result_id = str(row[1])
                chunk_id = str(row[2] or "").strip()
                section_id = str(row[3]) if row[3] else None
                section_path: str | None = (
                    section_path_by_id.get(section_id) if section_id else None
                )
                if is_excluded_section(
                    document_id=document_id,
                    section_path=section_path,
                    exclude_sections=exclude_sections,
                ):
                    continue
                if section_id and section_id not in section_path_by_id:
                    continue

                unit = UnitRow(
                    chunk_id=chunk_id,
                    section_id=section_id,
                    chunk_type=str(row[4] or "text"),
                    content=str(row[5] or ""),
                    sort_order=int(row[6] or 0),
                    source_chunk_path=str(row[7] or ""),
                    file_path=str(row[8] or ""),
                    metadata=_as_meta(row[9]),
                )
                by_doc.setdefault(document_id, []).append(unit)
                if chunk_id:
                    meta = {
                        "document_id": document_id,
                        "section_path": section_path,
                        "chunk_type": unit.chunk_type,
                        "file_path": unit.file_path or None,
                        "job_id": job_id_by_result_id.get(job_result_id),
                    }
                    # Bare chunk_id (last-wins) plus doc-scoped key so the same
                    # chunk_id can appear under multiple documents.
                    ref_index[chunk_id] = meta
                    ref_index[f"{document_id}:{chunk_id}"] = meta
            last_row = rows[-1]
            last_key = (
                str(last_row[0]),
                str(last_row[1]),
                int(last_row[6] or 0),
                str(last_row[2] or ""),
                str(last_row[10]),
            )
            if len(rows) < _CHUNK_BATCH_SIZE:
                break
    return by_doc, ref_index


def _as_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}
