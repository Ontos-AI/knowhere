"""Build persisted map-unit scoring rows from a hierarchy tool space."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, cast

from shared.services.retrieval.scoring.knowhere_hybrid import (
    build_content_search_text,
    build_path_search_text,
)

def _children_ids(ts: Any, section_id: str, doc_id: str) -> List[str]:
    children_fn = getattr(ts, "_children_for_section_path", None)
    if not callable(children_fn):
        st = ts.get_structure(section_id)
        rows = st.get("children") or []
        return [
            str(r.get("section_id") or "").strip() for r in rows if r.get("section_id")
        ]
    rows = cast(Sequence[Any], children_fn(section_id, doc_id))
    return [str(r.get("section_id") or "").strip() for r in rows if r.get("section_id")]


def _line_content(ts: Any, section_id: str, doc_id: str) -> str:
    """Raw line text for a section node (no truncation)."""
    idx = getattr(ts, "_idx", None)
    b = getattr(idx, "_bundles", {}).get(doc_id) if idx is not None else None
    if b is None:
        path_fn = getattr(ts, "path_titles", None)
        if callable(path_fn):
            path = str(path_fn(section_id, doc_id) or "").strip()
            return path.rsplit(" / ", 1)[-1] if path else ""
        st = ts.get_structure(section_id)
        return str(st.get("preview") or "").strip()
    loc = getattr(idx, "_node_to_doc_line", {}).get(section_id)
    if not loc:
        return ""
    _doc, line_idx = loc
    if line_idx < 0 or line_idx >= len(b.lines):
        return ""
    return str(b.lines[line_idx].content or "").strip()


def _ancestor_path_titles(ts: Any, section_id: str, doc_id: str) -> str:
    idx = getattr(ts, "_idx", None)
    if idx is None:
        # Provider-backed spaces expose the title chain directly; without this
        # the path channel would score every unit as empty.
        path_fn = getattr(ts, "path_titles", None)
        return str(path_fn(section_id, doc_id) or "") if callable(path_fn) else ""
    try:
        ancestors = list(idx.ancestor_line_node_ids(section_id))
    except Exception:
        ancestors = []
    titles: List[str] = []
    for aid in reversed(ancestors):
        if not str(aid).startswith(f"{doc_id}:"):
            continue
        titles.append(_line_content(ts, aid, doc_id))
    titles.append(_line_content(ts, section_id, doc_id))
    return " / ".join(t for t in titles if t)


def _self_only_text(ts: Any, section_id: str, doc_id: str) -> Tuple[str, bool]:
    """Return (self_text, has_interstitial_body).

    Interstitial means self_only span contains content beyond the heading line
    itself (structural: more than one line/chunk in the self span).
    """
    self_fn = getattr(ts, "materialize_self_only_chunks", None)
    if not callable(self_fn):
        return "", False
    chunks = list(cast(Sequence[Any], self_fn(section_id, doc_id) or []))
    if not chunks:
        return "", False
    texts = [str(getattr(c, "text", "") or "").strip() for c in chunks]
    texts = [t for t in texts if t]
    if not texts:
        return "", False
    # Structural interstitial: self span covers more than the node heading line.
    has_interstitial = len(chunks) > 1
    return "\n".join(texts), has_interstitial


def _section_body_text(ts: Any, section_id: str, doc_id: str) -> str:
    """Heading + lines until first structural child (leaf body / parent self span)."""
    text, _ = _self_only_text(ts, section_id, doc_id)
    if text:
        return text
    return _line_content(ts, section_id, doc_id)


def _iterate_children(ts: Any, section_id: str, doc_id: str) -> Iterator[str]:
    provider = getattr(ts, "_provider", None)
    iterator = getattr(provider, "iterateChildren", None)
    if callable(iterator):
        for child in cast(Iterator[str], iterator(section_id)):
            if child:
                yield str(child)
        return
    yield from _children_ids(ts, section_id, doc_id)


def _walk_tree(ts: Any, doc_id: str, root_ids: Iterator[str],
               children_map: Dict[str, List[str]], leaves: List[str],
               titles: Dict[str, str]) -> Iterator[None]:
    """Visit one node/edge per checkpoint without recursion or a giant child copy."""
    seen: Set[str] = set()
    for root in root_ids:
        if not root or root in seen:
            yield None
            continue
        seen.add(root)
        titles[root] = _line_content(ts, root, doc_id)
        children_map[root] = []
        stack: List[Tuple[str, Iterator[str]]] = [(root, _iterate_children(ts, root, doc_id))]
        yield None
        while stack:
            section_id, children = stack[-1]
            child: str | None = next(children, None)
            if child is None:
                if not children_map[section_id]:
                    leaves.append(section_id)
                stack.pop()
                yield None
                continue
            children_map[section_id].append(child)
            yield None
            if child in seen:
                continue
            seen.add(child)
            titles[child] = _line_content(ts, child, doc_id)
            children_map[child] = []
            stack.append((child, _iterate_children(ts, child, doc_id)))
            yield None


def _iterate_score_units(ts: Any, doc_id: str, root_ids: Optional[Sequence[str]] = None,
                         *, tokenize: bool = True) -> Iterator[dict | None]:
    provider = getattr(ts, "_provider", None)
    roots = getattr(provider, "iterateRoots", None)
    identities: Iterator[str] = iter(root_ids) if root_ids is not None else (
        cast(Iterator[str], roots(doc_id)) if callable(roots) else iter(ts.sections_for_doc(doc_id)))
    children_map: Dict[str, List[str]] = {}
    leaves: List[str] = []
    titles: Dict[str, str] = {}
    for _ in _walk_tree(ts, doc_id, identities, children_map, leaves, titles):
        yield None
    seen_unit_ids: Set[str] = set()
    for leaf_id in leaves:
        content: str = _section_body_text(ts, leaf_id, doc_id) or (titles.get(leaf_id) or _line_content(ts, leaf_id, doc_id))
        path_text: str = _ancestor_path_titles(ts, leaf_id, doc_id)
        if leaf_id in seen_unit_ids:
            yield None
            continue
        seen_unit_ids.add(leaf_id)
        title: str = titles.get(leaf_id) or _line_content(ts, leaf_id, doc_id)
        unit: dict = {"chunk_id": leaf_id, "section_id": leaf_id, "kind": "leaf", "content": content, "path_text": path_text,
            "path_search_text": build_path_search_text(section_path=path_text, section_title=title or content) if tokenize else "",
            "content_search_text": build_content_search_text(content) if tokenize else ""}
        if not tokenize:
            unit["section_title"] = title or content
        yield unit
    for sid, children in children_map.items():
        if not children:
            yield None
            continue
        self_text, has_interstitial = _self_only_text(ts, sid, doc_id)
        if not has_interstitial or not self_text:
            yield None
            continue
        unit_id: str = f"{sid}__self"
        if unit_id in seen_unit_ids:
            yield None
            continue
        seen_unit_ids.add(unit_id)
        path_text = _ancestor_path_titles(ts, sid, doc_id)
        title = titles.get(sid) or ""
        unit = {"chunk_id": unit_id, "section_id": sid, "kind": "self_only", "content": self_text, "path_text": path_text,
            "path_search_text": build_path_search_text(section_path=path_text, section_title=title) if tokenize else "",
            "content_search_text": build_content_search_text(self_text) if tokenize else ""}
        if not tokenize:
            unit["section_title"] = title
        yield unit


def build_score_units(ts: Any, doc_id: str, root_ids: Optional[Sequence[str]] = None,
                      *, tokenize: bool = True) -> List[dict]:
    """Build the same ordered leaf/self units, optionally retaining raw text only."""
    return [unit for unit in _iterate_score_units(ts, doc_id, root_ids, tokenize=tokenize) if unit is not None]
