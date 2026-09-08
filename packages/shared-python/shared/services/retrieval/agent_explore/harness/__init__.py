"""Pluggable ``agent_explore`` harness implementations.

``base.Harness`` is the provider-agnostic interface; ``openai_harness`` and
``cursor_harness`` are the two implementations selected via
``AGENT_EXPLORE_HARNESS`` (``execution/routes.py``). See the Phase 3.5
section of ``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``.
"""

from __future__ import annotations

from shared.services.retrieval.agent_explore.harness.base import Harness
from shared.services.retrieval.agent_explore.harness.resolve import resolve_harness

__all__ = ["Harness", "resolve_harness"]
