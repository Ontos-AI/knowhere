"""Nav COMPOSE packing: parent-scoped rank + one-level indent tree.

Parent sections are path headers only (not competing evidence units).
Child score = own_unit + w_conf * collect_confidence (default w_conf=0.5).
Group order: group_priority (external rank), then max child score, then doc order.

Over-budget trim (progressive): keep group order; drop from back groups forward —
first non-explicit COLLECT owners, then lowest unit-score — until under budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from shared.services.retrieval.hydration.evidence_text import render_evidence_blocks

from ._compat import Chunk
from ._compat import line_node_id
from ._compat import ToolSpace
from .nav_address import NavLevel, address_level, owner_document

from .nav_types import NavConfig, NavState


@dataclass
class ComposeFillResult:
    kept_chunks: List[Chunk]
    evidence_text: str
    evidence_chars_actual: int
    n_chunks_kept: int
    truncated_last: bool
    scored_chunks: List[Tuple[Chunk, float]]
    dropped_any: bool = False


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _section_doc_id(ts: ToolSpace, section_id: str, fallback_doc_id: str = "") -> str:
    """Resolve owning document_id (registry first; never parse id strings)."""
    sid = str(section_id or "").strip()
    if not sid:
        return str(fallback_doc_id or "")
    resolved = owner_document(ts, sid, "")
    if resolved:
        return resolved
    return str(fallback_doc_id or "")


def evidence_owner_section_id(chunk: Chunk) -> str:
    """Section that owns a hydrated evidence chunk (strip __path/__intro suffixes)."""
    nid = str(getattr(chunk, "node_id", "") or "").strip()
    for suf in ("__path", "__intro", "__outline", "__self"):
        if nid.endswith(suf):
            return nid[: -len(suf)]
    sid = str(getattr(chunk, "section_id", "") or "").strip()
    return nid or sid


def unit_score_for_evidence_chunk(chunk: Chunk, unit_scores: Dict[str, float]) -> float:
    """Map leaf/path materialize ids onto hybrid unit scores."""
    scores = unit_scores or {}
    nid = str(getattr(chunk, "node_id", "") or "")
    if nid.endswith("__path"):
        base = nid[: -len("__path")]
        return float(scores.get(base, 0.0) or 0.0)
    if nid.endswith("__intro"):
        base = nid[: -len("__intro")]
        return float(scores.get(f"{base}__self", scores.get(base, 0.0)) or 0.0)
    if nid.endswith("__self"):
        base = nid[: -len("__self")]
        return float(scores.get(f"{base}__self", scores.get(base, 0.0)) or 0.0)
    if nid.endswith("__outline"):
        base = nid[: -len("__outline")]
        sid = str(getattr(chunk, "section_id", "") or "")
        return float(scores.get(sid, scores.get(base, 0.0)) or 0.0)
    return float(scores.get(nid, 0.0) or 0.0)


def direct_parent_id(ts: ToolSpace, section_id: str, doc_id: str) -> Optional[str]:
    sid = str(section_id or "").strip()
    if not sid:
        return None
    resolved = _section_doc_id(ts, sid, doc_id)
    if not resolved:
        return None
    idx = getattr(ts, "_idx", None)
    if idx is None:
        return None
    loc = getattr(idx, "_node_to_doc_line", {}).get(sid)
    if not loc or loc[0] != resolved:
        return None
    _, j = loc
    parents = getattr(idx, "_doc_parents", {}).get(resolved, [])
    b = getattr(idx, "_bundles", {}).get(resolved)
    if not b or j >= len(parents):
        return None
    p = parents[j]
    if p is None or p < 0 or p >= len(b.lines):
        return None
    return line_node_id(resolved, b.lines[p].line_id)


def _section_title(ts: ToolSpace, section_id: str, doc_id: str) -> str:
    sid = str(section_id or "").strip()
    if not sid:
        return ""

    # ``path_titles`` is a lightweight title lookup.  Prefer it over
    # ``get_structure`` because the latter may calculate the complete subtree
    # chunk count, which is unnecessary while rendering evidence headers.
    path_titles = getattr(ts, "path_titles", None)
    if callable(path_titles):
        try:
            path = str(path_titles(sid, doc_id) or "").strip()
        except TypeError:
            path = str(path_titles(sid) or "").strip()
        if path:
            return path

    # Prefer structure title (Knowhere / ProviderToolSpace); never parse ids.
    try:
        st = ts.get_structure(sid)
    except Exception:
        st = None
    if isinstance(st, dict):
        raw = st.get("preview") or st.get("title") or ""
        if isinstance(raw, str) and raw.strip():
            return raw.strip()

    resolved = _section_doc_id(ts, sid, doc_id)
    idx = getattr(ts, "_idx", None)
    if idx is None:
        return sid
    loc = getattr(idx, "_node_to_doc_line", {}).get(sid)
    b = getattr(idx, "_bundles", {}).get(resolved) if loc and resolved and loc[0] == resolved else None
    if not b:
        level = address_level(ts, sid)
        if level == NavLevel.DOCUMENT and resolved:
            bb = getattr(idx, "_bundles", {}).get(resolved)
            if bb and bb.lines:
                title = (bb.lines[0].content or "").strip()
                return title if title else sid
        return sid
    _, j = loc
    if j < 0 or j >= len(b.lines):
        return sid
    title = (b.lines[j].content or "").strip()
    return title if title else sid


def _chunk_body(chunk: Chunk) -> str:
    """Strip legacy full-path [§ ...] headers; body only."""
    text = (chunk.text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("[§"):
        return "\n".join(lines[1:]).strip()
    return text


def _line_key(chunk: Chunk) -> Tuple[int, str]:
    lids = list(chunk.line_ids or ())
    if lids:
        return (min(lids), chunk.node_id)
    return (10**9, chunk.node_id)


def _child_final_score(
    chunk: Chunk,
    state: NavState,
    *,
    w_conf: float,
) -> float:
    owner = evidence_owner_section_id(chunk)
    own_unit = unit_score_for_evidence_chunk(chunk, dict(state.unit_scores or {}))
    conf = _clamp01(float((state.collect_confidence or {}).get(owner, 0.0) or 0.0))
    return float(own_unit) + float(w_conf) * conf


def _parent_id_for_compose(ts: ToolSpace, section_id: str, doc_id: str) -> Optional[str]:
    """Resolve a direct parent without materializing a section subtree."""
    parent_fn = getattr(ts, "parent_id", None)
    if callable(parent_fn):
        try:
            parent = parent_fn(section_id)
        except Exception:
            parent = None
        if parent:
            return str(parent)
    return direct_parent_id(ts, section_id, doc_id)


def _header_only_owners(
    owners: set[str], ts: ToolSpace, doc_id: str
) -> set[str]:
    """Find collected owners that are ancestors of another collected owner.

    Walk each owner's parent chain once instead of comparing every owner with
    every other owner.  This keeps compose grouping proportional to the number
    of owners and hierarchy depth, even when a broad collect returns thousands
    of chunks.
    """
    headers: set[str] = set()
    for owner in owners:
        if not owner:
            continue
        owner_doc = _section_doc_id(ts, owner, doc_id)
        current = owner
        for _ in range(64):
            parent = _parent_id_for_compose(ts, current, owner_doc)
            if parent is None:
                break
            if parent in owners and _section_doc_id(ts, parent, doc_id) == owner_doc:
                headers.add(parent)
            current = parent
    return headers


@dataclass
class _ChildItem:
    chunk: Chunk
    owner: str
    score: float
    line_key: Tuple[int, str]


@dataclass
class _ParentGroup:
    parent_id: Optional[str]
    parent_title: str
    children: List[_ChildItem]
    priority: float = 0.0

    @property
    def group_key(self) -> float:
        if not self.children:
            return 0.0
        return max(c.score for c in self.children)

    @property
    def doc_order_key(self) -> Tuple[int, str]:
        if not self.children:
            return (10**9, "")
        return min(c.line_key for c in self.children)



def dedupe_scored(scored: Sequence[Tuple[Chunk, float]]) -> List[Tuple[Chunk, float]]:
    """Keep highest score per chunk.node_id; sort by score descending."""
    best: Dict[str, Tuple[Chunk, float]] = {}
    for c, score in scored:
        nid = str(getattr(c, "node_id", "") or "")
        if not nid:
            continue
        prev = best.get(nid)
        if prev is None or float(score) > float(prev[1]):
            best[nid] = (c, float(score))
    out = list(best.values())
    out.sort(key=lambda x: -x[1])
    return out


def _build_groups(
    scored: Sequence[Tuple[Chunk, float]],
    ts: ToolSpace,
    state: NavState,
    config: NavConfig,
) -> List[_ParentGroup]:
    w_conf = float(getattr(config, "compose_confidence_weight", 0.5) or 0.5)
    seen_ids: set[str] = set()
    items: List[Tuple[Chunk, str, float]] = []
    for chunk, _bag in scored:
        nid = str(getattr(chunk, "node_id", "") or "")
        if not nid or nid in seen_ids:
            continue
        body = _chunk_body(chunk)
        if not body:
            continue
        seen_ids.add(nid)
        owner = evidence_owner_section_id(chunk)
        score = _child_final_score(chunk, state, w_conf=w_conf)
        items.append((chunk, owner, score))

    owners = {owner for _c, owner, _s in items if owner}
    header_owners = _header_only_owners(owners, ts, state.doc_id)

    groups: Dict[Optional[str], _ParentGroup] = {}
    for chunk, owner, score in items:
        if owner in header_owners:
            continue
        owner_doc = _section_doc_id(ts, owner, state.doc_id) or str(
            getattr(chunk, "doc_id", "") or ""
        )
        parent_id = direct_parent_id(ts, owner, owner_doc)
        if parent_id is None:
            parent_id = owner
        if parent_id not in groups:
            title = _section_title(ts, parent_id, owner_doc)
            groups[parent_id] = _ParentGroup(
                parent_id=parent_id,
                parent_title=title,
                children=[],
                priority=float((state.group_priority or {}).get(parent_id, 0.0) or 0.0),
            )
        groups[parent_id].children.append(
            _ChildItem(
                chunk=chunk,
                owner=owner,
                score=score,
                line_key=_line_key(chunk),
            )
        )
    return list(groups.values())


def _render_group(
    group: _ParentGroup,
    selected: Sequence[_ChildItem],
    *,
    evidence_index: int,
) -> str:
    """Render one evidence block via the shared evidence renderer."""
    bodies = [_chunk_body(child.chunk) for child in selected]
    return render_evidence_blocks(
        [(group.parent_title or "", bodies)],
        start_index=evidence_index,
    )


def _scored_flat(groups: Sequence[_ParentGroup]) -> List[Tuple[Chunk, float]]:
    out: List[Tuple[Chunk, float]] = []
    for g in groups:
        for c in sorted(g.children, key=lambda x: (-x.score, x.line_key)):
            out.append((c.chunk, c.score))
    return out


def _n_pool_children(groups: Sequence[_ParentGroup]) -> int:
    return sum(len(g.children) for g in groups)


def _is_explicit_collect(child: _ChildItem, state: NavState) -> bool:
    """True if LLM explicitly COLLECTed this owner (or left non-zero confidence)."""
    owner = str(child.owner or "").strip()
    if not owner:
        return False
    explicit = getattr(state, "explicit_collect_ids", None) or set()
    if owner in explicit:
        return True
    conf = float((state.collect_confidence or {}).get(owner, 0.0) or 0.0)
    return conf > 0.0


def _unit_score(child: _ChildItem, state: NavState) -> float:
    return float(
        unit_score_for_evidence_chunk(child.chunk, dict(state.unit_scores or {}))
    )


def _render_kept(
    groups: Sequence[_ParentGroup],
    kept_ids: Set[str],
    *,
    budget_chars: int,
    min_partial_chars: int = 20,
) -> ComposeFillResult:
    """Render kept children in group order (doc order within group), full text."""
    scored_flat = _scored_flat(groups)
    parts: List[str] = []
    kept_chunks: List[Chunk] = []
    used = 0
    truncated_last = False
    sep = "\n\n"
    n_pool = _n_pool_children(groups)

    for g in groups:
        entries = [
            c
            for c in sorted(g.children, key=lambda x: x.line_key)
            if c.chunk.node_id in kept_ids
        ]
        if not entries:
            continue
        block = _render_group(
            g, entries, evidence_index=len(parts) + 1
        )
        add = len(block) + (len(sep) if parts else 0)
        if used + add <= budget_chars:
            parts.append(block)
            kept_chunks.extend(c.chunk for c in entries)
            used += add
            continue
        remain = budget_chars - used - (len(sep) if parts else 0)
        if remain >= min_partial_chars and not parts:
            parts.append(block[:budget_chars])
            kept_chunks.extend(c.chunk for c in entries)
            used = budget_chars
            truncated_last = True
            break
        if remain >= min_partial_chars:
            parts.append(block[:remain])
            kept_chunks.extend(c.chunk for c in entries)
            used = budget_chars
            truncated_last = True
        break

    text = sep.join(parts)
    return ComposeFillResult(
        kept_chunks=kept_chunks,
        evidence_text=text,
        evidence_chars_actual=len(text),
        n_chunks_kept=len(kept_chunks),
        truncated_last=truncated_last,
        scored_chunks=scored_flat,
        dropped_any=len(kept_chunks) < n_pool or truncated_last,
    )


def _count_power10_boundaries(lower: int, upper: int) -> int:
    """Count decimal digit-boundary positions in ``(lower, upper]``."""
    if upper <= lower:
        return 0
    boundary = 10
    count = 0
    while boundary <= upper:
        if boundary > lower:
            count += 1
        boundary *= 10
    return count


class _SelectionLength:
    """Maintain exact rendered selection length under one-child mutations."""

    def __init__(self, groups: Sequence[_ParentGroup], kept_ids: Set[str]) -> None:
        self._groups = groups
        self.kept_ids = kept_ids
        self._group_by_node_id: Dict[str, int] = {}
        self._plain_lengths: Dict[str, int] = {}
        self._indented_lengths: Dict[str, int] = {}
        self._counts: List[int] = []
        self._plain_totals: List[int] = []
        self._indented_totals: List[int] = []
        self._nonempty_count = 0
        self.total = 0

        for group_index, group in enumerate(groups):
            plain_total = 0
            indented_total = 0
            count = 0
            for child in group.children:
                node_id = str(child.chunk.node_id or "")
                if not node_id:
                    continue
                body = _chunk_body(child.chunk)
                plain_length = len(body)
                indented_length = len(
                    "\n".join(
                        ("  " + line if line.strip() else line)
                        for line in body.splitlines()
                    )
                )
                self._group_by_node_id[node_id] = group_index
                self._plain_lengths[node_id] = plain_length
                self._indented_lengths[node_id] = indented_length
                if node_id in kept_ids:
                    count += 1
                    plain_total += plain_length
                    indented_total += indented_length
            self._counts.append(count)
            self._plain_totals.append(plain_total)
            self._indented_totals.append(indented_total)

        self._recalculate_total()

    @staticmethod
    def _block_length(
        group: _ParentGroup,
        *,
        count: int,
        plain_total: int,
        indented_total: int,
        evidence_index: int,
    ) -> int:
        if count <= 0:
            return 0
        prefix = len(f"[E{evidence_index}]")
        if group.parent_title:
            prefix += 1 + len(f"[§ {group.parent_title}]")
        body_length = indented_total if count >= 2 else plain_total
        return prefix + count + body_length

    def _recalculate_total(self) -> None:
        total = 0
        evidence_index = 0
        for index, group in enumerate(self._groups):
            count = self._counts[index]
            if count <= 0:
                continue
            evidence_index += 1
            total += self._block_length(
                group,
                count=count,
                plain_total=self._plain_totals[index],
                indented_total=self._indented_totals[index],
                evidence_index=evidence_index,
            )
        self._nonempty_count = evidence_index
        self.total = total + max(0, evidence_index - 1) * 2

    def _evidence_index(self, group_index: int) -> int:
        return sum(1 for count in self._counts[:group_index] if count > 0) + 1

    def remove(self, node_id: str) -> None:
        group_index = self._group_by_node_id.get(node_id)
        if group_index is None or node_id not in self.kept_ids:
            return
        old_count = self._counts[group_index]
        old_index = self._evidence_index(group_index)
        old_block = self._block_length(
            self._groups[group_index],
            count=old_count,
            plain_total=self._plain_totals[group_index],
            indented_total=self._indented_totals[group_index],
            evidence_index=old_index,
        )
        self.kept_ids.remove(node_id)
        self._counts[group_index] -= 1
        self._plain_totals[group_index] -= self._plain_lengths[node_id]
        self._indented_totals[group_index] -= self._indented_lengths[node_id]
        if old_count > 1:
            new_block = self._block_length(
                self._groups[group_index],
                count=old_count - 1,
                plain_total=self._plain_totals[group_index],
                indented_total=self._indented_totals[group_index],
                evidence_index=old_index,
            )
            self.total += new_block - old_block
            return

        self.total -= old_block
        if self._nonempty_count > 1:
            self.total -= 2
        self.total -= _count_power10_boundaries(old_index, self._nonempty_count)
        self._nonempty_count -= 1

    def add(self, node_id: str) -> bool:
        group_index = self._group_by_node_id.get(node_id)
        if group_index is None or node_id in self.kept_ids:
            return False
        old_count = self._counts[group_index]
        evidence_index = self._evidence_index(group_index)
        if old_count > 0:
            old_block = self._block_length(
                self._groups[group_index],
                count=old_count,
                plain_total=self._plain_totals[group_index],
                indented_total=self._indented_totals[group_index],
                evidence_index=evidence_index,
            )

        self.kept_ids.add(node_id)
        self._counts[group_index] += 1
        self._plain_totals[group_index] += self._plain_lengths[node_id]
        self._indented_totals[group_index] += self._indented_lengths[node_id]
        new_block = self._block_length(
            self._groups[group_index],
            count=self._counts[group_index],
            plain_total=self._plain_totals[group_index],
            indented_total=self._indented_totals[group_index],
            evidence_index=evidence_index,
        )
        if old_count > 0:
            self.total += new_block - old_block
        else:
            old_nonempty_count = self._nonempty_count
            self.total += new_block
            if old_nonempty_count > 0:
                self.total += 2
            self.total += _count_power10_boundaries(
                evidence_index, old_nonempty_count + 1
            )
            self._nonempty_count += 1
        return True


def _selection_chars(
    groups: Sequence[_ParentGroup], kept_ids: Set[str]
) -> int:
    """Exact rendered length of the current full-text selection."""
    return _SelectionLength(groups, kept_ids).total


def _refill_to_budget(
    groups: Sequence[_ParentGroup],
    kept_ids: Set[str],
    state: NavState,
    *,
    budget_chars: int,
) -> None:
    """Re-add dropped children, best first, while the rendered selection still fits.

    Dropping runs back-to-front and stops the moment the selection fits, so whole
    tail groups can be gone while a large slice of the budget sits unused. Without
    this pass a single oversized chunk near the cut point strands everything behind
    it (observed: 4 chunks kept, 5.7k of 12k budget idle, 414 dropped chunks that
    each still fit).
    """
    dropped = [
        c
        for g in groups
        for c in g.children
        if c.chunk.node_id and c.chunk.node_id not in kept_ids
    ]
    if not dropped:
        return
    dropped.sort(
        key=lambda c: (
            not _is_explicit_collect(c, state),
            -_unit_score(c, state),
            c.line_key,
        )
    )
    selection = _SelectionLength(groups, kept_ids)
    for child in dropped:
        if selection.total >= budget_chars:
            break
        if len(child.chunk.text or "") > budget_chars - selection.total:
            continue
        if not selection.add(child.chunk.node_id):
            continue
        if selection.total > budget_chars:
            selection.remove(child.chunk.node_id)


def _pack_trim(
    groups: List[_ParentGroup],
    state: NavState,
    *,
    budget_chars: int,
    min_partial_chars: int = 20,
) -> ComposeFillResult:
    """Keep group order; drop back-to-front until under budget, then refill slack.

    Phase 1: drop non-explicit COLLECT owners (later groups first, later doc first).
    Phase 2: drop remaining by lowest unit score (later groups first).
    Phase 3: re-add dropped children best-first while they still fit.
    """
    scored_flat = _scored_flat(groups)
    if budget_chars <= 0 or not groups:
        return ComposeFillResult([], "", 0, 0, False, scored_flat, dropped_any=False)

    kept_ids: Set[str] = {
        c.chunk.node_id for g in groups for c in g.children if c.chunk.node_id
    }
    if not kept_ids:
        return ComposeFillResult([], "", 0, 0, False, scored_flat, dropped_any=False)

    selection = _SelectionLength(groups, kept_ids)

    def fits() -> bool:
        return selection.total <= budget_chars

    if fits():
        return _render_kept(
            groups, kept_ids, budget_chars=budget_chars, min_partial_chars=min_partial_chars
        )

    # Phase 1: non-explicit, back group → front; within group, later doc first.
    for g in reversed(groups):
        candidates = [
            c
            for c in g.children
            if c.chunk.node_id in kept_ids and not _is_explicit_collect(c, state)
        ]
        candidates.sort(key=lambda c: c.line_key, reverse=True)
        for child in candidates:
            if fits():
                break
            selection.remove(child.chunk.node_id)
        if fits():
            break

    # Phase 2: still over → lowest unit first, back group → front.
    if not fits():
        for g in reversed(groups):
            candidates = [c for c in g.children if c.chunk.node_id in kept_ids]
            candidates.sort(
                key=lambda c: (_unit_score(c, state), c.line_key)
            )
            for child in candidates:
                if fits():
                    break
                if len(kept_ids) <= 1:
                    break
                selection.remove(child.chunk.node_id)
            if fits() or len(kept_ids) <= 1:
                break

    _refill_to_budget(groups, kept_ids, state, budget_chars=budget_chars)

    return _render_kept(
        groups, kept_ids, budget_chars=budget_chars, min_partial_chars=min_partial_chars
    )



def pack_nav_evidence(
    collected: Sequence[Tuple[Chunk, float]],
    ts: ToolSpace,
    state: NavState,
    config: NavConfig,
    *,
    budget_chars: int,
    min_partial_chars: int = 20,
) -> ComposeFillResult:
    """Pack collected evidence under parent scopes into a budgeted tree string.

    Only progressive trim+refill (``_pack_trim``). Waterfill/greedy paths removed.
    """
    if budget_chars <= 0:
        return ComposeFillResult([], "", 0, 0, False, [], dropped_any=False)

    groups = _build_groups(collected, ts, state, config)
    groups.sort(key=lambda g: (-g.priority, -g.group_key, g.doc_order_key))
    return _pack_trim(
        groups,
        state,
        budget_chars=budget_chars,
        min_partial_chars=min_partial_chars,
    )


def parse_collect_confidence(
    obj: Dict[str, Any],
    selected: Sequence[Any],
) -> Dict[str, float]:
    """Map selected LegalActions -> confidence in [0,1]. Missing => 0."""
    raw = (obj or {}).get("confidence")
    out: Dict[str, float] = {}
    if isinstance(raw, (int, float)):
        c = _clamp01(float(raw))
        for a in selected:
            aid = str(getattr(a, "action_id", "") or "").strip().upper()
            if aid:
                out[aid] = c
        return out
    if isinstance(raw, dict):
        norm = {str(k).strip().upper(): v for k, v in raw.items()}
        for a in selected:
            aid = str(getattr(a, "action_id", "") or "").strip().upper()
            if not aid:
                continue
            if aid in norm and norm[aid] is not None:
                try:
                    out[aid] = _clamp01(float(norm[aid]))
                except (TypeError, ValueError):
                    out[aid] = 0.0
            else:
                out[aid] = 0.0
        return out
    for a in selected:
        aid = str(getattr(a, "action_id", "") or "").strip().upper()
        if aid:
            out[aid] = 0.0
    return out
