from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .nav_types import NavConfig, Projection, SectionView, map_mode_enabled

try:
    from section_summary_store import get_summary
except Exception:  # pragma: no cover
    def get_summary(section_id: str, *, doc_id: str = "") -> Optional[str]:  # type: ignore
        return None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w\u4e00-\u9fff]+", (text or "").lower()))


def _lexical_score(query: str, text: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    t = _tokens(text)
    if not t:
        return 0.0
    inter = len(q & t)
    if inter == 0:
        return 0.0
    return float(inter) / math.sqrt(float(len(q) * len(t)))


def _section_view_from_structure(
    ts: Any,
    section_id: str,
    *,
    query: str,
    depth_from_scope: int,
    summary_chars: int,
) -> SectionView:
    st = ts.get_structure(section_id)
    preview = str(st.get("preview") or "").replace("\n", " ")[:summary_chars]
    children = st.get("children") or []
    return SectionView(
        section_id=str(st.get("section_id") or section_id),
        level=int(st.get("level") or 0) if str(st.get("level") or "").isdigit() else 0,
        preview=preview,
        score=_lexical_score(query, f"{section_id} {preview}"),
        n_lines=int(st.get("n_lines") or 0),
        n_chunks=int(st.get("n_chunks") or 0),
        has_children=bool(children),
        depth_from_scope=depth_from_scope,
        title=preview[:80] if preview else section_id,
    )


def _children(ts: Any, section_id: str, *, limit: int) -> List[dict]:
    children_fn = getattr(ts, "_children_for_section_path", None)
    if callable(children_fn):
        loc = getattr(ts, "_idx", None)
        doc_id = ""
        if loc is not None:
            node = getattr(loc, "_node_to_doc_line", {}).get(section_id)
            if node:
                doc_id = node[0]
            else:
                synth = getattr(ts, "_synthetic_doc_id", lambda _x: None)(section_id)
                doc_id = synth or ""
        if doc_id:
            rows = children_fn(section_id, doc_id, limit=max(1, int(limit)))
            if isinstance(rows, list):
                return [c for c in rows if isinstance(c, dict)]
    st = ts.get_structure(section_id)
    children = st.get("children") or []
    if isinstance(children, list):
        return [c for c in children if isinstance(c, dict)][: max(0, int(limit))]
    return []


def _top_sections(ts: Any, doc_id: str) -> List[str]:
    try:
        return list(ts.sections_for_doc(doc_id))
    except Exception:
        return []


def _title_from_row(row: dict, section_id: str) -> str:
    preview = str(row.get("preview") or "").replace("\n", " ").strip()
    if preview:
        return preview[:80]
    return section_id


@dataclass
class _MapNode:
    section_id: str
    depth: int
    title: str
    score: float
    n_lines: int
    n_chunks: int
    has_children: bool
    children: List["_MapNode"] = field(default_factory=list)
    n_descendants: int = 0
    hidden: bool = False
    is_highlight: bool = False
    map_id: str = ""
    parent_id: Optional[str] = None
    harvested_by: str = ""


def _count_descendants(node: _MapNode) -> int:
    total = 0
    for child in node.children:
        if child.hidden:
            continue
        total += 1 + _count_descendants(child)
    node.n_descendants = total
    return total


def _flatten(nodes: List[_MapNode], *, include_hidden: bool = False) -> List[_MapNode]:
    out: List[_MapNode] = []

    def rec(node: _MapNode) -> None:
        if node.hidden and not include_hidden:
            return
        out.append(node)
        for child in node.children:
            rec(child)

    for n in nodes:
        rec(n)
    return out


def _ancestors_in_tree(roots: List[_MapNode], section_id: str) -> List[str]:
    found: List[str] = []

    def dfs(node: _MapNode, trail: List[str]) -> bool:
        if node.section_id == section_id:
            found.extend(trail)
            return True
        for child in node.children:
            if dfs(child, trail + [node.section_id]):
                return True
        return False

    for root in roots:
        if dfs(root, []):
            break
    return found


def _protected_spine_ids(nodes: List[_MapNode], must_keep: Set[str]) -> Set[str]:
    """Return must-keep nodes and every ancestor needed to expose them."""
    protected: Set[str] = set()

    def visit(node: _MapNode) -> bool:
        contains = node.section_id in must_keep
        for child in node.children:
            contains = visit(child) or contains
        if contains:
            protected.add(node.section_id)
        return contains

    for root in nodes:
        visit(root)
    return protected


def _mark_hidden_subtree(node: _MapNode) -> None:
    node.hidden = True
    for child in node.children:
        _mark_hidden_subtree(child)


def _estimate_actionable_line(node: _MapNode, *, with_summary: bool = False) -> int:
    indent = 2 * node.depth
    title = len(node.title or node.section_id)
    meta = 28
    tags = 14
    actions = 48
    summary = 140 if with_summary else 0
    return indent + title + meta + tags + actions + summary + 1


def _estimate_actionable_total(nodes: List[_MapNode], *, with_summary: bool = False) -> int:
    total = 120
    stack = list(nodes)
    while stack:
        cur = stack.pop()
        if cur.hidden:
            continue
        total += _estimate_actionable_line(cur, with_summary=with_summary)
        stack.extend(cur.children)
    return total


def _visible_subtree_estimate(
    node: _MapNode,
    *,
    with_summary: bool = False,
) -> int:
    """Estimated actionable characters removed by hiding this visible subtree."""
    total = 0
    stack = [node]
    while stack:
        current = stack.pop()
        if current.hidden:
            continue
        total += _estimate_actionable_line(
            current,
            with_summary=with_summary,
        )
        stack.extend(current.children)
    return total


def _apply_budget_hide(
    nodes: List[_MapNode],
    *,
    char_limit: int,
    must_keep: Set[str],
    extra_hidden_ids: Optional[Set[str]] = None,
    with_summary: bool = False,
) -> None:
    """Hide low-score branches so the actionable map fits char_limit.

    TODO: if exploration near highlights fails, re-reveal previously hidden
    subtrees into the actionable map (budget permitting).

    Never hard-truncates: keeps hiding score-ordered candidates (depth-0 allowed)
    until the estimate fits or only must_keep spine remains.
    """
    flat_all = _flatten(nodes, include_hidden=True)
    protected = _protected_spine_ids(nodes, must_keep)
    extra = set(extra_hidden_ids or ())

    for node in flat_all:
        if node.section_id in extra and node.section_id not in protected:
            _mark_hidden_subtree(node)

    for root in nodes:
        _count_descendants(root)

    current_estimate = _estimate_actionable_total(
        nodes,
        with_summary=with_summary,
    )
    if current_estimate <= char_limit:
        return

    # Sort once. Chosen subtrees are disjoint after already-hidden descendants
    # are skipped, so total subtree-estimation work remains linear.
    candidates = [
        node
        for node in flat_all
        if not node.hidden and node.section_id not in protected
    ]
    candidates.sort(
        key=lambda node: (
            node.score,
            -node.n_descendants,
            -node.depth,
            node.section_id,
        )
    )
    for node in candidates:
        if current_estimate <= char_limit:
            break
        if node.hidden:
            continue
        current_estimate -= _visible_subtree_estimate(
            node,
            with_summary=with_summary,
        )
        _mark_hidden_subtree(node)


def _build_map_tree(
    ts: Any,
    *,
    root_ids: List[str],
    map_scores: Dict[str, float],
    children_limit: int,
    max_nodes: int = 20000,
    collected_section_ids: Optional[Set[str]] = None,
    dismissed_section_ids: Optional[Set[str]] = None,
    harvested_section_ids: Optional[Dict[str, str]] = None,
) -> List[_MapNode]:
    roots: List[_MapNode] = []
    seen: Set[str] = set()
    node_count = 0
    harvested = dict(harvested_section_ids or {})
    # collected = branch done (sid ∪ descendants already marked by caller).
    # Harvested roots stay visible as a collapsed single line (fix-map-
    # visibility); their descendants are still fully removed like any other
    # collected branch — the point is coverage context, not re-expansion.
    gone = (
        set(collected_section_ids or ()) - set(harvested.keys())
    ) | set(dismissed_section_ids or ())

    def make_node(section_id: str, depth: int, parent_id: Optional[str]) -> Optional[_MapNode]:
        nonlocal node_count
        if not section_id or section_id in seen or node_count >= max_nodes:
            return None
        if section_id in gone:
            return None
        seen.add(section_id)
        node_count += 1
        try:
            st = ts.get_structure(section_id)
        except Exception:
            return None
        preview = str(st.get("preview") or "").replace("\n", " ").strip()
        title = preview if preview else section_id
        score = float(map_scores.get(section_id, 0.0) or 0.0)
        return _MapNode(
            section_id=section_id,
            depth=depth,
            title=title,
            score=score,
            n_lines=int(st.get("n_lines") or 0),
            n_chunks=int(st.get("n_chunks") or 0),
            has_children=False,
            parent_id=parent_id,
        )

    def append_visible_descendants(
        parent_children: List[_MapNode],
        section_id: str,
        depth: int,
        parent_id: Optional[str],
        row_title_hint: Optional[dict] = None,
    ) -> None:
        """Attach section_id if visible. collected/dismissed drop node + subtree."""
        if not section_id or section_id in gone:
            return
        node = make_node(section_id, depth, parent_id)
        if node is None:
            return
        if row_title_hint and (not node.title or node.title == section_id):
            node.title = _title_from_row(row_title_hint, section_id)
        if section_id in harvested:
            # Collapsed leaf: this line alone represents the covered branch.
            node.harvested_by = str(harvested[section_id])
        else:
            for row in _children(ts, section_id, limit=children_limit):
                child_id = str(row.get("section_id") or "").strip()
                if child_id:
                    append_visible_descendants(
                        node.children, child_id, depth + 1, section_id, row
                    )
        node.has_children = bool(node.children)
        _count_descendants(node)
        parent_children.append(node)

    for rid in root_ids:
        append_visible_descendants(roots, rid, 0, None)
    return roots


def _clip_summary(text: str, *, head: int = 120) -> str:
    s = (text or "").replace("\n", " ").strip()
    if len(s) <= head:
        return s
    return s[: max(0, head - 1)].rstrip() + "…"


def format_hit_tag(*, is_highlight: bool) -> str:
    """Render baseline Hit badge from highlight_ids."""
    return " [Hit]" if is_highlight else ""


def format_harvested_tag(owner_subgoal_id: str) -> str:
    """Render the collapsed-coverage badge for a node kept visible post-collect."""
    sid = str(owner_subgoal_id or "").strip()
    return f" [harvested:{sid}]" if sid else ""


def _render_map(
    nodes: List[_MapNode],
    *,
    ts: Any = None,
    doc_id: str,
    scope: Optional[str],
    char_limit: int,
    highlight_ids: Optional[Set[str]] = None,
    inline_summary: bool = False,
) -> tuple[str, List[SectionView], Dict[str, str], bool]:
    """Render the budget-hidden title map (no mid-tree hard truncation)."""
    _ = char_limit
    hits = set(highlight_ids or ())
    lines: List[str] = []
    visible: List[SectionView] = []
    id_to_section: Dict[str, str] = {}
    counter = 1
    any_hidden = False

    lines.append(f"doc_id={doc_id}")
    lines.append(f"scope={scope or '<document-root>'}")
    lines.append(
        "map=title+summary" if inline_summary else "map=title-only (action IDs attached per node)"
    )

    def _summary_doc_for(section_id: str) -> str:
        if ts is None:
            return str(doc_id or "")
        try:
            from .nav_address import owner_document

            return owner_document(ts, section_id, doc_id) or str(doc_id or "")
        except Exception:
            return str(doc_id or "")

    def render(node: _MapNode) -> None:
        nonlocal counter, any_hidden
        if node.hidden:
            any_hidden = True
            return
        map_id = f"N{counter}"
        counter += 1
        node.map_id = map_id
        id_to_section[map_id] = node.section_id
        indent = "  " * node.depth
        is_hit = node.section_id in hits or node.is_highlight
        leaf_tag = " [Leaf]" if not node.has_children else ""
        hit_tag = format_hit_tag(is_highlight=is_hit)
        harvested_tag = format_harvested_tag(node.harvested_by)
        line = (
            f"{indent}[{map_id}] {node.title} ({node.n_chunks} chunks)"
            f"{leaf_tag}{hit_tag}{harvested_tag}"
        )
        lines.append(line)
        summary = ""
        if inline_summary:
            summary = _clip_summary(
                get_summary(node.section_id, doc_id=_summary_doc_for(node.section_id))
                or ""
            )
            if summary:
                lines.append(f"{indent}    summary: {summary}")
        visible.append(
            SectionView(
                section_id=node.section_id,
                level=node.depth,
                preview="",
                score=node.score,
                n_lines=node.n_lines,
                n_chunks=node.n_chunks,
                has_children=node.has_children,
                depth_from_scope=node.depth,
                map_id=map_id,
                title=node.title,
                n_descendants=node.n_descendants,
                is_highlight=is_hit,
                parent_id=node.parent_id,
                summary=summary,
                harvested_by=node.harvested_by,
            )
        )
        for child in node.children:
            render(child)

    for root in nodes:
        render(root)

    return "\n".join(lines), visible, id_to_section, any_hidden


def _fallback_highlights_from_tree(roots: List[_MapNode], k: int) -> List[str]:
    leaves = [n for n in _flatten(roots, include_hidden=True) if not n.has_children]
    leaves.sort(key=lambda n: (-n.score, n.section_id))
    return [n.section_id for n in leaves[: max(0, int(k))]]


def build_map(
    ts: Any,
    *,
    doc_id: str,
    query: str,
    scope: Optional[str],
    config: NavConfig,
    map_scores: Optional[Dict[str, float]] = None,
    collected_section_ids: Optional[Set[str]] = None,
    dismissed_section_ids: Optional[Set[str]] = None,
    highlight_ids: Optional[List[str]] = None,
    extra_hidden_ids: Optional[Set[str]] = None,
    harvested_section_ids: Optional[Dict[str, str]] = None,
) -> Projection:
    """Full-depth title map with score-ordered budget hiding (+ optional inline summary)."""
    scores = dict(map_scores or {})
    if scope:
        root_ids = [scope]
    else:
        root_ids = _top_sections(ts, doc_id)
    roots = _build_map_tree(
        ts,
        root_ids=root_ids,
        map_scores=scores,
        children_limit=max(1, int(config.map_children_limit)),
        collected_section_ids=collected_section_ids,
        dismissed_section_ids=dismissed_section_ids,
        harvested_section_ids=harvested_section_ids,
    )
    hits = [str(x).strip() for x in (highlight_ids or []) if str(x).strip()]
    if not hits:
        hits = _fallback_highlights_from_tree(roots, int(config.collect_top_k))
    hit_set = set(hits)
    for node in _flatten(roots, include_hidden=True):
        if node.section_id in hit_set:
            node.is_highlight = True

    must_keep: Set[str] = set(hit_set)
    for hid in hits:
        must_keep.update(_ancestors_in_tree(roots, hid))

    # Root is always title-only. A scoped region keeps summaries only while its
    # full actionable map is small; large scopes become title-only so the agent
    # can see more branches and choose another DISPATCH.
    scope_summary_limit = max(
        0, int(getattr(config, "scope_inline_summary_char_limit", 2000) or 0)
    )
    inline_summary = (
        scope is not None
        and _estimate_actionable_total(roots, with_summary=True)
        <= scope_summary_limit
    )
    char_limit = max(1, int(config.map_char_limit or config.projection_char_limit))
    _apply_budget_hide(
        roots,
        char_limit=char_limit,
        must_keep=must_keep,
        extra_hidden_ids=extra_hidden_ids,
        with_summary=inline_summary,
    )
    text, tree_visible, id_map, truncated = _render_map(
        roots,
        ts=ts,
        doc_id=doc_id,
        scope=scope,
        char_limit=char_limit,
        highlight_ids=hit_set,
        inline_summary=inline_summary,
    )
    visible_sorted = sorted(
        tree_visible,
        key=lambda v: (-v.score, v.depth_from_scope, v.section_id),
    )
    return Projection(
        doc_id=doc_id,
        scope=scope,
        text=text,
        visible_sections=visible_sorted,
        truncated=truncated,
        id_to_section=id_map,
        map_mode=True,
        tree_sections=list(tree_visible),
        highlight_ids=list(hits),
    )


def build_projection(
    ts: Any,
    *,
    doc_id: str,
    query: str,
    scope: Optional[str],
    config: NavConfig,
    map_scores: Optional[Dict[str, float]] = None,
    collected_section_ids: Optional[Set[str]] = None,
    dismissed_section_ids: Optional[Set[str]] = None,
    highlight_ids: Optional[List[str]] = None,
    extra_hidden_ids: Optional[Set[str]] = None,
    harvested_section_ids: Optional[Dict[str, str]] = None,
) -> Projection:
    if map_mode_enabled(config):
        return build_map(
            ts,
            doc_id=doc_id,
            query=query,
            scope=scope,
            config=config,
            map_scores=map_scores,
            collected_section_ids=collected_section_ids,
            dismissed_section_ids=dismissed_section_ids,
            highlight_ids=highlight_ids,
            extra_hidden_ids=extra_hidden_ids,
            harvested_section_ids=harvested_section_ids,
        )

    # Minimal non-map fallback (legacy shallow projection) — kept for ablation only.
    visible: List[SectionView] = []
    lines: List[str] = []
    truncated = False

    def add_line(text: str) -> None:
        nonlocal truncated
        if truncated:
            return
        candidate_len = sum(len(x) + 1 for x in lines) + len(text)
        if candidate_len > config.projection_char_limit:
            lines.append("... [projection truncated]")
            truncated = True
            return
        lines.append(text)

    add_line(f"doc_id={doc_id}")
    add_line(f"scope={scope or '<document-root>'}")

    if scope:
        root_ids = [scope]
    else:
        root_ids = _top_sections(ts, doc_id)

    collected = set(collected_section_ids or ())
    root_ids = root_ids[: max(1, config.projection_child_limit)]
    frontier: List[tuple[str, int]] = [(sid, 0) for sid in root_ids]
    seen: set[str] = set()
    while frontier:
        sid, depth = frontier.pop(0)
        if sid in seen or sid in collected:
            continue
        seen.add(sid)
        try:
            view = _section_view_from_structure(
                ts,
                sid,
                query=query,
                depth_from_scope=depth,
                summary_chars=config.summary_chars,
            )
        except Exception:
            continue
        visible.append(view)
        indent = "  " * depth
        leaf_tag = " [Leaf]" if not view.has_children else ""
        title = view.preview[:80] if view.preview else view.section_id
        add_line(
            f"{indent}[{view.section_id}] {title} ({view.n_chunks} chunks){leaf_tag}"
        )
        if view.preview:
            add_line(f"{indent}     Preview: \"{view.preview[:80]}\"")
        if depth + 1 >= max(1, config.projection_depth):
            continue
        child_rows = _children(ts, sid, limit=max(0, config.projection_child_limit))
        for child in child_rows:
            child_id = str(child.get("section_id") or "").strip()
            if child_id and child_id not in seen:
                frontier.append((child_id, depth + 1))

    visible.sort(key=lambda v: (-v.score, v.depth_from_scope, v.section_id))
    return Projection(
        doc_id=doc_id,
        scope=scope,
        text="\n".join(lines),
        visible_sections=visible,
        truncated=truncated,
        map_mode=False,
    )


def top_visible_sections(projection: Projection, *, limit: int) -> List[SectionView]:
    return list(projection.visible_sections[: max(0, limit)])
