"""Portable kernel seam: HierarchyProvider Protocol + ToolSpace-shaped adapter.

``docs/audit_plan_nav_overlap.md`` (§ToolSpace surface) shows every call site
under ``src/nav`` touches at most 5 hierarchy operations: a document's
top-level sections, a node's children, a node's structural metadata
(title/summary/chunk count), a node's ancestor/descendant ids, and a node's
full-subtree text as one evidence unit. Everything else this codebase's
``ToolSpace`` exposes (BM25/dense scoring, ``read_chunks``, ``_idx``,
``corpus_doc_ids``) is optional — every caller already reaches it through
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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple, runtime_checkable


@dataclass
class NodeMeta:
    title: str = ""
    summary: str = ""
    has_children: bool = False
    n_chunks: int = 0


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
        """Title/summary/chunk-count/has_children for one node."""
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

    def address_level(self, node_id: str):
        fn = getattr(self._provider, "address_level", None)
        return fn(node_id) if callable(fn) else None

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
        return [str(x) for x in (fn() or ()) if str(x).strip()]

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
            "n_chunks": int(meta.n_chunks),
            "children": [
                {"section_id": cid, "preview": self._provider.node_meta(cid).title}
                for cid in child_ids
            ],
            "exists": True,
        }

    def _children_for_section_path(
        self, section_id: str, doc_id: str, limit: Optional[int] = None
    ) -> List[dict]:
        del doc_id  # section_id is globally addressable in this provider model.
        child_ids = [str(c) for c in self._provider.children(section_id)]
        if limit is not None:
            child_ids = child_ids[: max(0, int(limit))]
        return [
            {"section_id": cid, "preview": self._provider.node_meta(cid).title}
            for cid in child_ids
        ]

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
        """(joined text, first sort_order, unit count) for one node's own units."""
        self_units = getattr(self._provider, "self_units", None)
        unit_text = getattr(self._provider, "unit_text", None)
        if not callable(self_units) or not callable(unit_text):
            return "", 0, 0
        units = list(self_units(section_id) or ())
        texts = [t for t in (str(unit_text(u) or "").strip() for u in units) if t]
        if not texts:
            return "", 0, len(units)
        first_order = int(getattr(units[0], "sort_order", 0) or 0)
        return "\n".join(texts), first_order, len(units)

    def _make_chunk(self, node_id: str, doc_id: str, text: str, order: int, section_id: str) -> Any:
        from ._compat import Chunk  # type: ignore

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
        for unit in self_units(section_id) or ():
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
            return [self._make_chunk(f"{section_id}__path", doc_id, text, 0, section_id)]

        # One unit per descendant leaf, plus one per interstitial parent, so
        # node ids line up with the keys nav_map_scores.build_score_units emits.
        out: List[Any] = []
        for leaf_id in leaf_fn(section_id) or ():
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

    def read_chunks(self, section_id: str, query: str, *, doc_id: str, k: int) -> List[Any]:
        del section_id, query, doc_id, k
        return []

    def release_section_units(self, section_id: str) -> None:
        """Release one lazy section without discarding the hierarchy."""
        release = getattr(self._provider, "release_section_units", None)
        if callable(release):
            release(section_id)

    def prefetch_document_units(self, doc_id: str) -> None:
        """Forward a provider's bounded document payload prefetch capability."""
        provider = self._provider
        fn = getattr(provider, "prefetch_document_units", None)
        if not callable(fn):
            return
        if callable(getattr(provider, "document_ids", None)):
            fn(doc_id)
            return
        if str(getattr(provider, "doc_id", "")) == str(doc_id):
            fn()

    def release_document_units(self, doc_id: str) -> None:
        """Forward release of one document's prefetched payloads."""
        provider = self._provider
        fn = getattr(provider, "release_document_units", None)
        if not callable(fn):
            return
        if callable(getattr(provider, "document_ids", None)):
            fn(doc_id)
            return
        if str(getattr(provider, "doc_id", "")) == str(doc_id):
            fn()


@dataclass
class InMemoryNode:
    section_id: str
    title: str
    content: str = ""
    children: List[str] = field(default_factory=list)


class InMemoryHierarchyProvider:
    """Minimal reference ``HierarchyProvider``: no scoring, no ToolSpace.

    Built directly from a ``{doc_id: [InMemoryNode, ...]}`` map plus a
    ``{doc_id: [root_section_id, ...]}`` map — the "hierarchy + summary is
    enough" claim's simplest possible witness.
    """

    def __init__(
        self,
        *,
        roots_by_doc: Dict[str, Sequence[str]],
        nodes: Dict[str, InMemoryNode],
        summaries: Optional[Dict[str, str]] = None,
    ) -> None:
        self._roots_by_doc = {k: list(v) for k, v in roots_by_doc.items()}
        self._nodes = dict(nodes)
        self._summaries = dict(summaries or {})
        self._parent: Dict[str, str] = {}
        for node in self._nodes.values():
            for child_id in node.children:
                self._parent[child_id] = node.section_id
        self._owner: Dict[str, str] = {}
        for doc_id, root_ids in self._roots_by_doc.items():
            stack = list(root_ids)
            while stack:
                sid = stack.pop()
                if sid in self._owner:
                    continue
                self._owner[sid] = doc_id
                node = self._nodes.get(sid)
                if node:
                    stack.extend(node.children)

    def owner_document(self, node_id: str) -> Optional[str]:
        return self._owner.get(str(node_id or "").strip())

    def roots(self, doc_id: str) -> Sequence[str]:
        return list(self._roots_by_doc.get(doc_id, ()))

    def children(self, section_id: str) -> Sequence[str]:
        node = self._nodes.get(section_id)
        return list(node.children) if node else []

    def node_meta(self, section_id: str) -> NodeMeta:
        node = self._nodes.get(section_id)
        if node is None:
            return NodeMeta()
        return NodeMeta(
            title=node.title,
            summary=self._summaries.get(section_id, ""),
            has_children=bool(node.children),
            n_chunks=1,
        )

    def parent_id(self, section_id: str) -> Optional[str]:
        return self._parent.get(section_id)

    def relations(self, section_id: str) -> Tuple[Set[str], Set[str]]:
        ancestors: Set[str] = set()
        cur = self._parent.get(section_id)
        while cur:
            ancestors.add(cur)
            cur = self._parent.get(cur)
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
        node = self._nodes.get(section_id)
        if node is None:
            return ""
        parts: List[str] = []

        def walk(sid: str) -> None:
            cur = self._nodes.get(sid)
            if cur is None:
                return
            if cur.content:
                parts.append(cur.content)
            for cid in cur.children:
                walk(cid)

        walk(section_id)
        return "\n".join(parts)
