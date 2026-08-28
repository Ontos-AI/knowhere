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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple

from .nav_address import NavLevel
from .nav_hierarchy import NodeMeta

_ASSET_TYPES = ("table", "image")
# Knowhere sentinel path for the virtual document container (not a collectable leaf).
ROOT_SECTION_PATH = "Root"
_DEFAULT_DSN = "postgresql://root:root123@127.0.0.1:5433/Knowhere"


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
    def load_document_units(
        self,
        document_id: str,
        section_ids: Sequence[str],
        extra_chunk_ids_by_section: Optional[Dict[str, Sequence[str]]] = None,
    ) -> List[UnitRow]:
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
        if not doc_id or not sid or not job_result_id or (doc_id, sid) in self._excluded_sections:
            return []
        cur = self._connection().cursor()
        try:
            ids = [str(chunk_id).strip() for chunk_id in extra_chunk_ids if str(chunk_id).strip()]
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

    def load_document_units(
        self,
        document_id: str,
        section_ids: Sequence[str],
        extra_chunk_ids_by_section: Optional[Dict[str, Sequence[str]]] = None,
    ) -> List[UnitRow]:
        """Load one document's section payloads in a single ordered query."""
        doc_id = str(document_id).strip()
        job_result_id = self._revisions.get(doc_id)
        section_values = [str(section_id).strip() for section_id in section_ids if str(section_id).strip()]
        extra_ids = [
            str(chunk_id).strip()
            for chunk_ids in (extra_chunk_ids_by_section or {}).values()
            for chunk_id in chunk_ids
            if str(chunk_id).strip()
        ]
        if not doc_id or not job_result_id or (not section_values and not extra_ids):
            return []

        predicates: list[str] = []
        params: list[object] = [doc_id, job_result_id]
        if section_values:
            predicates.append("section_id = ANY(%s)")
            params.append(section_values)
        if extra_ids:
            predicates.append("chunk_id = ANY(%s)")
            params.append(extra_ids)

        cur = self._connection().cursor()
        try:
            cur.execute(
                "SELECT chunk_id, section_id, chunk_type, content, sort_order, "
                "source_chunk_path, file_path, chunk_metadata "
                "FROM document_chunks "
                "WHERE document_id = %s AND job_result_id = %s AND ("
                + " OR ".join(predicates)
                + ") ORDER BY section_id, sort_order, chunk_id, id",
                params,
            )
            units: list[UnitRow] = []
            for row in cur.fetchall():
                section_id = str(row[1]) if row[1] else None
                if section_id and (doc_id, section_id) in self._excluded_sections:
                    continue
                units.append(_unit_from_row(row))
            return units
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
            n_chunks=len(self.subtree_units(section_id)),
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

    def release_section_units(self, section_id: str) -> None:
        """Drop one section's loaded payload while keeping its structure."""
        if self._lazy_loader is None:
            return
        sid = str(section_id or "").strip()
        if not sid:
            return
        self._units_by_section.pop(sid, None)
        self._loaded_sections.discard(sid)

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

    def chunk_count(self) -> int:
        return len(self._chunk_ids)


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
        self._remounted_assets_by_section = {
            str(section_id): [str(chunk_id) for chunk_id in chunk_ids]
            for section_id, chunk_ids in (remounted_assets_by_section or {}).items()
        }

    def prefetch_document_units(self) -> None:
        """Load this document's section payloads with one bounded SQL query."""
        section_ids = list(self._sections)
        loaded = self._chunk_store.load_document_units(
            self.doc_id,
            section_ids,
            self._remounted_assets_by_section,
        )
        by_section: Dict[str, List[UnitRow]] = {}
        for unit in loaded:
            sid = str(unit.section_id or "").strip()
            if sid and sid in self._sections:
                by_section.setdefault(sid, []).append(unit)

        units_by_id = {unit.chunk_id: unit for unit in loaded if unit.chunk_id}
        for section_id, asset_ids in self._remounted_assets_by_section.items():
            target = by_section.setdefault(section_id, [])
            known = {unit.chunk_id for unit in target}
            for asset_id in asset_ids:
                asset = units_by_id.get(asset_id)
                if asset is not None and asset.chunk_id not in known:
                    target.append(asset)
                    known.add(asset.chunk_id)

        for section_id in section_ids:
            units = by_section.get(section_id, [])
            units.sort(key=lambda unit: (unit.sort_order, unit.chunk_id))
            self._units_by_section[section_id] = units
            self._loaded_sections.add(section_id)

        for section_id in section_ids:
            if is_root_section_path(self.section_path(section_id)):
                self._units_by_section[section_id] = [
                    unit
                    for unit in self._units_by_section.get(section_id, ())
                    if unit.chunk_id not in self._root_asset_ids
                ]

    def release_document_units(self) -> None:
        """Release all payloads loaded by the document batch."""
        self._units_by_section.clear()
        self._loaded_sections.clear()

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

    def release_loaded_units(self) -> None:
        self._units_by_section.clear()
        self._loaded_sections.clear()


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
        merged_titles.update({str(k): str(v) for k, v in titles.items() if str(k).strip()})
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

    def release_loaded_units(self) -> None:
        for provider in self._docs.values():
            release = getattr(provider, "release_loaded_units", None)
            if callable(release):
                release()

    def release_section_units(self, section_id: str) -> None:
        owner = self._section_owner.get(str(section_id or "").strip())
        if not owner:
            return
        release = getattr(self._docs[owner], "release_section_units", None)
        if callable(release):
            release(section_id)

    def prefetch_document_units(self, doc_id: str) -> None:
        provider = self._docs.get(str(doc_id).strip())
        prefetch = getattr(provider, "prefetch_document_units", None)
        if callable(prefetch):
            prefetch()

    def release_document_units(self, doc_id: str) -> None:
        provider = self._docs.get(str(doc_id).strip())
        release = getattr(provider, "release_document_units", None)
        if callable(release):
            release()

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
            count_fn = getattr(provider, "chunk_count", None)
            n_chunks = int(count_fn()) if callable(count_fn) else sum(
                len(provider.self_units(sec)) for sec in provider.all_section_ids()
            )
            return NodeMeta(
                title=self._titles.get(sid, sid),
                summary="",
                has_children=bool(provider.roots(sid)),
                n_chunks=n_chunks,
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
                provider.content(root) for root in provider.roots(sid) if provider.content(root)
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
