"""Portable kernel seam: HierarchyProvider Protocol + ToolSpace-shaped adapter.

``docs/audit_plan_nav_overlap.md`` (§ToolSpace surface) shows every call site
under ``src/nav`` touches at most 5 hierarchy operations: a document's
top-level sections, a node's children, a node's structural metadata
(title/summary/chunk count), a node's ancestor/descendant ids, and a node's
full-subtree text as one evidence unit. Everything else this codebase's
``ToolSpace`` exposes (BM25/dense scoring, ``_idx``, ``corpus_doc_ids``) is
optional — every caller already reaches it through
``getattr(ts, "...", None)`` / ``callable(...)`` guards, so omitting it only
degrades ranking quality, never breaks the pipeline.

``HierarchyProvider`` names that 5-method minimum explicitly.
``ProviderToolSpace`` adapts any implementation of it to the ToolSpace-shaped
duck type every existing ``src/nav`` module already calls, so a knowhere-main
port only has to implement ``HierarchyProvider`` once — no other file in
``src/nav`` needs to change. See ``tests/test_nav_hierarchy_adapter.py`` for
the acceptance test: a pure in-memory provider (no scoring, no ToolSpace)
driving the full plan -> harvest -> plan_control -> settle pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    TYPE_CHECKING,
    cast,
    runtime_checkable,
)

from shared.services.retrieval.hydration.asset_inline import (
    inline_assets_at_placeholders,
)

if TYPE_CHECKING:
    from shared.services.retrieval.scoring.knowhere_hybrid import PersistedScoreCorpus


@dataclass
class Chunk:
    node_id: str
    doc_id: str
    text: str
    line_ids: Tuple[int, ...]
    section_id: Optional[str] = None
    text_line_id_groups: Optional[Tuple[Tuple[int, ...], ...]] = None


@dataclass
class NodeMeta:
    title: str = ""
    summary: str = ""
    has_children: bool = False


@runtime_checkable
class HierarchyProvider(Protocol):
    """The 5 capabilities every src/nav module needs, and nothing else."""

    def roots(self, doc_id: str) -> Sequence[str]:
        """Top-level section ids for a document (or corpus root)."""
        ...

    def children(self, section_id: str) -> Sequence[str]:
        """Direct child section ids, in document order."""
        ...

    def node_meta(self, section_id: str) -> NodeMeta:
        """Title/summary/has_children for one node."""
        ...

    def relations(self, section_id: str) -> Tuple[Set[str], Set[str]]:
        """(ancestor_ids, descendant_ids); section_id itself excluded from both."""
        ...

    def content(self, section_id: str) -> str:
        """Full text for this node's subtree, as one evidence unit."""
        ...


