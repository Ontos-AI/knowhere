"""Loader for ``CORPUS_OVERVIEW.md`` — the agent-facing corpus overview.

The API ``/mcp`` server instructions and ``agent_explore`` system prompt
share this text. Tool-specific usage lives on each tool's own description.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_OVERVIEW_PATH = Path(__file__).with_name("CORPUS_OVERVIEW.md")


@lru_cache(maxsize=1)
def load_corpus_overview_text() -> str:
    """Return the verbatim contents of ``CORPUS_OVERVIEW.md``."""
    return _OVERVIEW_PATH.read_text(encoding="utf-8")
