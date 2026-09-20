"""Shared hit rendering for ``corpus.grep`` and ``corpus.recall``.

``build_snippet`` windows body text around the first match: head + first-match
window + tail, ``...``-joined, overlap-merged. Only the first match is
windowed. ``format_search_hit_line`` renders the model-visible identifier
line so both tools stay on the same shape.
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


def format_search_hit_line(
    *,
    source_file_name: object,
    document_id: object,
    section_path: object,
    snippet: str,
    chunk_type: object,
    chunk_id: object | None = None,
    score: object | None = None,
) -> str:
    """Render one grep/recall hit with the fields the model can copy into read.

    Body hits keep ``document_id`` + ``section_path``. Image/table hits also
    include ``chunk_id`` because their stored ``section_path`` is the document
    Root, not the host section.
    """
    type_label = str(chunk_type or "").strip()
    score_part = f" score={score}" if score is not None else ""
    snippet_part = f": {snippet!r}" if snippet else ""
    if chunk_id:
        return (
            f"- [{type_label}] {source_file_name} ({document_id}) "
            f"chunk_id={chunk_id} / {section_path}{score_part}{snippet_part}"
        )
    return (
        f"- [{type_label}] {source_file_name} ({document_id}) / "
        f"{section_path}{score_part}{snippet_part}"
    )
