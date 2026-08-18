"""Locate hierarchy titles on PDF pages and resolve page ranges.

Deterministic title anchoring and range assembly. Leaf starts come from
offset-guided ``match_overrides`` or per-line strict exact. Null-page parents
are located upstream via compact-strict (cross-line) + optional VLM, then
resolved here including parent self-only spans for interstitial pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_heading_text,
)

TitleMatchSource = Literal[
    "anchored",
    "bulk_offset",
    "inspect_vlm",
    "physical_hint",
]


_ROMAN_MAP = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def classify_page_number_kind(label: Any) -> str:
    text = str(label or "").strip()
    if not text:
        return "other"
    if re.fullmatch(r"\d+", text):
        return "decimal"
    if re.fullmatch(r"[ivxlcdm]+", text, flags=re.IGNORECASE):
        return "roman"
    if re.fullmatch(r"[A-Za-z]+-\d+", text):
        return "prefixed"
    return "other"


def normalize_page_kind(kind: str) -> str:
    text = (kind or "other").strip().lower()
    if text in {"arabic", "arabic_digits", "decimal"}:
        return "decimal"
    if text == "roman":
        return "roman"
    if text in {"prefixed", "folio"}:
        return "prefixed"
    return text or "other"


def parse_printed_page(label: Any, *, kind: str) -> int | None:
    text = str(label or "").strip()
    if not text:
        return None
    kind_l = normalize_page_kind(kind)
    if kind_l == "decimal":
        return int(text) if text.isdigit() else None
    if kind_l == "roman":
        return _roman_to_int(text)
    if kind_l == "prefixed":
        match = re.fullmatch(r"[A-Za-z]+-(\d+)", text)
        return int(match.group(1)) if match else None
    if text.isdigit():
        return int(text)
    if re.fullmatch(r"[ivxlcdm]+", text, flags=re.IGNORECASE):
        return _roman_to_int(text)
    match = re.fullmatch(r"[A-Za-z]+-(\d+)", text)
    return int(match.group(1)) if match else None


def _roman_to_int(text: str) -> int | None:
    raw = text.strip().lower()
    if not raw or not re.fullmatch(r"[ivxlcdm]+", raw):
        return None
    total = 0
    prev = 0
    for ch in reversed(raw):
        value = _ROMAN_MAP.get(ch)
        if value is None:
            return None
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total if total > 0 else None


@dataclass(frozen=True)
class PageRange:
    start: int
    end: int

    def pages(self) -> list[int]:
        if self.end < self.start:
            return []
        return list(range(self.start, self.end + 1))


@dataclass(frozen=True)
class TitleNode:
    title: str
    level: int
    printed_page: int | None = None
    printed_label: str | None = None
    page_kind: str | None = None
    physical_page_hint: int | None = None
    children: list["TitleNode"] = field(default_factory=list)


@dataclass(frozen=True)
class TitleMatch:
    page: int
    confidence: float
    source: TitleMatchSource
    matched_line: str
    score: float
    candidates: list[int]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedHierarchyRange:
    title: str
    level: int
    start_page: int
    end_page: int
    path_titles: tuple[str, ...]
    match: TitleMatch | None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _LineHit:
    page: int
    line_index: int
    line: str
    source: TitleMatchSource
    score: float


def locate_title_strict_exact(
    title: str,
    *,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> TitleMatch | None:
    """Return a direct anchor only when a cleaned heading line has one page hit."""
    hits = _find_anchored_hits(title, scope_pages, page_texts)
    pages = sorted({hit.page for hit in hits})
    if len(pages) != 1:
        return None
    return _choose_best_hit(
        hits,
        source="anchored",
        extra_evidence={"accept": "strict_exact_unique"},
    )


def locate_title_compact_strict(
    title: str,
    *,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> TitleMatch | None:
    """Locate *title* after cross-line compact cleanup; accept only a unique page.

    Pipeline: compact(page text) → contiguous strict match of compact(title) →
    accept iff exactly one page in ``scope_pages`` hits. Handles PyMuPDF line
    splits; does not use token/normalized weak matching.
    """
    needle = _compact_match_text(clean_toc_title(title) or title)
    if not needle or not scope_pages:
        return None

    hit_pages: list[int] = []
    matched_preview = ""
    for page in scope_pages:
        haystack = _compact_match_text(page_texts.get(page, ""))
        if not haystack or needle not in haystack:
            continue
        hit_pages.append(page)
        if not matched_preview:
            matched_preview = needle[:160]

    unique_pages = sorted(set(hit_pages))
    if len(unique_pages) != 1:
        return None

    page = unique_pages[0]
    return TitleMatch(
        page=page,
        confidence=0.92,
        source="anchored",
        matched_line=matched_preview,
        score=0.96,
        candidates=[page],
        evidence={"accept": "compact_strict_unique"},
    )


def last_leaf_start_under(
    node: TitleNode,
    parent_titles: tuple[str, ...],
    match_overrides: dict[tuple[str, ...], TitleMatch],
) -> int | None:
    """Max start page among located leaves under *node*; None if none located."""
    max_page: int | None = None
    for leaf_path, _leaf in iter_leaf_title_nodes([node], parent_titles=parent_titles):
        match = match_overrides.get(leaf_path)
        if match is None:
            continue
        if max_page is None or match.page > max_page:
            max_page = match.page
    return max_page


def first_leaf_start_under(
    node: TitleNode,
    parent_titles: tuple[str, ...],
    match_overrides: dict[tuple[str, ...], TitleMatch],
) -> int | None:
    """Min start page among located leaves under *node*; None if none located."""
    min_page: int | None = None
    for leaf_path, _leaf in iter_leaf_title_nodes([node], parent_titles=parent_titles):
        match = match_overrides.get(leaf_path)
        if match is None:
            continue
        if min_page is None or match.page < min_page:
            min_page = match.page
    return min_page


def resolve_hierarchy_page_ranges(
    nodes: list[TitleNode],
    *,
    page_count: int,
    page_texts: dict[int, str],
    body_pages: list[int] | None = None,
    match_overrides: dict[tuple[str, ...], TitleMatch] | None = None,
) -> list[ResolvedHierarchyRange]:
    """Resolve hierarchy nodes into closed page ranges.

    Emits leaf ranges and parent self-only spans when a parent start is strictly
    before its first located descendant leaf. Ranges are closed-closed.
    """
    if page_count <= 0 or not nodes:
        return []

    pages = sorted(set(body_pages or list(range(1, page_count + 1))))
    pages = [page for page in pages if 1 <= page <= page_count]
    if not pages:
        return []

    allowed_pages = set(pages)
    scope = PageRange(start=pages[0], end=pages[-1])
    resolved: list[ResolvedHierarchyRange] = []
    _resolve_siblings(
        nodes,
        parent_scope=scope,
        allowed_pages=allowed_pages,
        parent_titles=(),
        page_texts=page_texts,
        match_overrides=match_overrides or {},
        resolved=resolved,
    )
    return resolved


def coverage_by_path(
    ranges: list[ResolvedHierarchyRange],
) -> dict[tuple[str, ...], tuple[int, int]]:
    """Aggregate resolved ranges into one closed span per ancestor path.

    ``resolve_hierarchy_page_ranges`` emits leaves (plus parent self-only
    spans), so an ancestor's span is the union of its descendants' ranges.
    """
    coverage: dict[tuple[str, ...], tuple[int, int]] = {}
    for item in ranges:
        for depth in range(1, len(item.path_titles) + 1):
            path = item.path_titles[:depth]
            span = coverage.get(path)
            if span is None:
                coverage[path] = (item.start_page, item.end_page)
            else:
                coverage[path] = (
                    min(span[0], item.start_page),
                    max(span[1], item.end_page),
                )
    return coverage


def deepest_covering_path(
    coverage: dict[tuple[str, ...], tuple[int, int]],
    *,
    start: int,
    end: int,
) -> tuple[str, ...] | None:
    """Deepest path whose span contains the closed window ``[start, end]``."""
    found: tuple[str, ...] | None = None
    for path, span in coverage.items():
        if span[0] <= start and end <= span[1]:
            if found is None or len(path) > len(found):
                found = path
    return found


def extract_toc_nodes(toc_hierarchies: list[dict[str, Any]] | None) -> list[TitleNode]:
    """Build a title tree from supported TOC hierarchy payloads."""
    flat_entries: list[dict[str, Any]] = []
    for hierarchy in toc_hierarchies or []:
        entries = _extract_flat_entries(hierarchy.get("toc_with_level"))
        if not entries and hierarchy.get("toc_tree"):
            entries = _flatten_tree_entries(hierarchy["toc_tree"])
        flat_entries.extend(entries)
    return _entries_to_tree(flat_entries)


def _resolve_siblings(
    nodes: list[TitleNode],
    *,
    parent_scope: PageRange,
    allowed_pages: set[int],
    parent_titles: tuple[str, ...],
    page_texts: dict[int, str],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    resolved: list[ResolvedHierarchyRange],
) -> None:
    located: list[tuple[TitleNode, int, TitleMatch | None]] = []
    lower_bound = parent_scope.start

    for index, node in enumerate(nodes):
        path_titles = (*parent_titles, node.title)
        pages = _allowed_pages_between(lower_bound, parent_scope.end, allowed_pages)
        match = _locate_match_for_node(
            node,
            path_titles=path_titles,
            scope_pages=pages,
            page_texts=page_texts,
            match_overrides=match_overrides,
        )
        if match is None:
            start_page = lower_bound
        else:
            start_page = max(parent_scope.start, min(match.page, parent_scope.end))
        located.append((node, start_page, match))
        if match is not None:
            lower_bound = start_page
        elif index + 1 < len(nodes):
            next_match = _find_next_located_sibling(
                nodes=nodes,
                start_index=index + 1,
                lower_bound=lower_bound,
                parent_end=parent_scope.end,
                allowed_pages=allowed_pages,
                page_texts=page_texts,
                match_overrides=match_overrides,
                parent_titles=parent_titles,
            )
            if next_match is not None:
                lower_bound = next_match.page

    for index, (node, start_page, match) in enumerate(located):
        next_start = _next_located_start(located, index + 1)
        end_page = next_start if next_start is not None else parent_scope.end
        if end_page < start_page:
            end_page = start_page

        path_titles = (*parent_titles, node.title)
        evidence = _range_evidence(match)
        if match is None:
            evidence.update(
                _unlocated_warning_evidence(
                    title=node.title,
                    path_titles=path_titles,
                    start_page=start_page,
                    end_page=end_page,
                    parent_scope=parent_scope,
                )
            )

        if node.children:
            first_child_start = first_leaf_start_under(
                node, parent_titles, match_overrides
            )
            if (
                match is not None
                and first_child_start is not None
                and start_page < first_child_start
            ):
                resolved.append(
                    ResolvedHierarchyRange(
                        title=node.title,
                        level=node.level,
                        start_page=start_page,
                        end_page=first_child_start,
                        path_titles=path_titles,
                        match=match,
                        evidence={**evidence, "skeleton_kind": "parent_self_only"},
                    )
                )
            _resolve_siblings(
                node.children,
                parent_scope=PageRange(start_page, end_page),
                allowed_pages=allowed_pages,
                parent_titles=path_titles,
                page_texts=page_texts,
                match_overrides=match_overrides,
                resolved=resolved,
            )
            continue

        resolved.append(
            ResolvedHierarchyRange(
                title=node.title,
                level=node.level,
                start_page=start_page,
                end_page=end_page,
                path_titles=path_titles,
                match=match,
                evidence=evidence,
            )
        )


def _locate_match_for_node(
    node: TitleNode,
    *,
    path_titles: tuple[str, ...],
    scope_pages: list[int],
    page_texts: dict[int, str],
    match_overrides: dict[tuple[str, ...], TitleMatch],
) -> TitleMatch | None:
    match = _match_override(path_titles, match_overrides, scope_pages)
    if match is not None:
        return match
    match = _match_physical_hint(node=node, scope_pages=scope_pages)
    if match is not None:
        return match
    if node.children:
        # Parent active locate is upstream (compact-strict / visual). Wide-window
        # strict_exact is intentionally not used here.
        return _infer_start_from_descendant_overrides(
            node, parent_titles=path_titles[:-1], match_overrides=match_overrides,
            scope_pages=scope_pages,
        )
    return locate_title_strict_exact(
        node.title,
        scope_pages=scope_pages,
        page_texts=page_texts,
    )


def _find_next_located_sibling(
    *,
    nodes: list[TitleNode],
    start_index: int,
    lower_bound: int,
    parent_end: int,
    allowed_pages: set[int],
    page_texts: dict[int, str],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    parent_titles: tuple[str, ...],
) -> TitleMatch | None:
    pages = _allowed_pages_between(lower_bound, parent_end, allowed_pages)
    for sibling in nodes[start_index:]:
        path_titles = (*parent_titles, sibling.title)
        match = _locate_match_for_node(
            sibling,
            path_titles=path_titles,
            scope_pages=pages,
            page_texts=page_texts,
            match_overrides=match_overrides,
        )
        if match is not None:
            return match
    return None


def _infer_start_from_descendant_overrides(
    node: TitleNode,
    parent_titles: tuple[str, ...],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    scope_pages: list[int],
) -> TitleMatch | None:
    """Final fallback: parent start = earliest located descendant leaf page."""
    if not node.children or not match_overrides:
        return None
    leaves = iter_leaf_title_nodes([node], parent_titles=parent_titles)
    min_page: int | None = None
    min_match: TitleMatch | None = None
    for leaf_path, _leaf_node in leaves:
        m = match_overrides.get(leaf_path)
        if m is None:
            continue
        if m.page not in scope_pages:
            continue
        if min_page is None or m.page < min_page:
            min_page = m.page
            min_match = m
    if min_match is None:
        return None
    return TitleMatch(
        page=min_match.page,
        confidence=min(min_match.confidence, 0.80),
        source=min_match.source,
        matched_line="",
        score=min(min_match.score, 0.80),
        candidates=[min_match.page],
        evidence={
            "inferred_from": "descendant_leaf_override",
            "original_confidence": min_match.confidence,
            "status": "degraded",
        },
    )


def _match_override(
    path_titles: tuple[str, ...],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    scope_pages: list[int],
) -> TitleMatch | None:
    match = match_overrides.get(path_titles)
    if match is None or match.page not in scope_pages:
        return None
    return match


def iter_leaf_title_nodes(
    nodes: list[TitleNode],
    *,
    parent_titles: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], TitleNode]]:
    leaves: list[tuple[tuple[str, ...], TitleNode]] = []
    for node in nodes:
        path_titles = (*parent_titles, node.title)
        if node.children:
            leaves.extend(iter_leaf_title_nodes(node.children, parent_titles=path_titles))
        else:
            leaves.append((path_titles, node))
    return leaves


def _next_located_start(
    located: list[tuple[TitleNode, int, TitleMatch | None]],
    start_index: int,
) -> int | None:
    for _node, start_page, match in located[start_index:]:
        if match is not None:
            return start_page
    return None


def _range_evidence(match: TitleMatch | None) -> dict[str, Any]:
    if match is None:
        return {"source": "unlocated", "confidence": 0.0, "candidates": []}
    return {
        "source": match.source,
        "confidence": match.confidence,
        "matched_line": match.matched_line,
        "candidates": match.candidates,
        "score": match.score,
        **match.evidence,
    }


def _unlocated_warning_evidence(
    *,
    title: str,
    path_titles: tuple[str, ...],
    start_page: int,
    end_page: int,
    parent_scope: PageRange,
) -> dict[str, Any]:
    warning = {
        "code": "section_title_unlocated",
        "title": title,
        "path_titles": list(path_titles),
        "assigned_range": [start_page, end_page],
        "parent_scope": [parent_scope.start, parent_scope.end],
        "message": (
            "Section title was not found on any allowed body page; assigned range "
            "from neighboring hierarchy boundaries."
        ),
    }
    return {
        "status": "inherited_unlocated",
        "warning": warning,
        "warnings": [warning],
    }


def _match_physical_hint(
    *,
    node: TitleNode,
    scope_pages: list[int],
) -> TitleMatch | None:
    if node.physical_page_hint is None or node.physical_page_hint not in scope_pages:
        return None
    return TitleMatch(
        page=node.physical_page_hint,
        confidence=0.88,
        source="physical_hint",
        matched_line="",
        score=0.88,
        candidates=[node.physical_page_hint],
        evidence={"physical_page_hint": node.physical_page_hint},
    )


def _allowed_pages_between(start: int, end: int, allowed_pages: set[int]) -> list[int]:
    if end < start:
        return []
    return [page for page in range(start, end + 1) if page in allowed_pages]


def _compact_match_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_heading_text(text)).casefold()


def _find_anchored_hits(
    title: str,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> list[_LineHit]:
    hits: list[_LineHit] = []
    needle = normalize_heading_text(clean_toc_title(title) or title).casefold()
    if not needle:
        return hits
    for page, line_index, line in _iter_lines(scope_pages, page_texts):
        cleaned_line = normalize_heading_text(clean_toc_title(line)).casefold()
        if cleaned_line == needle:
            hits.append(
                _LineHit(
                    page=page,
                    line_index=line_index,
                    line=line.strip(),
                    source="anchored",
                    score=_line_score(line=line, line_index=line_index, base=0.96),
                )
            )
    return hits


def _choose_best_hit(
    hits: list[_LineHit],
    *,
    source: TitleMatchSource,
    extra_evidence: dict[str, Any] | None = None,
) -> TitleMatch:
    ordered = sorted(
        hits,
        key=lambda hit: (hit.score, -hit.line_index, -hit.page),
        reverse=True,
    )
    best = ordered[0]
    pages = sorted({hit.page for hit in ordered})
    confidence_by_source = {
        "anchored": 0.92,
        "bulk_offset": 0.90,
        "physical_hint": 0.88,
        "inspect_vlm": 0.75,
    }
    return TitleMatch(
        page=best.page,
        confidence=confidence_by_source[source],
        source=source,
        matched_line=best.line[:160],
        score=best.score,
        candidates=pages,
        evidence={
            "line_index": best.line_index,
            "candidate_count": len(pages),
            **(extra_evidence or {}),
        },
    )


def _line_score(*, line: str, line_index: int, base: float) -> float:
    stripped = normalize_heading_text(line)
    short_line_bonus = max(0.0, 1.0 - (len(stripped) / 140.0))
    top_bonus = max(0.0, 1.0 - (line_index / 18.0))
    return base + short_line_bonus * 0.12 + top_bonus * 0.1


def _iter_lines(
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    for page in scope_pages:
        for line_index, line in enumerate(page_texts.get(page, "").splitlines()):
            if line.strip():
                rows.append((page, line_index, line))
    return rows


def _extract_flat_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [
            entry
            for entry in payload
            if isinstance(entry, dict) and entry.get("heading")
        ]
    if not isinstance(payload, str):
        return []
    return _parse_markdown_toc_entries(payload)


def _parse_markdown_toc_entries(markdown: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    headers: list[str] | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if headers is None:
            headers = [cell.lower() for cell in cells]
            continue
        row = dict(zip(headers, cells))
        level = _safe_int(row.get("level"))
        heading = row.get("heading")
        if heading and level:
            raw_page = row.get("page_number")
            entries.append(
                {
                    "heading": heading,
                    "level": level,
                    # Preserve raw label (roman / prefixed / decimal); parsing is
                    # regime-aware at TitleNode construction time.
                    "page_number": raw_page.strip()
                    if isinstance(raw_page, str)
                    else raw_page,
                }
            )
    return entries


def _flatten_tree_entries(
    tree: dict[str, Any],
    *,
    level: int = 1,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for title, children in tree.items():
        entries.append({"heading": title, "level": level})
        if isinstance(children, dict):
            entries.extend(_flatten_tree_entries(children, level=level + 1))
    return entries


def _entries_to_tree(entries: list[dict[str, Any]]) -> list[TitleNode]:
    roots: list[TitleNode] = []
    stack: list[tuple[int, TitleNode]] = []

    for entry in entries:
        # Keep original TOC heading (incl. numbering). Prefix stripping belongs
        # only in text/compact match helpers used for null-page parents.
        title = normalize_heading_text(str(entry.get("heading") or ""))
        level = _safe_int(entry.get("level")) or 1
        if not title or len(title) < 2:
            continue
        raw_label = entry.get("page_number")
        printed_label = (
            None
            if raw_label is None or raw_label == ""
            else str(raw_label).strip()
        )
        page_kind = classify_page_number_kind(printed_label) if printed_label else None
        printed_page = (
            parse_printed_page(printed_label, kind=page_kind or "other")
            if printed_label
            else None
        )
        node = TitleNode(
            title=title,
            level=level,
            printed_page=printed_page,
            printed_label=printed_label,
            page_kind=page_kind,
        )
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))

    return roots


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collapse_intermediate_single_child_chains(
    nodes: list[TitleNode],
) -> list[TitleNode]:
    """Collapse single-child chains of intermediate (non-leaf) nodes.

    Leaf nodes (children=[]) are never absorbed into their parent title.
    Shared by page_memory C4 and calibration finalize.
    """
    from dataclasses import replace as _replace

    def _collapse(node: TitleNode) -> TitleNode:
        collapsed_children = [_collapse(c) for c in node.children]

        if len(collapsed_children) == 1:
            only_child = collapsed_children[0]
            if only_child.children:
                merged_title = f"{node.title} {only_child.title}"
                merged_printed_page = only_child.printed_page or node.printed_page
                merged_printed_label = only_child.printed_label or node.printed_label
                merged_page_kind = only_child.page_kind or node.page_kind
                merged_physical_hint = (
                    only_child.physical_page_hint or node.physical_page_hint
                )
                promoted = [
                    _replace(gc, level=max(1, gc.level - 1))
                    for gc in only_child.children
                ]
                return _replace(
                    node,
                    title=merged_title,
                    printed_page=merged_printed_page,
                    printed_label=merged_printed_label,
                    page_kind=merged_page_kind,
                    physical_page_hint=merged_physical_hint,
                    children=promoted,
                )

        return _replace(node, children=collapsed_children)

    return [_collapse(n) for n in nodes]
