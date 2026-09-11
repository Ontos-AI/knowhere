"""Concurrency-safe per-call tool dispatch shared by every ``agent_explore`` harness.

Mirrors ``apps/api/app/mcp/dynamic_tools.py``'s ``_dispatch_tool``: SQLAlchemy's
``AsyncSession`` is not safe for concurrent use from multiple coroutines, and a
harness cannot always control whether the orchestrating model issues tool
calls in parallel (verified against the Cursor SDK harness — see
``harness/cursor_harness.py``'s module docstring). Opening a short-lived
session per tool call, instead of sharing one ``AsyncSession`` across the
whole episode, makes dispatch safe regardless of how a harness calls it —
strictly sequential (``harness/openai_harness.py``) or genuinely concurrent
(``harness/cursor_harness.py``).

This is a behavior change for the OpenAI harness too (previously one shared
session for the whole episode), but a safe one: it already dispatched
sequentially, so a fresh session per call only adds one extra connection
checkout per tool call, never a correctness risk.
"""

from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_tools import REGISTRY, ToolBudget, ToolContext, ToolResult

DbFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def dispatch_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    db_factory: DbFactory,
    user_id: str,
    namespace: str,
    document_scope: DocumentScope = DocumentScope(),
    budget: ToolBudget | None = None,
) -> ToolResult:
    """Run one ``REGISTRY`` tool call against a fresh, call-scoped DB session.

    ``budget`` is forwarded into the ``ToolContext`` built for this one call
    (defaults to ``ToolBudget()``, matching prior behavior); callers that need
    the same budget value for their own text-capping (``shared.tool_message_content``)
    should hold onto the ``ToolBudget`` they pass here rather than reach back
    into the (call-scoped, already-closed) ``ToolContext``.
    """
    try:
        async with db_factory() as db:
            tool_ctx = ToolContext(
                db=db, user_id=user_id, namespace=namespace, budget=budget or ToolBudget(),
                document_scope=document_scope
            )
            return await REGISTRY.dispatch(name, tool_ctx, args)
    except Exception as exc:  # noqa: BLE001 - one broken tool must not kill the episode
        return ToolResult(text="", error=f"{type(exc).__name__}: {exc}")
