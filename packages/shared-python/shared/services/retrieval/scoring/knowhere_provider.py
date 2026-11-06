"""Publication-time hierarchy provider over section/chunk rows.

Document publication and classic recall use this to build map-unit indexes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable, Iterator
from heapq import merge
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
)

from shared.services.retrieval.scoring.hierarchy import NodeMeta


_ASSET_TYPES = ("table", "image")
# Body chunk types that can own a Root-parked asset via connect_to. Both
# chunk-track ("text") and page-track ("page") body chunks can embed assets.
_BODY_CHUNK_TYPES = ("text", "page")
# Knowhere sentinel path for the virtual document container (not a collectable leaf).
ROOT_SECTION_PATH = "Root"
_Row = TypeVar("_Row")


def _iterateSortedRows(rows: Sequence[_Row], key: Callable[[_Row], tuple[int, str]]) -> Iterator[_Row | None]:
    # Stable bounded runs avoid one document-sized synchronous Timsort. Merge
    # preserves the same equal-key source order as the original stable sort.
    runs: List[List[_Row]] = []
    for offset in range(0, len(rows), 128):
        yield None
        runs.append(sorted(rows[offset:offset + 128], key=key))
    yield from merge(*runs, key=key)


def _sortRows(rows: Sequence[_Row], key: Callable[[_Row], tuple[int, str]],
              checkpoint: Callable[[], None] | None) -> List[_Row]:
    if checkpoint is None:
        return sorted(rows, key=key)
    ordered: List[_Row] = []
    for row in _iterateSortedRows(rows, key):
        checkpoint()
        if row is not None:
            ordered.append(row)
    return ordered


async def _sortRowsAsync(rows: Sequence[_Row], key: Callable[[_Row], tuple[int, str]],
                         checkpoint: Callable[[], Awaitable[None]]) -> List[_Row]:
    ordered: List[_Row] = []
    for row in _iterateSortedRows(rows, key):
        await checkpoint()
        if row is not None:
            ordered.append(row)
    return ordered


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


def _connect_to_targets(metadata: Dict[str, Any]) -> Iterator[str]:
    """``chunk_metadata.connect_to[].target`` ids (document order, first wins upstream)."""
    raw = metadata.get("connect_to") if isinstance(metadata, dict) else None
    if not isinstance(raw, list):
        return
    for conn in raw:
        if not isinstance(conn, dict):
            continue
        target = str(conn.get("target") or "").strip()
        if target:
            yield target


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
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        for _ in self._initializeRows(doc_id=doc_id, sections=sections,
            orderedSections=_sortRows(sections, lambda row: (row.sort_order, row.section_id), checkpoint),
            orderedUnits=_sortRows(units, lambda row: (row.sort_order, row.chunk_id), checkpoint),
            lazy_loader=lazy_loader, known_chunk_ids=known_chunk_ids):
            if checkpoint is not None:
                checkpoint()
        self._remount_root_assets(checkpoint=checkpoint)

    @classmethod
    async def createAsync(cls, *, doc_id: str, sections: Sequence[SectionRow],
                          units: Sequence[UnitRow], checkpoint: Callable[[], Awaitable[None]]) -> "KnowhereProvider":
        provider: KnowhereProvider = cls(doc_id=doc_id, sections=(), units=())
        orderedSections: List[SectionRow] = await _sortRowsAsync(sections,
            lambda row: (row.sort_order, row.section_id), checkpoint)
        orderedUnits: List[UnitRow] = await _sortRowsAsync(units,
            lambda row: (row.sort_order, row.chunk_id), checkpoint)
        for _ in provider._initializeRows(doc_id=doc_id, sections=sections,
            orderedSections=orderedSections, orderedUnits=orderedUnits):
            await checkpoint()
        for owner in provider._remountRootAssetSteps():
            if owner is not None:
                provider._units_by_section[owner] = await _sortRowsAsync(provider._units_by_section[owner],
                    lambda row: (row.sort_order, row.chunk_id), checkpoint)
            await checkpoint()
        return provider

    def _initializeRows(self, *, doc_id: str, sections: Sequence[SectionRow],
                        orderedSections: Sequence[SectionRow], orderedUnits: Sequence[UnitRow],
                        lazy_loader: Optional[Callable[[str], Sequence[UnitRow]]] = None,
                        known_chunk_ids: Optional[Sequence[str]] = None) -> Iterator[None]:
        self.doc_id = str(doc_id)
        self._lazy_loader = lazy_loader
        self._loaded_sections: Set[str] = set()
        self._sections: Dict[str, SectionRow] = {}
        for section in sections:
            self._sections[section.section_id] = section
            yield
        self._children: Dict[str, List[str]] = {}
        self._roots: List[str] = []
        self._path_to_id: Dict[str, str] = {}
        self._title_paths: Dict[str, str] = {}
        for row in orderedSections:
            parent = row.parent_section_id
            if parent and parent in self._sections:
                self._children.setdefault(parent, []).append(row.section_id)
            else:
                self._roots.append(row.section_id)
            key = normalize_section_path(row.section_path)
            if key:
                self._path_to_id[key] = row.section_id
            yield

        self._units_by_section: Dict[str, List[UnitRow]] = {}
        self._chunk_ids: Set[str] = set()
        if known_chunk_ids:
            for chunkId in known_chunk_ids:
                if str(chunkId).strip():
                    self._chunk_ids.add(str(chunkId).strip())
                yield
        for unit in orderedUnits:
            sid = unit.section_id
            if not sid or sid not in self._sections:
                yield
                continue
            self._units_by_section.setdefault(sid, []).append(unit)
            if unit.chunk_id:
                self._chunk_ids.add(unit.chunk_id)
            yield

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

    def _remount_root_assets(self, *, checkpoint: Callable[[], None] | None = None) -> None:
        for owner in self._remountRootAssetSteps():
            if owner is not None:
                self._units_by_section[owner] = _sortRows(self._units_by_section[owner],
                    lambda unit: (unit.sort_order, unit.chunk_id), checkpoint)
            if checkpoint is not None:
                checkpoint()

    def _remountRootAssetSteps(self) -> Iterator[str | None]:
        """Reattach Root-FK image|table units to host sections via ``connect_to``.

        Aligns with Knowhere ``resolve_root_asset_owners``: assets whose FK still
        points at Root are owned by the text chunk that lists them in
        ``metadata.connect_to``. Unresolved Root assets leave the evidence surface.
        """
        root_sids: List[str] = []
        for sid, row in self._sections.items():
            if is_root_section_path(row.section_path):
                root_sids.append(sid)
            yield None
        if not root_sids:
            return

        root_assets: Dict[str, UnitRow] = {}
        for sid in root_sids:
            for unit in self._units_by_section.get(sid, ()):
                if unit.chunk_type in _ASSET_TYPES and unit.chunk_id:
                    root_assets[unit.chunk_id] = unit
                yield None
        if not root_assets:
            return

        owner_by_asset: Dict[str, str] = {}
        for sid, units in self._units_by_section.items():
            row = self._sections.get(sid)
            if row is None or is_root_section_path(row.section_path):
                yield None
                continue
            for unit in units:
                if unit.chunk_type not in _BODY_CHUNK_TYPES:
                    yield None
                    continue
                for target in _connect_to_targets(unit.metadata or {}):
                    if target in root_assets and target not in owner_by_asset:
                        owner_by_asset[target] = sid
                    yield None
                yield None

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
            yield None

        for sid in root_sids:
            retained: List[UnitRow] = []
            for unit in self._units_by_section.get(sid, ()):
                if unit.chunk_type not in _ASSET_TYPES:
                    retained.append(unit)
                yield None
            self._units_by_section[sid] = retained
        for sid in touched_owners:
            yield sid

    def owner_document(self, node_id: str) -> Optional[str]:
        sid = str(node_id or "").strip()
        if not sid:
            return None
        if sid == self.doc_id or sid in self._sections or sid in self._chunk_ids:
            return self.doc_id
        return None

    def roots(self, doc_id: str) -> Sequence[str]:
        return list(self._roots) if str(doc_id) == self.doc_id else []

    def iterateRoots(self, doc_id: str) -> Iterator[str]:
        return iter(self._roots) if str(doc_id) == self.doc_id else iter(())

    def children(self, section_id: str) -> Sequence[str]:
        return list(self._children.get(section_id, ()))

    def iterateChildren(self, section_id: str) -> Iterator[str]:
        return iter(self._children.get(section_id, ()))

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

    def iterateSelfUnits(self, section_id: str) -> Iterator[UnitRow]:
        self._ensure_section_loaded(section_id)
        return iter(self._units_by_section.get(section_id, ()))

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
        cached: str | None = self._title_paths.get(section_id)
        if cached is not None:
            return cached
        chain: List[SectionRow] = []
        seen: Set[str] = set()
        cur = self._sections.get(section_id)
        while cur is not None and cur.section_id not in self._title_paths:
            if cur.section_id in seen:
                raise ValueError("Hierarchy path contains a parent cycle")
            seen.add(cur.section_id)
            chain.append(cur)
            parent = cur.parent_section_id
            cur = self._sections.get(parent) if parent else None
        path: str = self._title_paths[cur.section_id] if cur is not None else ""
        for row in reversed(chain):
            if row.section_title:
                path = f"{path} / {row.section_title}" if path else row.section_title
            self._title_paths[row.section_id] = path
        return self._title_paths.get(section_id, "")

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

