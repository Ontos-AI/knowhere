"""Shared evidence_text rendering: one ``[E#]`` block per group.

Each block is ``[E#]`` + ``[§ path]`` (full traceable path) + body lines.
Groups are caller-provided; bodies in one group stay in that group.
"""

from __future__ import annotations

from typing import Sequence


def render_evidence_blocks(
    groups: Sequence[tuple[str, Sequence[str]]],
    *,
    start_index: int = 1,
) -> str:
    """Render (path, bodies) groups into evidence_text.

    ``path`` is the full traceable header (e.g. file / section chain).
    Bodies are joined with newlines; multiple bodies in one group are indented.
    ``start_index`` sets the first ``[E#]`` number (default 1).
    """
    parts: list[str] = []
    index = max(1, int(start_index or 1))
    for path, bodies in groups:
        texts = [str(t or "").strip() for t in bodies]
        texts = [t for t in texts if t]
        if not texts:
            continue
        block: list[str] = [f"[E{index + len(parts)}]"]
        header = str(path or "").strip()
        if header:
            block.append(f"[§ {header}]")
        indent = len(texts) >= 2
        for text in texts:
            if indent:
                block.append(
                    "\n".join(
                        ("  " + ln if ln.strip() else ln) for ln in text.splitlines()
                    )
                )
            else:
                block.append(text)
        parts.append("\n".join(block).strip())
    return "\n\n".join(parts)
