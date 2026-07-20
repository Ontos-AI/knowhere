"""Locate hierarchy titles on PDF pages and resolve page ranges.

This module is intentionally deterministic: it performs strict title anchoring
and range assembly. Titles are accepted only via strict exact anchoring or
offset-guided ``match_overrides``; weaker candidate collection has been removed
in favor of VLM-verified calibration upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_heading_text,
)

TitleMatchSource = Literal[
    "anchored",
    "h1_result",
    "agent_vlm",
    "agent_heuristic",
]


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
        printed_page=None,
        page_offset_hint=None,
        extra_evidence={"accept": "strict_exact_unique"},
    )


def resolve_hierarchy_page_ranges(
    nodes: list[TitleNode],
    *,
    page_count: int,
    page_texts: dict[int, str],
    body_pages: list[int] | None = None,
    page_offset_hint: int | None = None,
    match_overrides: dict[tuple[str, ...], TitleMatch] | None = None,
) -> list[ResolvedHierarchyRange]:
    """Resolve leaf hierarchy nodes into closed page ranges.

    The emitted ranges are leaf-first and intentionally closed-closed: if the
    next leaf starts on page N, the previous leaf may also include page N. This
    preserves page-to-section many-to-many mapping for dense documents.
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
        page_offset_hint=page_offset_hint,
        match_overrides=match_overrides or {},
        resolved=resolved,
    )
    return resolved


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
    page_offset_hint: int | None,
    match_overrides: dict[tuple[str, ...], TitleMatch],
    resolved: list[ResolvedHierarchyRange],
) -> None:
    located: list[tuple[TitleNode, int, TitleMatch | None]] = []
    lower_bound = parent_scope.start

    for index, node in enumerate(nodes):
        path_titles = (*parent_titles, node.title)
        pages = _allowed_pages_between(lower_bound, parent_scope.end, allowed_pages)
        match = _match_override(path_titles, match_overrides, pages)
        if match is None:
            match = _match_physical_hint(node=node, scope_pages=pages)
        if match is None:
            match = locate_title_strict_exact(
                node.title,
                scope_pages=pages,
                page_texts=page_texts,
            )
        if match is None and node.children and match_overrides:
            match = _infer_start_from_descendant_overrides(
                node, parent_titles, match_overrides, pages,
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
                page_offset_hint=page_offset_hint,
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
            _resolve_siblings(
                node.children,
                parent_scope=PageRange(start_page, end_page),
                allowed_pages=allowed_pages,
                parent_titles=path_titles,
                page_texts=page_texts,
                page_offset_hint=page_offset_hint,
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


def _find_next_located_sibling(
    *,
    nodes: list[TitleNode],
    start_index: int,
    lower_bound: int,
    parent_end: int,
    allowed_pages: set[int],
    page_texts: dict[int, str],
    page_offset_hint: int | None,
    match_overrides: dict[tuple[str, ...], TitleMatch],
    parent_titles: tuple[str, ...],
) -> TitleMatch | None:
    pages = _allowed_pages_between(lower_bound, parent_end, allowed_pages)
    for sibling in nodes[start_index:]:
        path_titles = (*parent_titles, sibling.title)
        match = _match_override(path_titles, match_overrides, pages)
        if match is None:
            match = _match_physical_hint(node=sibling, scope_pages=pages)
        if match is not None:
            return match
        match = locate_title_strict_exact(
            sibling.title,
            scope_pages=pages,
            page_texts=page_texts,
        )
        if match is None and sibling.children and match_overrides:
            match = _infer_start_from_descendant_overrides(
                sibling, parent_titles, match_overrides, pages,
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
    """Infer a parent node's start page from its earliest located descendant leaf.

    When a non-leaf node cannot be directly located (no printed_page, no grep
    match), its descendant leaves may already be in match_overrides from
    offset-guided bulk anchoring. Use the minimum page among those descendants
    as a synthetic match so the resolver can cap the previous sibling's end_page.
    """
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
        source="h1_result",
        matched_line="",
        score=0.88,
        candidates=[node.physical_page_hint],
        evidence={"physical_page_hint": node.physical_page_hint},
    )


def _allowed_pages_between(start: int, end: int, allowed_pages: set[int]) -> list[int]:
    if end < start:
        return []
    return [page for page in range(start, end + 1) if page in allowed_pages]


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
    printed_page: int | None,
    page_offset_hint: int | None,
    extra_evidence: dict[str, Any] | None = None,
) -> TitleMatch:
    expected_page = (
        printed_page + page_offset_hint
        if printed_page is not None and page_offset_hint is not None
        else None
    )

    def sort_key(hit: _LineHit) -> tuple[float, int, int, int]:
        printed_bonus = 0
        if expected_page is not None:
            printed_bonus = -abs(hit.page - expected_page)
        return (hit.score, printed_bonus, -hit.line_index, -hit.page)

    ordered = sorted(hits, key=sort_key, reverse=True)
    best = ordered[0]
    pages = sorted({hit.page for hit in ordered})
    confidence_by_source = {
        "anchored": 0.92,
        "h1_result": 0.88,
        "agent_vlm": 0.75,
        "agent_heuristic": 0.5,
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
            "printed_page": printed_page,
            "page_offset_hint": page_offset_hint,
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
            entries.append(
                {
                    "heading": heading,
                    "level": level,
                    "page_number": _safe_int(row.get("page_number")),
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
        raw_title = str(entry.get("heading") or "").strip()
        title = clean_toc_title(raw_title) or normalize_heading_text(raw_title)
        level = _safe_int(entry.get("level")) or 1
        if not title or len(title) < 2:
            continue
        node = TitleNode(
            title=title,
            level=level,
            printed_page=_safe_int(entry.get("page_number")),
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
