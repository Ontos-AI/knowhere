"""``AGENT_EXPLORE_HARNESS`` env switch — same pattern as ``execution/routes.py``'s
``_resolve_agentic_router()`` (``RETRIEVAL_AGENTIC_ROUTER``): unrecognized or
unset values fall back to the default rather than raising, so a typo'd env
var degrades to known-good behavior instead of breaking the route.

Each branch below imports its harness implementation lazily so that
selecting ``openai`` never imports ``cursor_sdk``-dependent code (and
vice versa) — see ``cursor_harness.py``'s guarded import.
"""

from __future__ import annotations

import os

from shared.services.retrieval.agent_explore.harness.base import Harness

_HARNESS_ENV = "AGENT_EXPLORE_HARNESS"
_HARNESSES = {"openai", "cursor_sdk"}
_DEFAULT_HARNESS = "openai"


def resolve_harness_name() -> str:
    """``openai`` (default, current production behavior) or ``cursor_sdk``."""
    value = os.environ.get(_HARNESS_ENV, "").strip().lower()
    return value if value in _HARNESSES else _DEFAULT_HARNESS


def resolve_harness(name: str | None = None) -> Harness:
    """Build the ``Harness`` implementation for ``name`` (default: env-resolved)."""
    resolved = (name or resolve_harness_name()).strip().lower()
    if resolved == "cursor_sdk":
        from shared.services.retrieval.agent_explore.harness.cursor_harness import (
            CursorHarness,
        )

        return CursorHarness()
    from shared.services.retrieval.agent_explore.harness.openai_harness import (
        OpenAIHarness,
    )

    return OpenAIHarness()