class ProviderToolSpace:
    """Adapts a ``HierarchyProvider`` to the ToolSpace duck type nav modules use.

    Deliberately does not implement ``_idx`` / ``corpus_doc_ids`` — the parts of
    the optional surface that assume this repo's line-indexed bundles.
    Grouping/title lookups in ``nav_compose`` and the non-map-mode collect
    fallback in ``nav_agent`` already fall back to id-derived defaults when
    ``_idx`` is absent.

    Five capabilities beyond the required 5 are forwarded when the provider
    offers them, which is what turns subtree-blob evidence into the
    chunk-granularity units map scoring needs (plus direct-parent lookup for
    ``widen``):

    ``self_units(section_id)``
        units attached to this node itself, in document order
    ``leaf_ids(section_id)``
        descendant leaf ids in document order
    ``unit_text(unit)`` / ``path_titles(section_id)``
        an evidence unit's body text, and a node's root-first title chain
    ``parent_id(section_id)``
        direct parent section id (provider capability; unused by checklist widen)

    Without them the adapter keeps its original behaviour: one blob per
    subtree, no path channel, no parent-based widen (harvest falls back to
    the document root instead of a parent scope).
    """

    def __init__(self, provider: HierarchyProvider) -> None:
        self._provider = provider

    def owner_document(self, node_id: str) -> Optional[str]:
        fn = getattr(self._provider, "owner_document", None)
        if not callable(fn):
            return None
        got = fn(node_id)
        return str(got) if got else None

    def document_ids(self) -> List[str]:
        """Forward only when the provider is namespace-mode (has ``document_ids``)."""
        fn = getattr(self._provider, "document_ids", None)
        if not callable(fn):
            return []
        return [str(x) for x in cast(Sequence[Any], fn() or ()) if str(x).strip()]

    def sections_for_doc(self, doc_id: str) -> List[str]:
        return [str(s) for s in self._provider.roots(doc_id)]

    def get_structure(self, section_id: str) -> dict:
        meta = self._provider.node_meta(section_id)
        child_ids = [str(c) for c in self._provider.children(section_id)]
        return {
            "section_id": section_id,
            "level": 0,
            "preview": meta.title,
            "summary": str(meta.summary or ""),
            "n_lines": 1,
            "children": [
                {"section_id": cid, "preview": self._provider.node_meta(cid).title}
                for cid in child_ids
            ],
            "exists": True,
        }

    def _children_for_section_path(
        self, section_id: str, doc_id: str
    ) -> List[dict]:
        child_ids = [str(c) for c in self._provider.children(section_id)]
        # ``node_meta`` may materialize a lazy subtree to calculate chunk
        # counts. Tree traversal needs only the child id/title; avoid an N+1
        # payload load while building the scoring tree.
        out: List[dict] = []
        for cid in child_ids:
            path = self.path_titles(cid, doc_id)
            out.append(
                {
                    "section_id": cid,
                    "preview": path.rsplit(" / ", 1)[-1] if path else "",
                }
            )
        return out

    def section_relation_ids(
        self, section_id: str, doc_id: str
    ) -> Tuple[Set[str], Set[str]]:
        del doc_id
        return self._provider.relations(section_id)

    def path_titles(self, section_id: str, doc_id: str) -> str:
        del doc_id
        fn = getattr(self._provider, "path_titles", None)
        return str(fn(section_id) or "") if callable(fn) else ""

    def parent_id(self, section_id: str) -> Optional[str]:
        fn = getattr(self._provider, "parent_id", None)
        if not callable(fn):
            return None
        parent = fn(section_id)
        return str(parent) if parent else None

    def _node_unit_span(self, section_id: str) -> Tuple[str, int, int]:
        """(joined text, first sort_order, unit count) for one node's own units.

        Evidence display only: text units insert connected assets at placeholders.
        Scoring still uses ``materialize_self_only_chunks`` / raw ``unit_text``.
        """
        self_units = getattr(self._provider, "self_units", None)
        unit_text = getattr(self._provider, "unit_text", None)
        if not callable(self_units) or not callable(unit_text):
            return "", 0, 0
        units = list(cast(Sequence[Any], self_units(section_id) or ()))
        if not units:
            return "", 0, 0
        first_order = int(getattr(units[0], "sort_order", 0) or 0)

        asset_types = {"image", "table"}
        text_units: List[Any] = []
        asset_by_id: Dict[str, str] = {}
        for unit in units:
            chunk_type = str(getattr(unit, "chunk_type", "") or "").strip().lower()
            chunk_id = str(getattr(unit, "chunk_id", "") or "").strip()
            body = str(unit_text(unit) or "").strip()
            if chunk_type in asset_types:
                if chunk_id and body:
                    asset_by_id[chunk_id] = body
                continue
            text_units.append(unit)

        if not text_units:
            texts = [body for body in asset_by_id.values() if body]
            return "\n".join(texts), first_order, len(units)

        parts: List[str] = []
        used_assets: Set[str] = set()
        for unit in text_units:
            content = str(unit_text(unit) or "").strip()
            meta = getattr(unit, "metadata", None) or {}
            connections = (
                meta.get("connect_to") if isinstance(meta, dict) else None
            ) or []
            if not isinstance(connections, list):
                connections = []
            wanted = {
                str(item.get("target") or "").strip()
                for item in connections
                if isinstance(item, dict)
            }
            display = {
                target_id: asset_by_id[target_id]
                for target_id in wanted
                if target_id in asset_by_id
            }
            content, embedded = inline_assets_at_placeholders(
                content,
                connections=connections,
                display_by_target=display,
            )
            used_assets.update(embedded)
            if content:
                parts.append(content)

        for target_id, body in asset_by_id.items():
            if target_id in used_assets or not body:
                continue
            parts.append(body)

        return "\n".join(parts), first_order, len(units)

    def _make_chunk(
        self, node_id: str, doc_id: str, text: str, order: int, section_id: str
    ) -> Any:
        return Chunk(
            node_id=node_id,
            doc_id=doc_id,
            text=text,
            line_ids=(int(order),),
            section_id=section_id,
        )

    def materialize_self_only_chunks(self, section_id: str, doc_id: str) -> List[Any]:
        self_units = getattr(self._provider, "self_units", None)
        unit_text = getattr(self._provider, "unit_text", None)
        if not callable(self_units) or not callable(unit_text):
            return []
        out: List[Any] = []
        for unit in cast(Sequence[Any], self_units(section_id) or ()):
            text = str(unit_text(unit) or "").strip()
            if not text:
                continue
            out.append(
                self._make_chunk(
                    str(getattr(unit, "chunk_id", "") or section_id),
                    doc_id,
                    text,
                    int(getattr(unit, "sort_order", 0) or 0),
                    section_id,
                )
            )
        return out

    def _materialize_leaf_path_chunks(self, section_id: str, doc_id: str) -> List[Any]:
        leaf_fn = getattr(self._provider, "leaf_ids", None)
        if not callable(leaf_fn):
            text = str(self._provider.content(section_id) or "")
            if not text.strip():
                return []
            return [
                self._make_chunk(f"{section_id}__path", doc_id, text, 0, section_id)
            ]

        # One unit per descendant leaf, plus one per interstitial parent, so
        # node ids line up with the keys scoring.score_units.build_score_units emits.
        out: List[Any] = []
        for leaf_id in cast(Sequence[Any], leaf_fn(section_id) or ()):
            text, order, _count = self._node_unit_span(leaf_id)
            if text:
                out.append(self._make_chunk(leaf_id, doc_id, text, order, leaf_id))
        interstitial = [section_id, *sorted(self._provider.relations(section_id)[1])]
        for sid in interstitial:
            if not self._provider.children(sid):
                continue
            text, order, count = self._node_unit_span(sid)
            if text and count > 1:
                out.append(self._make_chunk(f"{sid}__self", doc_id, text, order, sid))
        out.sort(key=lambda c: (min(c.line_ids or (0,)), c.node_id))
        return out

    def load_persisted_score_corpus(
        self,
        doc_ids: Sequence[str],
        queries: Sequence[str],
    ) -> Optional["PersistedScoreCorpus"]:
        """Forward the optional revision-pinned map-unit index capability."""
        fn = getattr(self._provider, "load_persisted_score_corpus", None)
        if not callable(fn):
            return None
        return cast(Optional["PersistedScoreCorpus"], fn(doc_ids, queries))

