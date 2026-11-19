"""Nest the TOCs of one PDF before calibration.

TOCs are walked in page order with a stack of open TOCs. Each later TOC is
first checked as a paired duplicate of the TOC before it that is not itself
paired (unchanged pairing gate), then asked whether it lies inside the stack
top: if so it is pushed under it, otherwise the top is closed (popped) and the
next one down is asked. A TOC that no open TOC contains sits at document level.

When a nesting check gives no answer, the TOC gets no parent (``unknown``) and the stack stays as it was; its placement is decided after
calibration from evidenced spans.

Parents use the host keys of pending records: ``"root"`` is the primary
(earliest) TOC, an int is an index into ``pending``, ``None`` is document level.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.anchoring_primitives import (
    toc_range_start,
)

_LOG_PREFIX = "[profile.toc_nesting]"

TocParent = Literal["root"] | int | None


@dataclass
class PendingTocNesting:
    parents: dict[int, TocParent] = field(default_factory=dict)
    """Parent of every pending TOC whose nesting checks were answered."""
    unknown: set[int] = field(default_factory=set)
    """Pending TOCs whose nesting check gave no answer."""
    paired: dict[int, dict[str, Any]] = field(default_factory=dict)
    """Paired pending record per pending index; these skip calibration."""

    def calibrated_indices(self) -> list[int]:
        """Pending TOCs that calibrate: every one that is not a paired duplicate."""
        return sorted([*self.parents, *self.unknown])

    def is_descendant(self, index: int, ancestor: int) -> bool:
        parent = self.parents.get(index)
        while isinstance(parent, int):
            if parent == ancestor:
                return True
            parent = self.parents.get(parent)
        return False

    def scope_end(
        self,
        index: int,
        *,
        pending: list[dict[str, Any]],
        page_count: int,
    ) -> int:
        """Last page of a pending TOC's content: before the next TOC it does not contain.

        Its own sub-TOCs lie inside it and do not end it; paired duplicates
        never end a scope. An unknown TOC contains none, so the next calibrated
        TOC ends its scope, and it ends the scope of the TOC before it.
        """
        for later in self.calibrated_indices():
            if later <= index or self.is_descendant(later, index):
                continue
            start = toc_range_start(pending[later])
            if start is not None:
                return start - 1
        return page_count


def nest_pending_tocs(
    *,
    primary_toc: dict[str, Any],
    pending: list[dict[str, Any]],
    ctx: ToolContext,
) -> PendingTocNesting:
    """Nest ``pending`` (sorted by TOC start page) under ``primary_toc`` and each other."""
    from app.services.document_agent.structure.toc_pairing import (
        check_paired_toc,
        check_toc_nesting,
    )

    model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    nesting = PendingTocNesting()
    # Open TOCs, innermost last; the document level is below the bottom.
    stack: list[Literal["root"] | int] = ["root"]
    # The latest TOC that is not a paired duplicate.
    pair_host: Literal["root"] | int = "root"

    def toc_of(key: Literal["root"] | int) -> dict[str, Any]:
        return primary_toc if key == "root" else pending[key]

    for index, pending_toc in enumerate(pending):
        match = check_paired_toc(toc_of(pair_host), pending_toc, ctx=ctx, model=model)
        if match.is_paired:
            nesting.paired[index] = _paired_record(
                pending_toc,
                host=pair_host,
                host_toc=toc_of(pair_host),
                pattern=match.pattern,
                reason=match.reason,
            )
            continue
        pair_host = index

        # Ask open TOCs from the innermost out; pops apply only once answered.
        # Every TOC placed so far was pushed, so the stack is never empty here.
        depth = len(stack)
        answered = True
        while depth:
            inside = check_toc_nesting(
                toc_of(stack[depth - 1]), pending_toc, ctx=ctx, model=model
            ).inside
            if inside is None:
                answered = False
                break
            if inside:
                break
            depth -= 1

        if not answered:
            nesting.unknown.add(index)
            logger.info(
                "{} pending TOC toc_range={} nesting unknown",
                _LOG_PREFIX,
                pending_toc.get("toc_range"),
            )
            continue
        del stack[depth:]
        parent: TocParent = stack[-1] if stack else None
        nesting.parents[index] = parent
        stack.append(index)
        logger.info(
            "{} pending TOC toc_range={} parent={}",
            _LOG_PREFIX,
            pending_toc.get("toc_range"),
            parent,
        )
    return nesting


def _paired_record(
    pending_toc: dict[str, Any],
    *,
    host: Literal["root"] | int,
    host_toc: dict[str, Any],
    pattern: str | None,
    reason: str,
) -> dict[str, Any]:
    host_range = list(host_toc.get("toc_range") or [])
    pending_toc["paired_with_range"] = host_range
    pending_toc["paired_pattern"] = pattern
    logger.info(
        "{} pending TOC toc_range={} paired with {} pattern={}",
        _LOG_PREFIX,
        pending_toc.get("toc_range"),
        host_range,
        pattern,
    )
    return {
        "toc": pending_toc,
        "relationship": "paired",
        "host": host,
        "paired_with_range": host_range,
        "paired_pattern": pattern,
        "paired_reason": reason,
    }
