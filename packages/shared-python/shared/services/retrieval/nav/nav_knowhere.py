"""Knowhere-native hierarchy provider for MAP-NAV.

Knowhere stores everything MAP-NAV needs in two tables:

``document_sections``
    ``section_id`` (PK) / ``parent_section_id`` (self FK) / ``section_path`` /
    ``section_title`` / ``section_level`` / ``summary`` / ``sort_order``
``document_chunks``
    ``chunk_id`` / ``section_id`` (FK) / ``chunk_type`` / ``content`` /
    ``chunk_metadata`` / ``sort_order``

``SectionRow`` / ``UnitRow`` mirror those shapes. ``KnowhereProvider`` is a
synchronous in-memory snapshot (so the nav kernel stays sync inside knowhere's
async path). Load from the production Postgres schema via
``load_document_from_db`` / ``load_namespace_from_db`` (local Docker or prod).

Hierarchy comes from ``parent_section_id`` and depth from ``section_level``,
not from parsing ``section_id`` or ``section_path`` separators.
"""

from __future__ import annotations

import os
import logging
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)

from .nav_address import NavLevel
from .nav_hierarchy import NodeMeta
from .knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    tokenize_query_for_ranker,
)

_ASSET_TYPES = ("table", "image")
# Knowhere sentinel path for the virtual document container (not a collectable leaf).
ROOT_SECTION_PATH = "Root"
_DEFAULT_DSN = "postgresql://root:root123@127.0.0.1:5433/Knowhere"
_MAP_SCORE_CHANNELS: Tuple[str, str] = ("path", "content")
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SectionRow:
    """One ``document_sections`` row."""

    section_id: str
    parent_section_id: Optional[str]
    section_path: str
    section_title: str
    section_level: int
    summary: str
    sort_order: int


@dataclass(frozen=True)
class UnitRow:
    """One ``document_chunks`` row."""

    chunk_id: str
    section_id: Optional[str]
    chunk_type: str
    content: str
    sort_order: int
    source_chunk_path: str = ""
    file_path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


def asset_display_text(unit: UnitRow) -> str:
    """Body text for an asset unit, whose ``content`` is only a file path.

    Mirrors knowhere's own assembly: an asset contributes its summary, not its
    path. Without this an asset unit is unscorable and unreadable.
    """
    meta = unit.metadata or {}
    title = str(meta.get("asset_title") or "").strip()
    summary = str(meta.get("summary") or "").strip()
    ref = unit.file_path or unit.source_chunk_path or unit.content
    label = "Table" if unit.chunk_type == "table" else "Image"
    parts = [f"[{label}: {ref}]"] if ref else [f"[{label}]"]
    if title:
        parts.append(title)
    if summary:
        parts.append(summary)
    return "\n".join(parts)


def normalize_section_path(path: str) -> str:
    """Canonical path for gold/lookup: ``a / b`` (accepts ``a/b`` or ``a / b``)."""
    raw = str(path or "").strip().strip("/")
    if not raw or raw == ROOT_SECTION_PATH:
        return ""
    if " / " in raw:
        parts = [p.strip() for p in raw.split(" / ") if p.strip()]
    else:
        parts = [p.strip() for p in raw.split("/") if p.strip()]
    return " / ".join(parts)


def is_root_section_path(path: str) -> bool:
    """True when the raw ``section_path`` is Knowhere's Root container."""
    return str(path or "").strip() == ROOT_SECTION_PATH


def is_root_section(provider_or_ts: Any, section_id: str) -> bool:
    """True when ``section_id`` is a Root container (raw path, not normalized)."""
    sid = str(section_id or "").strip()
    if not sid:
        return False
    path_fn = getattr(provider_or_ts, "section_path", None)
    if callable(path_fn):
        return is_root_section_path(path_fn(sid))
    provider = getattr(provider_or_ts, "_provider", None)
    path_fn = getattr(provider, "section_path", None) if provider is not None else None
    if callable(path_fn):
        return is_root_section_path(path_fn(sid))
    return False


def _connect_to_targets(metadata: Dict[str, Any]) -> List[str]:
    """``chunk_metadata.connect_to[].target`` ids (document order, first wins upstream)."""
    raw = metadata.get("connect_to") if isinstance(metadata, dict) else None
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for conn in raw:
        if not isinstance(conn, dict):
            continue
        target = str(conn.get("target") or "").strip()
        if target:
            out.append(target)
    return out


def knowhere_database_url() -> str:
    configured = (
        str(os.environ.get("KNOWHERE_DATABASE_URL") or "").strip()
        or str(os.environ.get("DATABASE_URL") or "").strip()
    )
    if configured:
        # ``ReadOnlyChunkStore`` uses psycopg2's native connector, which
        # accepts libpq URLs but not SQLAlchemy's ``+driver`` suffix.
        return configured.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
            "postgresql+psycopg2://", "postgresql://", 1
        )
    return _DEFAULT_DSN


