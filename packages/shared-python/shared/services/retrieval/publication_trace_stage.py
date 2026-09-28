"""Optional named timing around one publication operation."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, ContextManager

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace


def trace_publication_stage(
    trace: PublicationTrace | None,
    name: str,
) -> ContextManager[None]:
    """Return a named timer when tracing is enabled."""
    return trace.stage(name) if trace is not None else nullcontext()
