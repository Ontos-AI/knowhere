"""Shared hit rendering for every ``corpus.*`` tool.

``build_snippet`` windows body text around the first match: head + first-match
window + tail, ``...``-joined, overlap-merged. Only the first match is
windowed.

``build_row`` / ``format_row`` are the one row shape every tool shares —
outline/node_filter (map rows: indented, ``summary``),
grep/recall (hit rows: flat, ``snippet``, ``score``), and assets (asset rows:
``chunk_id`` plus the hosting ``section_path``) all build the same record for
``payload["rows"]`` and render it the same way, so a model reads one row
shape regardless of which tool produced it and copies the same fields
(``document_id`` + ``section_path``/``chunk_id``) into ``corpus.read``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HIT_CONTEXT_CHARS = 80
HEAD_TAIL_CHARS = 50


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(s for s in spans if s[1] > s[0])
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def build_snippet(
    text: str,
    hit: tuple[int, int] | None = None,
    *,
    hit_context: int = HIT_CONTEXT_CHARS,
    head_tail: int = HEAD_TAIL_CHARS,
) -> str:
    """Head/tail anchor + first-match window, joined by ``...`` where spans don't touch.

    ``hit`` is the ``(start, end)`` char offset of the located match in
    ``text``, or ``None`` when no specific position is known (falls back to
    head/tail anchors only). Short text (<= ``head_tail * 2`` chars) is
    returned unchanged.
    """
    if not text:
        return ""
    if len(text) <= head_tail * 2:
        return text

    spans: list[tuple[int, int]] = [
        (0, head_tail),
        (max(len(text) - head_tail, 0), len(text)),
    ]
    if hit is not None:
        hit_start, hit_end = hit
        spans.append(
            (max(hit_start - hit_context, 0), min(hit_end + hit_context, len(text)))
        )

    merged = _merge_spans(spans)
    parts: list[str] = []
    prev_end = 0
    for start, end in merged:
        if start > prev_end:
            parts.append("...")
        parts.append(text[start:end])
        prev_end = end
    if prev_end < len(text):
        parts.append("...")
    return "".join(parts)


def build_row(
    *,
    kind: str,
    document_id: object,
    section_path: object,
    title: object = "",
    chunk_id: object | None = None,
    summary: str = "",
    snippet: str = "",
    score: float | None = None,
    depth: int = 0,
    is_hit: bool = False,
    hosted: bool | None = None,
) -> dict[str, Any]:
    """One row record. Every ``corpus.*`` search/map tool returns these in
    ``payload["rows"]`` and renders them with ``format_row``.

    ``kind`` is ``section`` (outline/node_filter node), ``text``/``page``
    (body hit), or ``image``/``table`` (asset — its own body chunk or a hit
    on one). For ``image``/``table`` rows ``section_path`` is the *hosting*
    section, and ``hosted`` says whether a host was found (``False`` keeps
    the asset's own ``Root`` path); it is ``None`` for every other kind.
    ``summary`` is a map-row field; ``snippet``/``score`` are hit-row
    fields; ``depth`` indents a map row under its parent; ``is_hit`` marks a
    node_filter predicate match.
    """
    return {
        "kind": kind,
        "title": str(title or "").strip(),
        "document_id": str(document_id),
        "section_path": str(section_path),
        "chunk_id": str(chunk_id) if chunk_id else None,
        "summary": summary,
        "snippet": snippet,
        "score": score,
        "is_hit": is_hit,
        "depth": max(depth, 0),
        "hosted": hosted,
    }


def format_row(row: Mapping[str, Any]) -> str:
    """Render one ``build_row`` record as the shared model-visible row."""
    indent = "  " * row["depth"]
    header = f"{indent}- [{row['kind']}]"
    if row["title"]:
        header += f" {row['title']}"
    header += f" | document_id={row['document_id']} section_path={row['section_path']}"
    if row["hosted"] is False:
        header += " (no host section)"
    if row["chunk_id"]:
        header += f" chunk_id={row['chunk_id']}"
    if row["score"] is not None:
        header += f" score={row['score']}"
    if row["is_hit"]:
        header += " [Hit]"
    lines = [header]
    if row["summary"]:
        lines.append(f"{indent}  summary: {row['summary']}")
    if row["snippet"]:
        lines.append(f"{indent}  snippet: {row['snippet']!r}")
    return "\n".join(lines)