class ChunkStore(Protocol):
    def load_chunk_reference_metadata(
        self,
        document_id: str,
        chunk_id: str,
    ) -> Optional[Mapping[str, Any]]:
        raise NotImplementedError

    def load_persisted_score_corpus(
        self,
        document_ids: Sequence[str],
        allowed_section_ids_by_document: Mapping[str, Sequence[str]],
        queries: Sequence[str],
    ) -> Optional[PersistedScoreCorpus]:
        raise NotImplementedError

    def load_section_units(
        self,
        document_id: str,
        section_id: str,
        extra_chunk_ids: Sequence[str] = (),
    ) -> List[UnitRow]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class ReadOnlyChunkStore:
    """Episode-local, revision-pinned loader for lazy map-nav chunks."""

    def __init__(
        self,
        *,
        dsn: str,
        revisions: Dict[str, str],
        excluded_sections: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> None:
        self._dsn = str(dsn)
        self._revisions = dict(revisions)
        self._excluded_sections = set(excluded_sections or ())
        self._conn: Optional[_SyncConnection] = None
        self._score_unit_rows_cache: dict[
            tuple[tuple[str, str], ...], list[dict[str, object]]
        ] = {}
        self._score_average_idf_cache: dict[
            tuple[tuple[str, str], ...], tuple[float, float]
        ] = {}

    def _connection(self) -> "_SyncConnection":
        if self._conn is None:
            self._conn = _connect(self._dsn)
            self._conn.set_session(readonly=True, autocommit=True)
        return self._conn

    def load_section_units(
        self,
        document_id: str,
        section_id: str,
        extra_chunk_ids: Sequence[str] = (),
    ) -> List[UnitRow]:
        doc_id = str(document_id).strip()
        sid = str(section_id).strip()
        job_result_id = self._revisions.get(doc_id)
        if (
            not doc_id
            or not sid
            or not job_result_id
            or (doc_id, sid) in self._excluded_sections
        ):
            return []
        cur = self._connection().cursor()
        try:
            ids = [
                str(chunk_id).strip()
                for chunk_id in extra_chunk_ids
                if str(chunk_id).strip()
            ]
            if ids:
                cur.execute(
                    "SELECT chunk_id, section_id, chunk_type, content, sort_order, "
                    "source_chunk_path, file_path, chunk_metadata "
                    "FROM document_chunks "
                    "WHERE document_id = %s AND job_result_id = %s "
                    "AND (section_id = %s OR chunk_id = ANY(%s)) "
                    "ORDER BY sort_order, chunk_id, id",
                    (doc_id, job_result_id, sid, ids),
                )
            else:
                cur.execute(
                    "SELECT chunk_id, section_id, chunk_type, content, sort_order, "
                    "source_chunk_path, file_path, chunk_metadata "
                    "FROM document_chunks "
                    "WHERE document_id = %s AND job_result_id = %s AND section_id = %s "
                    "ORDER BY sort_order, chunk_id, id",
                    (doc_id, job_result_id, sid),
                )
            return [_unit_from_row(row) for row in cur.fetchall()]
        finally:
            cur.close()

    def load_chunk_reference_metadata(
        self,
        document_id: str,
        chunk_id: str,
    ) -> Optional[Mapping[str, Any]]:
        """Resolve deferred reference fields for one selected chunk."""
        doc_id = str(document_id).strip()
        cid = str(chunk_id).strip()
        job_result_id = self._revisions.get(doc_id)
        if not doc_id or not cid or not job_result_id:
            return None
        cur = self._connection().cursor()
        try:
            cur.execute(
                "SELECT file_path FROM document_chunks "
                "WHERE document_id = %s AND job_result_id = %s AND chunk_id = %s "
                "ORDER BY sort_order DESC, id DESC LIMIT 1",
                (doc_id, job_result_id, cid),
            )
            row = cur.fetchone()
            return {"file_path": str(row[0] or "") or None} if row else None
        finally:
            cur.close()

    def load_persisted_score_corpus(
        self,
        document_ids: Sequence[str],
        allowed_section_ids_by_document: Mapping[str, Sequence[str]],
        queries: Sequence[str],
    ) -> Optional[PersistedScoreCorpus]:
        """Load query-token score inputs from map-unit tables when indexed.

        Units and lengths come from ``document_map_units``. Frequencies come from
        ``document_map_unit_tokens`` filtered to the query tokens. Average IDF
        comes from ``document_map_unit_indexes`` (written at index time).
        """
        from shared.services.retrieval.nav.persisted_score_load import (
            build_channel_bm25_stats,
            combine_average_idf,
        )

        revisions = [
            (document_id, self._revisions[document_id])
            for raw_document_id in document_ids
            if (document_id := str(raw_document_id).strip()) in self._revisions
        ]
        if not revisions or len(revisions) != len(document_ids):
            return None
        values_sql = ", ".join(["(%s, %s)"] * len(revisions))
        revision_params: List[object] = [
            value for revision in revisions for value in revision
        ]
        unique_queries = list(dict.fromkeys(str(query) for query in queries))
        query_tokens = list(
            dict.fromkeys(
                token
                for query in unique_queries
                for token in tokenize_query_for_ranker(query)
            )
        )
        query_token_hashes = [
            sha256(token.encode("utf-8")).hexdigest() for token in query_tokens
        ]
        cur = self._connection().cursor()
        try:
            revision_key = tuple(revisions)
            stage_started = time.perf_counter()
            try:
                cur.execute(
                    "SELECT indexes.document_id, indexes.job_result_id, "
                    "indexes.format_version, indexes.unit_count, "
                    "indexes.average_idf_path, indexes.average_idf_content, "
                    "indexes.path_document_count, indexes.path_total_length, "
                    "indexes.content_document_count, indexes.content_total_length "
                    "FROM document_map_unit_indexes AS indexes "
                    f"JOIN (VALUES {values_sql}) AS revisions(document_id, job_result_id) "
                    "ON indexes.document_id = revisions.document_id "
                    "AND indexes.job_result_id = revisions.job_result_id",
                    revision_params,
                )
            except Exception as exc:
                if getattr(exc, "pgcode", None) in {"42P01", "42703"}:
                    return None
                raise
            index_rows = list(cur.fetchall())
            _logger.info(
                "retrieval map-index load stage=indexes seconds=%.3f rows=%d",
                time.perf_counter() - stage_started,
                len(index_rows),
            )
            if len(index_rows) != len(revisions) or any(
                len(row) < 10 or int(row[2]) != MAP_UNIT_INDEX_FORMAT_VERSION
                or any(value is None for value in row[6:10])
                for row in index_rows
            ):
                return None

            if revision_key in self._score_average_idf_cache:
                average_idf_path, average_idf_content = self._score_average_idf_cache[
                    revision_key
                ]
            else:
                average_idf_path = combine_average_idf(
                    [(float(row[4] or 0.0), int(row[3] or 0)) for row in index_rows]
                )
                average_idf_content = combine_average_idf(
                    [
                        (float(row[5] or 0.0), int(row[3] or 0))
                        for row in index_rows
                    ]
                )
                self._score_average_idf_cache[revision_key] = (
                    average_idf_path,
                    average_idf_content,
                )

            allowed_pairs_set: set[tuple[str, str]] = set()
            if not self._excluded_sections:
                # The lazy snapshot was built from the complete pinned
                # namespace and no section filters were requested. Avoid a
                # second GROUP BY over document_sections just to prove the
                # same fact; filtered snapshots retain the exact validation
                # query below.
                has_complete_section_scope = True
            else:
                allowed_by_document = {
                    str(document_id): {str(section_id) for section_id in section_ids}
                    for document_id, section_ids in allowed_section_ids_by_document.items()
                }
                allowed_pairs_set = {
                    (document_id, section_id)
                    for document_id, section_ids in allowed_by_document.items()
                    for section_id in section_ids
                }
                cur.execute(
                    "SELECT sections.document_id, sections.job_result_id, count(*) "
                    "FROM document_sections AS sections "
                    f"JOIN (VALUES {values_sql}) AS revisions(document_id, job_result_id) "
                    "ON sections.document_id = revisions.document_id "
                    "AND sections.job_result_id = revisions.job_result_id "
                    "GROUP BY sections.document_id, sections.job_result_id",
                    revision_params,
                )
                section_counts = {
                    (str(document_id), str(job_result_id)): int(count)
                    for document_id, job_result_id, count in cur.fetchall()
                }
                has_complete_section_scope = all(
                    len(allowed_by_document.get(document_id, set()))
                    == section_counts.get((document_id, job_result_id), 0)
                    for document_id, job_result_id in revisions
                )
            all_unit_rows = self._score_unit_rows_cache.get(revision_key)
            if has_complete_section_scope and query_token_hashes:
                stage_started = time.perf_counter()
                cur.execute(
                    "WITH matching_tokens AS MATERIALIZED ("
                    "SELECT DISTINCT map_unit_id FROM document_map_unit_tokens "
                    "WHERE channel = ANY(%s) AND token_hash = ANY(%s)"
                    "), scoped_units AS MATERIALIZED ("
                    f"SELECT units.id, units.document_id, units.unit_id, units.section_id, "
                    "units.path_token_count, units.content_token_count "
                    "FROM document_map_units AS units "
                    f"JOIN (VALUES {values_sql}) AS revisions(document_id, job_result_id) "
                    "ON units.document_id = revisions.document_id "
                    "AND units.job_result_id = revisions.job_result_id"
                    ") SELECT scoped_units.id, scoped_units.document_id, "
                    "scoped_units.unit_id, scoped_units.section_id, "
                    "scoped_units.path_token_count, scoped_units.content_token_count "
                    "FROM matching_tokens JOIN scoped_units "
                    "ON scoped_units.id = matching_tokens.map_unit_id",
                    [
                        list(_MAP_SCORE_CHANNELS),
                        list(query_token_hashes),
                        *revision_params,
                    ],
                )
                unit_rows = [
                    {
                        "map_unit_id": str(row[0]),
                        "document_id": str(row[1]),
                        "unit_id": str(row[2]),
                        "section_id": str(row[3] or ""),
                        "path_token_count": int(row[4] or 0),
                        "content_token_count": int(row[5] or 0),
                    }
                    for row in cur.fetchall()
                ]
                _logger.info(
                    "retrieval map-index load stage=units-selective seconds=%.3f rows=%d",
                    time.perf_counter() - stage_started,
                    len(unit_rows),
                )
                all_unit_rows = None
            elif all_unit_rows is None:
                stage_started = time.perf_counter()
                cur.execute(
                    "SELECT units.id, units.document_id, units.unit_id, "
                    "units.section_id, units.path_token_count, units.content_token_count "
                    "FROM document_map_units AS units "
                    f"JOIN (VALUES {values_sql}) AS revisions(document_id, job_result_id) "
                    "ON units.document_id = revisions.document_id "
                    "AND units.job_result_id = revisions.job_result_id "
                    "ORDER BY units.document_id, units.sort_order, units.unit_id",
                    revision_params,
                )
                all_unit_rows = [
                    {
                        "map_unit_id": str(row[0]),
                        "document_id": str(row[1]),
                        "unit_id": str(row[2]),
                        "section_id": str(row[3] or ""),
                        "path_token_count": int(row[4] or 0),
                        "content_token_count": int(row[5] or 0),
                    }
                    for row in cur.fetchall()
                ]
                expected_unit_count = sum(int(row[3] or 0) for row in index_rows)
                if expected_unit_count and len(all_unit_rows) != expected_unit_count:
                    return None
                self._score_unit_rows_cache[revision_key] = all_unit_rows
                _logger.info(
                    "retrieval map-index load stage=units seconds=%.3f rows=%d cache_hit=%s",
                    time.perf_counter() - stage_started,
                    len(all_unit_rows),
                    False,
                )
            else:
                _logger.info(
                    "retrieval map-index load stage=units seconds=0.000 rows=%d cache_hit=%s",
                    len(all_unit_rows),
                    True,
                )

            if not has_complete_section_scope or not query_token_hashes:
                unit_rows = [
                    row
                    for row in all_unit_rows or []
                    if (str(row["document_id"]), str(row["section_id"]))
                    in allowed_pairs_set
                ]
            frequencies: Dict[Tuple[str, str], Dict[str, int]] = {}
            if unit_rows and query_tokens:
                stage_started = time.perf_counter()
                # Keep the episode scope explicit without materializing every
                # token row matching a common query term.  PostgreSQL can choose
                # either the scoped-unit side or the channel/token_hash-leading
                # index, while the scope still limits results to the pinned
                # revision and allowed sections.
                allowed_map_unit_ids = [str(row["map_unit_id"]) for row in unit_rows]
                cur.execute(
                    "WITH scoped_units AS MATERIALIZED ("
                    "SELECT unnest(%s::text[]) AS map_unit_id"
                    ") "
                    "SELECT tokens.map_unit_id, tokens.channel, tokens.token, "
                    "tokens.frequency "
                    "FROM document_map_unit_tokens AS tokens "
                    "JOIN scoped_units "
                    "ON scoped_units.map_unit_id = tokens.map_unit_id "
                    "WHERE tokens.channel = ANY(%s) "
                    "AND tokens.token_hash = ANY(%s)",
                    [
                        allowed_map_unit_ids,
                        list(_MAP_SCORE_CHANNELS),
                        list(query_token_hashes),
                    ],
                )
                for map_unit_id, channel, token, frequency in cur.fetchall():
                    frequencies.setdefault((str(map_unit_id), str(channel)), {})[
                        str(token)
                    ] = int(frequency)
                _logger.info(
                    "retrieval map-index load stage=frequencies seconds=%.3f units=%d tokens=%d",
                    time.perf_counter() - stage_started,
                    len(unit_rows),
                    len(query_tokens),
                )

            _logger.info(
                "retrieval map-index load stage=complete units=%d queries=%d",
                len(unit_rows),
                len(unique_queries),
            )
            path_stats = build_channel_bm25_stats(
                unit_rows=unit_rows,
                map_unit_id_field="map_unit_id",
                length_field="path_token_count",
                channel="path",
                query_tokens=query_tokens,
                frequencies=frequencies,
                average_idf=average_idf_path,
                document_count_override=(
                    sum(int(row[6] or 0) for row in index_rows)
                    if has_complete_section_scope and query_token_hashes
                    else None
                ),
                total_length_override=(
                    sum(int(row[7] or 0) for row in index_rows)
                    if has_complete_section_scope and query_token_hashes
                    else None
                ),
            )
            content_stats = build_channel_bm25_stats(
                unit_rows=unit_rows,
                map_unit_id_field="map_unit_id",
                length_field="content_token_count",
                channel="content",
                query_tokens=query_tokens,
                frequencies=frequencies,
                average_idf=average_idf_content,
                document_count_override=(
                    sum(int(row[8] or 0) for row in index_rows)
                    if has_complete_section_scope and query_token_hashes
                    else None
                ),
                total_length_override=(
                    sum(int(row[9] or 0) for row in index_rows)
                    if has_complete_section_scope and query_token_hashes
                    else None
                ),
            )
            return PersistedScoreCorpus(
                units=[
                    PersistedScoreUnit(
                        unit_id=str(row["unit_id"]),
                        path_length=int(row["path_token_count"]),
                        content_length=int(row["content_token_count"]),
                        path_frequencies=frequencies.get(
                            (str(row["map_unit_id"]), "path"), {}
                        ),
                        content_frequencies=frequencies.get(
                            (str(row["map_unit_id"]), "content"), {}
                        ),
                    )
                    for row in unit_rows
                    # Only units with at least one query-token frequency can
                    # score > 0; zero-frequency units are implicit 0 and are
                    # never materialized for BM25.
                    if frequencies.get((str(row["map_unit_id"]), "path"))
                    or frequencies.get((str(row["map_unit_id"]), "content"))
                ],
                path_stats=path_stats,
                content_stats=content_stats,
            )
        finally:
            cur.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class _SyncCursor(Protocol):
    def execute(self, query: str, params: Sequence[object]) -> None:
        raise NotImplementedError

    def fetchall(self) -> Sequence[Sequence[object]]:
        raise NotImplementedError

    def fetchone(self) -> Optional[Sequence[object]]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _SyncConnection(Protocol):
    def set_session(self, *, readonly: bool, autocommit: bool) -> None:
        raise NotImplementedError

    def cursor(self) -> _SyncCursor:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _unit_from_row(row: Sequence[object]) -> UnitRow:
    return UnitRow(
        chunk_id=str(row[0] or ""),
        section_id=str(row[1]) if row[1] else None,
        chunk_type=str(row[2] or "text"),
        content=str(row[3] or ""),
        sort_order=int(row[4] or 0),
        source_chunk_path=str(row[5] or ""),
        file_path=str(row[6] or ""),
        metadata=_as_meta(row[7]),
    )


class KnowhereProvider:
    """``HierarchyProvider`` over knowhere section/chunk rows."""

    def __init__(
        self,
        *,
        doc_id: str,
        sections: Sequence[SectionRow],
        units: Sequence[UnitRow],
        lazy_loader: Optional[Callable[[str], Sequence[UnitRow]]] = None,
        known_chunk_ids: Optional[Sequence[str]] = None,
    ) -> None:
        self.doc_id = str(doc_id)
        self._lazy_loader = lazy_loader
        self._loaded_sections: Set[str] = set()
        self._sections: Dict[str, SectionRow] = {s.section_id: s for s in sections}
        self._children: Dict[str, List[str]] = {}
        self._roots: List[str] = []
        self._path_to_id: Dict[str, str] = {}
        for row in sorted(sections, key=lambda s: (s.sort_order, s.section_id)):
            parent = row.parent_section_id
            if parent and parent in self._sections:
                self._children.setdefault(parent, []).append(row.section_id)
            else:
                self._roots.append(row.section_id)
            key = normalize_section_path(row.section_path)
            if key:
                self._path_to_id[key] = row.section_id

        self._units_by_section: Dict[str, List[UnitRow]] = {}
        self._chunk_ids: Set[str] = set()
        if known_chunk_ids:
            self._chunk_ids.update(
                str(chunk_id).strip()
                for chunk_id in known_chunk_ids
                if str(chunk_id).strip()
            )
        for unit in sorted(units, key=lambda u: (u.sort_order, u.chunk_id)):
            sid = unit.section_id
            if not sid or sid not in self._sections:
                continue
            self._units_by_section.setdefault(sid, []).append(unit)
            if unit.chunk_id:
                self._chunk_ids.add(unit.chunk_id)
        self._remount_root_assets()

    def _ensure_section_loaded(self, section_id: str) -> None:
        if self._lazy_loader is None or section_id in self._loaded_sections:
            return
        loaded = list(self._lazy_loader(section_id) or ())
        self._loaded_sections.add(section_id)
        if not loaded:
            return
        current = self._units_by_section.setdefault(section_id, [])
        known = {unit.chunk_id for unit in current}
        for unit in loaded:
            if unit.chunk_id and unit.chunk_id not in known:
                current.append(unit)
                known.add(unit.chunk_id)
        current.sort(key=lambda unit: (unit.sort_order, unit.chunk_id))

    def _remount_root_assets(self) -> None:
        """Reattach Root-FK image|table units to host sections via ``connect_to``.

        Aligns with Knowhere ``resolve_root_asset_owners``: assets whose FK still
        points at Root are owned by the text chunk that lists them in
        ``metadata.connect_to``. Unresolved Root assets leave the evidence surface.
        """
        root_sids = [
            sid
            for sid, row in self._sections.items()
            if is_root_section_path(row.section_path)
        ]
        if not root_sids:
            return

        root_assets: Dict[str, UnitRow] = {}
        for sid in root_sids:
            for unit in self._units_by_section.get(sid, ()):
                if unit.chunk_type in _ASSET_TYPES and unit.chunk_id:
                    root_assets[unit.chunk_id] = unit
        if not root_assets:
            return

        owner_by_asset: Dict[str, str] = {}
        for sid, units in self._units_by_section.items():
            row = self._sections.get(sid)
            if row is None or is_root_section_path(row.section_path):
                continue
            for unit in units:
                if unit.chunk_type != "text":
                    continue
                for target in _connect_to_targets(unit.metadata or {}):
                    if target in root_assets and target not in owner_by_asset:
                        owner_by_asset[target] = sid

        touched_owners: Set[str] = set()
        for chunk_id, owner_sid in owner_by_asset.items():
            unit = root_assets[chunk_id]
            remounted = UnitRow(
                chunk_id=unit.chunk_id,
                section_id=owner_sid,
                chunk_type=unit.chunk_type,
                content=unit.content,
                sort_order=unit.sort_order,
                source_chunk_path=unit.source_chunk_path,
                file_path=unit.file_path,
                metadata=dict(unit.metadata or {}),
            )
            self._units_by_section.setdefault(owner_sid, []).append(remounted)
            touched_owners.add(owner_sid)

        for sid in root_sids:
            self._units_by_section[sid] = [
                u
                for u in self._units_by_section.get(sid, ())
                if u.chunk_type not in _ASSET_TYPES
            ]
        for sid in touched_owners:
            self._units_by_section[sid].sort(key=lambda u: (u.sort_order, u.chunk_id))

    def address_level(self, node_id: str) -> Optional[NavLevel]:
        sid = str(node_id or "").strip()
        if not sid:
            return NavLevel.NAMESPACE
        if sid == self.doc_id:
            return NavLevel.DOCUMENT
        if sid in self._sections:
            return NavLevel.SECTION
        if sid in self._chunk_ids:
            return NavLevel.CHUNK
        return None

    def owner_document(self, node_id: str) -> Optional[str]:
        sid = str(node_id or "").strip()
        if not sid:
            return None
        if sid == self.doc_id or sid in self._sections or sid in self._chunk_ids:
            return self.doc_id
        return None

    def roots(self, doc_id: str) -> Sequence[str]:
        return list(self._roots) if str(doc_id) == self.doc_id else []

    def children(self, section_id: str) -> Sequence[str]:
        return list(self._children.get(section_id, ()))

    def node_meta(self, section_id: str) -> NodeMeta:
        row = self._sections.get(section_id)
        if row is None:
            return NodeMeta()
        return NodeMeta(
            title=row.section_title,
            summary=row.summary,
            has_children=bool(self._children.get(section_id)),
        )

    def relations(self, section_id: str) -> Tuple[Set[str], Set[str]]:
        ancestors: Set[str] = set()
        cur = self._sections.get(section_id)
        while cur is not None and cur.parent_section_id:
            parent = cur.parent_section_id
            if parent in ancestors:
                break
            ancestors.add(parent)
            cur = self._sections.get(parent)
        descendants: Set[str] = set()
        stack = list(self.children(section_id))
        while stack:
            cid = stack.pop()
            if cid in descendants:
                continue
            descendants.add(cid)
            stack.extend(self.children(cid))
        return ancestors, descendants

    def content(self, section_id: str) -> str:
        units = self.subtree_units(section_id)
        return "\n".join(self.unit_text(u) for u in units if self.unit_text(u))

    def self_units(self, section_id: str) -> List[UnitRow]:
        self._ensure_section_loaded(section_id)
        return list(self._units_by_section.get(section_id, ()))

    def subtree_units(self, section_id: str) -> List[UnitRow]:
        out = list(self.self_units(section_id))
        for cid in self.relations(section_id)[1]:
            out.extend(self.self_units(cid))
        out.sort(key=lambda u: (u.sort_order, u.chunk_id))
        return out

    def leaf_ids(self, section_id: str) -> List[str]:
        out: List[str] = []

        def rec(sid: str) -> None:
            kids = self.children(sid)
            if not kids:
                out.append(sid)
                return
            for kid in kids:
                rec(kid)

        rec(section_id)
        return out

    def path_titles(self, section_id: str) -> str:
        chain: List[str] = []
        cur = self._sections.get(section_id)
        while cur is not None:
            if cur.section_title:
                chain.append(cur.section_title)
            parent = cur.parent_section_id
            cur = self._sections.get(parent) if parent else None
        return " / ".join(reversed(chain))

    def parent_id(self, section_id: str) -> Optional[str]:
        row = self._sections.get(section_id)
        return row.parent_section_id if row else None

    def section_path(self, section_id: str) -> str:
        row = self._sections.get(section_id)
        return str(row.section_path or "") if row else ""

    def resolve_path(self, path: str) -> Optional[str]:
        """Map a human/gold path to ``section_id`` (``sec_*``)."""
        key = normalize_section_path(path)
        if not key:
            return None
        return self._path_to_id.get(key)

    def unit_text(self, unit: UnitRow) -> str:
        if unit.chunk_type in _ASSET_TYPES:
            return asset_display_text(unit)
        return str(unit.content or "").strip()

    def summaries(self) -> Dict[str, str]:
        return {
            sid: row.summary
            for sid, row in self._sections.items()
            if str(row.summary or "").strip()
        }

    def all_section_ids(self) -> List[str]:
        return list(self._sections)


class LazyKnowhereProvider(KnowhereProvider):
    """Hierarchy provider that loads full chunk rows only on first access."""

    def __init__(
        self,
        *,
        doc_id: str,
        sections: Sequence[SectionRow],
        chunk_store: ChunkStore,
        known_chunk_ids: Sequence[str],
        root_asset_ids: Sequence[str] = (),
        remounted_assets_by_section: Optional[Dict[str, Sequence[str]]] = None,
    ) -> None:
        super().__init__(
            doc_id=doc_id,
            sections=sections,
            units=(),
            lazy_loader=lambda section_id: chunk_store.load_section_units(
                doc_id,
                section_id,
                (remounted_assets_by_section or {}).get(section_id, ()),
            ),
            known_chunk_ids=known_chunk_ids,
        )
        self._chunk_store = chunk_store
        self._root_asset_ids = {str(chunk_id) for chunk_id in root_asset_ids}

    def _ensure_section_loaded(self, section_id: str) -> None:
        super()._ensure_section_loaded(section_id)
        if (
            section_id in self._units_by_section
            and self._root_asset_ids
            and is_root_section_path(self.section_path(section_id))
        ):
            self._units_by_section[section_id] = [
                unit
                for unit in self._units_by_section[section_id]
                if unit.chunk_id not in self._root_asset_ids
            ]

    def close(self) -> None:
        self._chunk_store.close()


def _connect(dsn: str) -> _SyncConnection:
    import psycopg2

    return psycopg2.connect(dsn)


def _as_meta(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        import json

        try:
            obj = json.loads(raw)
        except Exception:
            return {}
        return dict(obj) if isinstance(obj, dict) else {}
    return {}


def load_document_from_db(
    document_id: str,
    *,
    dsn: Optional[str] = None,
) -> KnowhereProvider:
    """Load one document's current revision into a ``KnowhereProvider``."""
    doc_id = str(document_id or "").strip()
    if not doc_id:
        raise ValueError("document_id is required")
    url = dsn or knowhere_database_url()
    conn = _connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_id, current_job_result_id, source_file_name "
                "FROM documents WHERE document_id = %s AND status = 'active'",
                (doc_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"active document not found: {doc_id}")
            job_result_id = row[1]
            if not job_result_id:
                raise ValueError(f"document {doc_id} has no current_job_result_id")

            cur.execute(
                "SELECT section_id, parent_section_id, section_path, section_title, "
                "section_level, summary, sort_order "
                "FROM document_sections "
                "WHERE document_id = %s AND job_result_id = %s "
                "ORDER BY sort_order, section_id",
                (doc_id, job_result_id),
            )
            sections = [
                SectionRow(
                    section_id=str(r[0]),
                    parent_section_id=str(r[1]) if r[1] else None,
                    section_path=str(r[2] or ""),
                    section_title=str(r[3] or "").strip(),
                    section_level=int(r[4] or 0),
                    summary=str(r[5] or "").strip(),
                    sort_order=int(r[6] or 0),
                )
                for r in cur.fetchall()
            ]

            cur.execute(
                "SELECT chunk_id, section_id, chunk_type, content, sort_order, "
                "source_chunk_path, file_path, chunk_metadata "
                "FROM document_chunks "
                "WHERE document_id = %s AND job_result_id = %s "
                "ORDER BY sort_order, chunk_id",
                (doc_id, job_result_id),
            )
            units = [
                UnitRow(
                    chunk_id=str(r[0] or ""),
                    section_id=str(r[1]) if r[1] else None,
                    chunk_type=str(r[2] or "text"),
                    content=str(r[3] or ""),
                    sort_order=int(r[4] or 0),
                    source_chunk_path=str(r[5] or ""),
                    file_path=str(r[6] or ""),
                    metadata=_as_meta(r[7]),
                )
                for r in cur.fetchall()
            ]
    finally:
        conn.close()
    return KnowhereProvider(doc_id=doc_id, sections=sections, units=units)


def load_namespace_from_db(
    *,
    namespace: str,
    document_ids: Optional[Sequence[str]] = None,
    titles: Optional[Dict[str, str]] = None,
    dsn: Optional[str] = None,
) -> NamespaceKnowhereProvider:
    """Load active documents in a namespace (or an explicit id list)."""
    ns = str(namespace or "").strip()
    url = dsn or knowhere_database_url()
    wanted = [str(d).strip() for d in (document_ids or ()) if str(d).strip()]
    if not wanted:
        if not ns:
            raise ValueError("namespace or document_ids is required")
        conn = _connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT document_id, source_file_name FROM documents "
                    "WHERE namespace = %s AND status = 'active' "
                    "ORDER BY document_id",
                    (ns,),
                )
                rows = cur.fetchall()
                wanted = [str(r[0]) for r in rows]
                auto_titles = {
                    str(r[0]): str(r[1] or r[0]).strip() for r in rows if r[0]
                }
        finally:
            conn.close()
    else:
        auto_titles = {}

    if not wanted:
        raise ValueError(f"no active documents for namespace={ns!r}")

    providers = [load_document_from_db(did, dsn=url) for did in wanted]
    merged_titles = dict(auto_titles)
    if titles:
        merged_titles.update(
            {str(k): str(v) for k, v in titles.items() if str(k).strip()}
        )
    return NamespaceKnowhereProvider(providers, titles=merged_titles or None)


class NamespaceKnowhereProvider:
    """Multi-document provider: document ids are DISPATCH-only map nodes.

    Namespace root is empty scope (not a node). ``roots("")`` returns the
    document ids; ``children(document_id)`` returns that document's section
    roots. Section/chunk identity and ownership stay on the real keys.
    """

    def __init__(
        self,
        providers: Sequence[KnowhereProvider],
        *,
        titles: Optional[Dict[str, str]] = None,
        chunk_owner_by_id: Optional[Dict[str, str]] = None,
    ) -> None:
        self._docs: Dict[str, KnowhereProvider] = {
            p.doc_id: p for p in providers if p.doc_id
        }
        if not self._docs:
            raise ValueError("NamespaceKnowhereProvider requires at least one document")
        self._titles = {
            did: str((titles or {}).get(did) or did).strip() or did
            for did in self._docs
        }
        self._section_owner: Dict[str, str] = {}
        self._chunk_owner: Dict[str, str] = {}
        for doc_id, provider in self._docs.items():
            for sid in provider.all_section_ids():
                self._section_owner[sid] = doc_id
        if chunk_owner_by_id:
            self._chunk_owner.update(
                {
                    str(chunk_id): str(doc_id)
                    for chunk_id, doc_id in chunk_owner_by_id.items()
                    if str(chunk_id).strip() and str(doc_id).strip()
                }
            )
        else:
            for doc_id, provider in self._docs.items():
                for sid in provider.all_section_ids():
                    for unit in provider.self_units(sid):
                        if unit.chunk_id:
                            self._chunk_owner[unit.chunk_id] = doc_id

    def document_ids(self) -> List[str]:
        return list(self._docs)

    def close(self) -> None:
        for provider in self._docs.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def load_persisted_score_corpus(
        self,
        doc_ids: Sequence[str],
        queries: Sequence[str],
    ) -> Optional[PersistedScoreCorpus]:
        """Return the index projection only when all documents share one store."""
        providers = [
            self._docs[document_id]
            for raw_document_id in doc_ids
            if (document_id := str(raw_document_id).strip()) in self._docs
        ]
        if len(providers) != len(doc_ids) or not all(
            isinstance(provider, LazyKnowhereProvider) for provider in providers
        ):
            return None
        lazy_providers = [
            provider
            for provider in providers
            if isinstance(provider, LazyKnowhereProvider)
        ]
        stores = {
            id(provider._chunk_store): provider._chunk_store
            for provider in lazy_providers
        }
        if len(stores) != 1:
            return None
        store = next(iter(stores.values()))
        loader = getattr(store, "load_persisted_score_corpus", None)
        if not callable(loader):
            return None
        return loader(
            [provider.doc_id for provider in lazy_providers],
            {provider.doc_id: list(provider._sections) for provider in lazy_providers},
            queries,
        )

    def address_level(self, node_id: str) -> Optional[NavLevel]:
        sid = str(node_id or "").strip()
        if not sid:
            return NavLevel.NAMESPACE
        if sid in self._docs:
            return NavLevel.DOCUMENT
        if sid in self._section_owner:
            return NavLevel.SECTION
        if sid in self._chunk_owner:
            return NavLevel.CHUNK
        return None

    def owner_document(self, node_id: str) -> Optional[str]:
        sid = str(node_id or "").strip()
        if not sid:
            return None
        if sid in self._docs:
            return sid
        return self._section_owner.get(sid) or self._chunk_owner.get(sid)

    def roots(self, doc_id: str) -> Sequence[str]:
        key = str(doc_id or "").strip()
        if not key:
            return list(self._docs)
        provider = self._docs.get(key)
        return list(provider.roots(key)) if provider else []

    def children(self, section_id: str) -> Sequence[str]:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            return list(self._docs[sid].roots(sid))
        owner = self._section_owner.get(sid)
        if not owner:
            return []
        return list(self._docs[owner].children(sid))

    def node_meta(self, section_id: str) -> NodeMeta:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            provider = self._docs[sid]
            return NodeMeta(
                title=self._titles.get(sid, sid),
                summary="",
                has_children=bool(provider.roots(sid)),
            )
        owner = self._section_owner.get(sid)
        if not owner:
            return NodeMeta()
        return self._docs[owner].node_meta(sid)

    def relations(self, section_id: str) -> Tuple[Set[str], Set[str]]:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            descendants = set(self._docs[sid].all_section_ids())
            return set(), descendants
        owner = self._section_owner.get(sid)
        if not owner:
            return set(), set()
        ancestors, descendants = self._docs[owner].relations(sid)
        row_parent = self._docs[owner].parent_id(sid)
        if row_parent is None:
            ancestors = set(ancestors) | {owner}
        return ancestors, descendants

    def content(self, section_id: str) -> str:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            provider = self._docs[sid]
            return "\n".join(
                provider.content(root)
                for root in provider.roots(sid)
                if provider.content(root)
            )
        owner = self._section_owner.get(sid)
        if not owner:
            return ""
        return self._docs[owner].content(sid)

    def self_units(self, section_id: str) -> List[UnitRow]:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            return []
        owner = self._section_owner.get(sid)
        if not owner:
            return []
        return self._docs[owner].self_units(sid)

    def leaf_ids(self, section_id: str) -> List[str]:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            out: List[str] = []
            for root in self._docs[sid].roots(sid):
                out.extend(self._docs[sid].leaf_ids(root))
            return out
        owner = self._section_owner.get(sid)
        if not owner:
            return []
        return self._docs[owner].leaf_ids(sid)

    def path_titles(self, section_id: str) -> str:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            return self._titles.get(sid, sid)
        owner = self._section_owner.get(sid)
        if not owner:
            return ""
        title = self._docs[owner].path_titles(sid)
        doc_title = self._titles.get(owner, owner)
        return f"{doc_title} / {title}" if title else doc_title

    def parent_id(self, section_id: str) -> Optional[str]:
        sid = str(section_id or "").strip()
        if sid in self._docs:
            return None
        owner = self._section_owner.get(sid)
        if not owner:
            return None
        parent = self._docs[owner].parent_id(sid)
        return parent if parent is not None else owner

    def section_path(self, section_id: str) -> str:
        sid = str(section_id or "").strip()
        owner = self._section_owner.get(sid)
        if not owner:
            return ""
        return self._docs[owner].section_path(sid)

    def resolve_path(self, path: str, doc_id: str = "") -> Optional[str]:
        did = str(doc_id or "").strip()
        if did and did in self._docs:
            return self._docs[did].resolve_path(path)
        for provider in self._docs.values():
            hit = provider.resolve_path(path)
            if hit:
                return hit
        return None

    def unit_text(self, unit: UnitRow) -> str:
        owner = self._chunk_owner.get(unit.chunk_id) or self._section_owner.get(
            str(unit.section_id or "")
        )
        if not owner:
            return ""
        return self._docs[owner].unit_text(unit)

    def summaries(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for provider in self._docs.values():
            out.update(provider.summaries())
        return out

    def all_section_ids(self) -> List[str]:
        out: List[str] = []
        for provider in self._docs.values():
            out.extend(provider.all_section_ids())
        return out
