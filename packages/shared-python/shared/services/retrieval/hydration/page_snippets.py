"""
Query-hit snippet extraction for page chunks in retrieval responses.

Page chunks can be very large (a whole scanned page of a directory, form, or
manual). Retrieval responses surface only the chunk's LLM ``summary``, which
rarely contains the exact queried term. These helpers extract every
occurrence of the query terms from the full page content so the response can
show the actual matching lines.
"""

from __future__ import annotations

import re

_PAGE_SNIPPET_CONTEXT_CHARS = 100
_PAGE_SNIPPET_MAX = 20
_ELLIPSIS = "…"


def extract_page_snippets(
    content: str,
    query_tokens: list[str],
    *,
    context_chars: int = _PAGE_SNIPPET_CONTEXT_CHARS,
    max_snippets: int = _PAGE_SNIPPET_MAX,
) -> list[str]:
    """Extract every query-term occurrence from ``content`` as a snippet.

    Each snippet centers one occurrence of any query token with
    ``context_chars`` characters of surrounding context on each side, trimmed
    at word boundaries and marked with an ellipsis at truncated edges.
    Occurrences are returned in document order, deduplicated, and capped at
    ``max_snippets`` so pathological pages (hundreds of hits for a common
    name) stay bounded.
    """
    if not content or not query_tokens:
        return []

    lower_content = content.lower()
    patterns = [
        re.compile(rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])")
        for token in query_tokens
        if token
    ]
    if not patterns:
        return []

    seen: set[str] = set()
    snippets: list[str] = []
    for match in _iter_occurrences(lower_content, patterns):
        snippet = _build_snippet(
            content,
            match.start(),
            match.end(),
            context_chars=context_chars,
        )
        if snippet in seen:
            continue
        seen.add(snippet)
        snippets.append(snippet)
        if len(snippets) >= max_snippets:
            break
    return snippets


def _iter_occurrences(lower_content: str, patterns: list[re.Pattern[str]]):
    """Yield non-overlapping occurrence matches across all patterns in order."""
    matches: list[re.Match[str]] = []
    for pattern in patterns:
        matches.extend(pattern.finditer(lower_content))
    matches.sort(key=lambda m: (m.start(), m.end()))
    return _dedupe_overlapping(matches)


def _dedupe_overlapping(matches: list[re.Match[str]]):
    last_end = -1
    for match in matches:
        if match.start() < last_end:
            continue
        last_end = match.end()
        yield match


def _build_snippet(
    content: str,
    start: int,
    end: int,
    *,
    context_chars: int,
) -> str:
    snippet_start = max(0, start - context_chars)
    snippet_end = min(len(content), end + context_chars)

    if snippet_start > 0:
        snippet_start = _next_word_boundary(content, snippet_start, direction=-1)
    if snippet_end < len(content):
        snippet_end = _next_word_boundary(content, snippet_end, direction=1)

    prefix = _ELLIPSIS if snippet_start > 0 else ""
    suffix = _ELLIPSIS if snippet_end < len(content) else ""
    return f"{prefix}{content[snippet_start:snippet_end]}{suffix}"


def _next_word_boundary(text: str, index: int, *, direction: int) -> int:
    """Move ``index`` to a nearby word boundary in the given direction.

    Only walks a few characters (``_BOUNDARY_WALK_LIMIT``) so long unbroken
    runs (e.g. filler lines) do not stretch the snippet across the whole
    page. Returns the original index when no boundary is nearby.
    """
    length = len(text)
    cursor = index
    for _ in range(_BOUNDARY_WALK_LIMIT):
        if not 0 < cursor < length:
            break
        if not text[cursor - 1].isalnum() and not text[cursor].isalnum():
            return cursor
        cursor += direction
    return index


_BOUNDARY_WALK_LIMIT = 16
