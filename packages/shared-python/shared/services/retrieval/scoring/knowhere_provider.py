"""Publication-time hierarchy provider over section/chunk rows.

Extracted from the map-nav package so document publication and classic
recall can build map-unit indexes without the episode kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from shared.services.retrieval.scoring.hierarchy import NodeMeta


_ASSET_TYPES = ("table", "image")
# Body chunk types that can own a Root-parked asset via connect_to. Both
# chunk-track ("text") and page-track ("page") body chunks can embed assets.
_BODY_CHUNK_TYPES = ("text", "page")
# Knowhere sentinel path for the virtual document container (not a collectable leaf).
ROOT_SECTION_PATH = "Root"


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
                if unit.chunk_type not in _BODY_CHUNK_TYPES:
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

