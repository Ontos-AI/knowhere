"""Locate hierarchy titles on PDF pages and resolve page ranges.

This module is intentionally deterministic: it performs strict title anchoring,
candidate collection, and range assembly. The page-memory residual agent calls
into these primitives for grep-like tools and adds VLM verification outside this
module.
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
    "exact",
    "anchored",
    "page_compact",
    "normalized",
    "token",
    "printed_prior",
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


_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def locate_title_start_page(
    title: str,
    *,
    scope_pages: list[int],
    page_texts: dict[int, str],
    printed_page: int | None = None,
    page_offset_hint: int | None = None,
) -> TitleMatch | None:
    """Locate *title* using deterministic weak evidence.

    This is a candidate-gathering primitive. The C4 page-memory path should only
    directly accept :func:`locate_title_strict_exact`; weak results from this
    function are meant for residual agent/VLM arbitration.
    """
    matches = collect_title_candidate_matches(
        title,
        scope_pages=scope_pages,
        page_texts=page_texts,
        printed_page=printed_page,
        page_offset_hint=page_offset_hint,
    )
    return matches[0] if matches else None


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


def collect_title_candidate_matches(
    title: str,
    *,
    scope_pages: list[int],
    page_texts: dict[int, str],
    printed_page: int | None = None,
    page_offset_hint: int | None = None,
    limit: int | None = None,
) -> list[TitleMatch]:
    """Collect grep-style candidate pages for a title without final arbitration."""
    normalized_title = normalize_heading_text(title)
    if not normalized_title or not scope_pages:
        return []

    hits: list[_LineHit] = []
    for _source, finder in (
        ("anchored", _find_anchored_hits),
        ("page_compact", _find_page_compact_hits),
        ("normalized", _find_normalized_hits),
        ("token", _find_token_hits),
    ):
        hits.extend(finder(normalized_title, scope_pages, page_texts))

    by_page: dict[int, list[_LineHit]] = {}
    for hit in hits:
        by_page.setdefault(hit.page, []).append(hit)

    matches = [
        _choose_best_hit(
            page_hits,
            source=_preferred_source(page_hits),
            printed_page=printed_page,
            page_offset_hint=page_offset_hint,
        )
        for page_hits in by_page.values()
    ]

    prior_page = _resolve_printed_prior(
        printed_page=printed_page,
        page_offset_hint=page_offset_hint,
        scope_pages=scope_pages,
    )
    if prior_page is not None and prior_page not in by_page:
        matches.append(
            TitleMatch(
                page=prior_page,
                confidence=0.35,
                source="printed_prior",
                matched_line="",
                score=0.35,
                candidates=[prior_page],
                evidence={
                    "printed_page": printed_page,
                    "page_offset_hint": page_offset_hint,
                },
            )
        )

    matches.sort(
        key=lambda match: (
            match.score,
            match.confidence,
            -abs(match.page - (printed_page + page_offset_hint))
            if printed_page is not None and page_offset_hint is not None
            else 0,
            -match.page,
        ),
        reverse=True,
    )
    if limit is not None:
        return matches[: max(int(limit), 0)]
    return matches


def resolve_hierarchy_page_ranges(
    nodes: list[TitleNode],
    *,
    page_count: int,
    page_texts: dict[int, str],
    body_pages: list[int] | None = None,
    page_offset_hint: int | None = None,
    match_overrides: dict[tuple[str, ...], TitleMatch] | None = None,
    use_weak_fallback: bool = False,
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
        use_weak_fallback=use_weak_fallback,
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
    use_weak_fallback: bool,
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
        if match is None and use_weak_fallback:
            match = locate_title_start_page(
                node.title,
                scope_pages=pages,
                page_texts=page_texts,
                printed_page=node.printed_page,
                page_offset_hint=page_offset_hint,
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
                use_weak_fallback=use_weak_fallback,
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
                use_weak_fallback=use_weak_fallback,
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
    use_weak_fallback: bool,
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
        if match is None and use_weak_fallback:
            match = locate_title_start_page(
                sibling.title,
                scope_pages=pages,
                page_texts=page_texts,
                printed_page=sibling.printed_page,
                page_offset_hint=page_offset_hint,
            )
        if match is not None:
            return match
    return None


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


def max_title_depth(nodes: list[TitleNode]) -> int:
    if not nodes:
        return 0
    return max(
        max(node.level, max_title_depth(node.children))
        if node.children
        else node.level
        for node in nodes
    )


def prune_title_nodes_for_emit_depth(
    nodes: list[TitleNode],
    *,
    emit_depth: int,
) -> list[TitleNode]:
    pruned: list[TitleNode] = []
    for node in nodes:
        if node.level >= emit_depth or not node.children:
            pruned.append(
                TitleNode(
                    title=node.title,
                    level=node.level,
                    printed_page=node.printed_page,
                    physical_page_hint=node.physical_page_hint,
                    children=[],
                )
            )
            continue
        pruned.append(
            TitleNode(
                title=node.title,
                level=node.level,
                printed_page=node.printed_page,
                physical_page_hint=node.physical_page_hint,
                children=prune_title_nodes_for_emit_depth(
                    node.children,
                    emit_depth=emit_depth,
                ),
            )
        )
    return pruned


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


def _find_exact_hits(
    title: str,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> list[_LineHit]:
    hits: list[_LineHit] = []
    needle = normalize_heading_text(title).casefold()
    for page, line_index, line in _iter_lines(scope_pages, page_texts):
        normalized_line = normalize_heading_text(line).casefold()
        if needle and needle in normalized_line:
            base = 1.0
            cleaned_line = normalize_heading_text(clean_toc_title(line)).casefold()
            if normalized_line == needle or cleaned_line == needle:
                base = 1.18
            hits.append(
                _LineHit(
                    page=page,
                    line_index=line_index,
                    line=line.strip(),
                    source="exact",
                    score=_line_score(line=line, line_index=line_index, base=base),
                )
            )
    return hits


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


def _find_page_compact_hits(
    title: str,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> list[_LineHit]:
    hits: list[_LineHit] = []
    needles = _compact_title_variants(title)
    if not needles:
        return hits

    for page in scope_pages:
        raw_text = page_texts.get(page, "")
        compact_text = _compact_match_text(raw_text)
        if not compact_text:
            continue
        matched_needle = next((needle for needle in needles if needle in compact_text), None)
        if matched_needle is None:
            continue
        line_index, evidence = _compact_match_evidence(raw_text, matched_needle)
        hits.append(
            _LineHit(
                page=page,
                line_index=line_index,
                line=evidence,
                source="page_compact",
                score=_line_score(line=evidence, line_index=line_index, base=0.94),
            )
        )
    return hits


def _find_normalized_hits(
    title: str,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> list[_LineHit]:
    hits: list[_LineHit] = []
    needle = normalize_heading_text(clean_toc_title(title)).casefold()
    if len(needle) < 2:
        return hits
    for page, line_index, line in _iter_lines(scope_pages, page_texts):
        cleaned_line = normalize_heading_text(clean_toc_title(line)).casefold()
        if not cleaned_line:
            continue
        if needle in cleaned_line or _is_strong_reverse_match(cleaned_line, needle):
            hits.append(
                _LineHit(
                    page=page,
                    line_index=line_index,
                    line=line.strip(),
                    source="normalized",
                    score=_line_score(line=line, line_index=line_index, base=0.9),
                )
            )
    return hits


def _find_token_hits(
    title: str,
    scope_pages: list[int],
    page_texts: dict[int, str],
) -> list[_LineHit]:
    title_tokens = _significant_tokens(clean_toc_title(title) or title)
    if not title_tokens:
        return []

    hits: list[_LineHit] = []
    for page, line_index, line in _iter_lines(scope_pages, page_texts):
        line_tokens = _significant_tokens(line)
        if not line_tokens:
            continue
        coverage = len(title_tokens & line_tokens) / len(title_tokens)
        if coverage < 0.8:
            continue
        hits.append(
            _LineHit(
                page=page,
                line_index=line_index,
                line=line.strip(),
                source="token",
                score=_line_score(line=line, line_index=line_index, base=0.78)
                + coverage,
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
        "exact": 0.95,
        "anchored": 0.92,
        "page_compact": 0.9,
        "normalized": 0.84,
        "token": 0.72,
        "printed_prior": 0.35,
        "h1_result": 0.88,
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


def _preferred_source(hits: list[_LineHit]) -> TitleMatchSource:
    priority = {
        "anchored": 50,
        "page_compact": 40,
        "normalized": 30,
        "token": 20,
        "printed_prior": 10,
        "exact": 5,
        "h1_result": 60,
        "agent_vlm": 70,
        "agent_heuristic": 15,
    }
    return max(hits, key=lambda hit: (priority.get(hit.source, 0), hit.score)).source


def _resolve_printed_prior(
    *,
    printed_page: int | None,
    page_offset_hint: int | None,
    scope_pages: list[int],
) -> int | None:
    if printed_page is None or page_offset_hint is None or not scope_pages:
        return None
    page = printed_page + page_offset_hint
    if page in scope_pages:
        return page
    return None


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


def _compact_title_variants(title: str) -> list[str]:
    normalized = normalize_heading_text(clean_toc_title(title) or title).casefold()
    compacted: list[str] = []
    compact = _compact_match_text(normalized)
    if compact:
        compacted.append(compact)
    return compacted


def _compact_match_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_heading_text(text)).casefold()


def _compact_match_evidence(raw_text: str, compact_needle: str) -> tuple[int, str]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return 0, ""

    compact_so_far = ""
    start_index = 0
    for index, line in enumerate(lines):
        line_compact = _compact_match_text(line)
        if not compact_so_far:
            start_index = index
        compact_so_far += line_compact
        if compact_needle in compact_so_far:
            return start_index, " ".join(lines[start_index : index + 1])[:160]
        if len(compact_so_far) > len(compact_needle) * 3:
            compact_so_far = line_compact
            start_index = index

    return 0, " ".join(lines[:3])[:160]


def _is_strong_reverse_match(fragment: str, title: str) -> bool:
    return len(fragment) >= 6 and fragment in title


def _significant_tokens(text: str) -> set[str]:
    normalized = normalize_heading_text(clean_toc_title(text) or text).casefold()
    latin = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]+", normalized)
        if token not in _STOPWORDS
    }
    cjk = set(re.findall(r"[\u4e00-\u9fff]", normalized))
    return latin | cjk


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
