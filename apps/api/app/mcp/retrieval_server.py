from __future__ import annotations

from typing import Annotated, Any, AsyncContextManager, Callable

from app.services.auth.current_user_authentication_service import (
    get_current_user_authentication_service,
)
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db_context
from shared.services.retrieval import run_retrieval_query

DbFactory = Callable[[], AsyncContextManager[AsyncSession]]
KNOWHERE_NAMESPACE_HEADER = "x-knowhere-namespace"
DEFAULT_NAMESPACE = "default"


def create_public_mcp_transport_security() -> TransportSecuritySettings:
    """Match the public API ingress posture for the mounted MCP endpoint."""
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def get_header(headers: Any, name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        value = headers.get(name.title())
    return value


def get_mcp_request(ctx: Context | None) -> Any:
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    if request is None:
        raise RuntimeError("MCP auth context request is not available")
    return request


def resolve_mcp_namespace(*, ctx: Context | None) -> str:
    try:
        request = get_mcp_request(ctx)
    except (RuntimeError, ValueError):
        return DEFAULT_NAMESPACE
    headers = getattr(request, "headers", {}) or {}
    namespace = str(get_header(headers, KNOWHERE_NAMESPACE_HEADER) or "").strip()
    return namespace or DEFAULT_NAMESPACE


def to_mcp_query_response(response: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for row in response.get("results", []):
        if not isinstance(row, dict):
            continue

        source_value = row.get("source")
        source = source_value if isinstance(source_value, dict) else row
        result: dict[str, Any] = {
            "content": row.get("content"),
            "source_file_name": source.get("source_file_name"),
            "section_path": source.get("section_path"),
            "chunk_type": row.get("chunk_type"),
        }
        if row.get("asset_url"):
            result["asset_url"] = row["asset_url"]
        results.append(result)

    mcp_response: dict[str, Any] = {
        "query": response.get("query"),
        "results": results,
    }
    # Forward evidence_text for agentic mode
    if response.get("evidence_text") is not None:
        mcp_response["evidence_text"] = response["evidence_text"]

    return mcp_response


async def resolve_mcp_user_id(*, ctx: Context | None, db: AsyncSession) -> str:
    request = get_mcp_request(ctx)
    headers = getattr(request, "headers", {}) or {}
    authorization = get_header(headers, "authorization")
    return await get_current_user_authentication_service().authenticate_authorization_header(
        db,
        authorization=authorization,
    )


def create_retrieval_mcp_server(
    *,
    db_factory: DbFactory = get_db_context,
    streamable_http_path: str = "/mcp",
):
    server = FastMCP(
        "knowhere-retrieval",
        instructions=(
            "Use this server to search knowledge. "
            "If you need information before answering, try searching with this tool."
        ),
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        transport_security=create_public_mcp_transport_security(),
    )

    @server.tool(
        name="kb.query",
        description="Search for information and return relevant knowledge snippets.",
    )
    async def kb_query(
        query: Annotated[str, Field(description="What you want to search for.")],
        top_k: Annotated[
            int, Field(description="Maximum number of results to return.")
        ] = 5,
        ctx: Context | None = None,
    ) -> dict:
        namespace = resolve_mcp_namespace(ctx=ctx)
        async with db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            response = await run_retrieval_query(
                db=db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=top_k,
                exclude_document_ids=[],
                exclude_sections=[],
            )
            return to_mcp_query_response(response)

    return server
