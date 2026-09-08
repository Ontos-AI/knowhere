"""Shared hit-anchored snippet builder for ``corpus.grep`` and ``corpus.recall``.

Both tools locate a single pattern/term inside one chunk's text and need to
show a bounded excerpt around it. The shape is: head anchor + the window
around the (first) match + tail anchor, with ``...`` between spans that do
not touch. Overlapping/adjacent spans are merged before rendering so short
chunks never produce duplicate text or a stray ``...`` inside otherwise
continuous text.

Only the first match is windowed. A single call passes a single
pattern/term, but that pattern can still occur more than once inside one
chunk (verified against real data); later occurrences in the same chunk are
not separately windowed here — use ``corpus.read`` on the chunk for the rest.
"""

from __future__ import annotations

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
