"""In-memory hierarchy fixtures for map-nav tests.

``ProviderToolSpace`` / ``NodeMeta`` / ``HierarchyProvider`` live in
``shared.services.retrieval.scoring.hierarchy``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from shared.services.retrieval.scoring.hierarchy import NodeMeta

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
