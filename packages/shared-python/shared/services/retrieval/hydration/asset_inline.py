"""Insert connected image/table bodies at text placeholders.

Replaces ``[images/...]`` / ``[tables/...]`` (or ``connect_to.ref``) with the
asset display body, with a newline before and after. Targets not found at a
placeholder are appended once. Leftover path placeholders are stripped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from shared.utils.chunk_refs import RESOURCE_PATH_REF_RE, match_resource_path_ref

_SAME_AS_RE = re.compile(r"\[SAME-AS [^\]]+\]")


def remove_path_placeholders(content: str) -> str:
    text = RESOURCE_PATH_REF_RE.sub("", content)
    return _SAME_AS_RE.sub("", text)


def strip_path_placeholders(content: str) -> str:
    return remove_path_placeholders(content).strip()


def inline_assets_at_placeholders(
    host_text: str,
    *,
    connections: Sequence[Mapping[str, Any]] | Sequence[Any],
    display_by_target: Mapping[str, str],
) -> tuple[str, set[str]]:
    """Return (body, embedded_target_ids).

    ``display_by_target`` maps chunk_id → display body. Only targets present in
    that map are inserted. Each target is inserted at most once.
    """
    text = str(host_text or "")
    embedded: set[str] = set()
    pending_append: list[tuple[str, str]] = []

    for item in connections or ():
        if not isinstance(item, Mapping):
            continue
        target_id = str(item.get("target") or "").strip()
        if not target_id or target_id in embedded:
            continue
        body = str(display_by_target.get(target_id) or "").strip()
        if not body:
            continue

        ref = str(item.get("ref") or "").strip()
        placed = False
        match = match_resource_path_ref(text, ref)
        if match is not None:
            text = f"{text[: match.start()]}\n{body}\n{text[match.end() :]}"
            embedded.add(target_id)
            placed = True
        if not placed:
            pending_append.append((target_id, body))

    for target_id, body in pending_append:
        if target_id in embedded:
            continue
        if text.strip():
            text = f"{text.rstrip()}\n\n{body}"
        else:
            text = body
        embedded.add(target_id)

    return strip_path_placeholders(text), embedded
