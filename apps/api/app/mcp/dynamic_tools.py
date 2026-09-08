"""Bridge ``shared.services.retrieval.agent_tools.REGISTRY`` onto FastMCP.

FastMCP's public registration API (``FastMCP.tool`` / ``ToolManager.add_tool``)
only builds a tool's schema by introspecting a Python function's *signature*
(``mcp.server.fastmcp.tools.base.Tool.from_function`` -> ``func_metadata``);
there is no public entry point to register a tool from an already-built JSON
Schema dict, which is what every ``agent_tools.ToolSpec`` carries. Since our
schema is the one already shipped to ``agent_explore`` and meant to be
verbatim-identical across harnesses (see ``CORPUS_SCHEMA.md``), we construct
``Tool`` objects directly instead of round-tripping through a synthetic
Python function signature, and insert them into the tool manager's registry
dict — the same dict ``ToolManager.__init__`` accepts a ``tools=`` list for,
just with no public single-tool equivalent of that constructor path.
"""

from __future__ import annotations

from typing import Any, AsyncContextManager, Callable

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from pydantic import create_model
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_tools import REGISTRY, ToolContext, ToolSpec

DbFactory = Callable[[], AsyncContextManager[AsyncSession]]


def _lenient_arg_model(spec: ToolSpec) -> type[ArgModelBase]:
    """Build a permissive pydantic arg model for FastMCP's internal validation.

    The schema actually exposed to MCP clients is ``spec.json_schema``
    (``Tool.parameters``, returned verbatim in ``tools/list`` — see
    ``FastMCP.list_tools``); this model only has to satisfy
    ``FuncMetadata.call_fn_with_arg_validation`` well enough to forward
    whatever the client sent through to ``_dispatch_tool``'s ``**kwargs``.
    Every property is optional/``Any`` — each tool already validates its own
    required args and reports a caller-facing ``ToolResult.error`` for a
    missing one, so duplicating "required" enforcement here would just
    produce a less informative MCP-level error instead.
    """
    properties = spec.json_schema.get("properties", {})
    fields: dict[str, Any] = {key: (Any, None) for key in properties}
    return create_model(f"{spec.name.replace('.', '_')}_Args", __base__=ArgModelBase, **fields)


def _make_tool(spec: ToolSpec, *, db_factory: DbFactory) -> Tool:
    async def _dispatch_tool(
        ctx: Context | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        from app.mcp.retrieval_server import resolve_mcp_namespace, resolve_mcp_user_id

        namespace = resolve_mcp_namespace(ctx=ctx)
        async with db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            tool_ctx = ToolContext(db=db, user_id=user_id, namespace=namespace)
            result = await REGISTRY.dispatch(spec.name, tool_ctx, kwargs)
        return {"text": result.text, "payload": result.payload, "refs": result.refs, "error": result.error}

    return Tool(
        fn=_dispatch_tool,
        name=spec.name,
        title=None,
        description=spec.description,
        parameters=spec.json_schema,
        fn_metadata=FuncMetadata(arg_model=_lenient_arg_model(spec)),
        is_async=True,
        context_kwarg="ctx",
        annotations=None,
    )


def register_corpus_tools(server: FastMCP, *, db_factory: DbFactory) -> None:
    """Register every ``agent_tools.REGISTRY`` tool onto ``server``."""
    for spec in REGISTRY.all():
        tool = _make_tool(spec, db_factory=db_factory)
        server._tool_manager._tools[tool.name] = tool
