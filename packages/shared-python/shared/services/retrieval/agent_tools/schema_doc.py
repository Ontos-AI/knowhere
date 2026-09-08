"""Single loader for ``CORPUS_SCHEMA.md`` — the agent-facing corpus schema text.

Both harnesses (Phase 3) read through this function instead of the file
directly, so there is exactly one place that resolves the path: the API
``/mcp`` server's ``instructions`` and ``agent_explore``'s system prompt must
stay byte-identical for the shared schema portion (see the module docstring
at the top of ``CORPUS_SCHEMA.md`` — "do not duplicate it elsewhere").
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("CORPUS_SCHEMA.md")


@lru_cache(maxsize=1)
def load_corpus_schema_text() -> str:
    """Return the verbatim contents of ``CORPUS_SCHEMA.md``."""
    return _SCHEMA_PATH.read_text(encoding="utf-8")
