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
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.app_service import run_retrieval_query
from shared.services.retrieval.settings import DEFAULT_TOP_K

DbFactory = Callable[[], AsyncContextManager[AsyncSession]]
KNOWHERE_NAMESPACE_HEADER = "x-knowhere-namespace"


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
        return normalize_retrieval_namespace(None)
    headers = getattr(request, "headers", {}) or {}
    namespace = get_header(headers, KNOWHERE_NAMESPACE_HEADER)
    return normalize_retrieval_namespace(namespace)


def to_mcp_query_response(response: dict[str, Any]) -> dict[str, Any]:
    """Project the internal retrieval response to the MCP agent contract.

    MCP returns exactly 3 PRIMARY fields:
    - evidence_text: hierarchical evidence tree for LLM consumption
    - referenced_chunks: structured chunk references for citation / follow-up
    - decision_trace: navigation decisions including terminal stop/failure
    """
    return {
        "query": response.get("query"),
        "evidence_text": response.get("evidence_text") or "",
        "referenced_chunks": response.get("referenced_chunks") or [],
        "decision_trace": response.get("decision_trace") or [],
    }


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
            "Use this server to search published documents. "
            "It returns evidence_text (hierarchical evidence tree), "
            "referenced_chunks (structured chunk citations), and "
            "decision_trace (navigation decisions). "
            "Downstream agents should synthesize answers from evidence_text."
        ),
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        transport_security=create_public_mcp_transport_security(),
    )

    @server.tool(
        name="retrieval.query",
        description=(
            "Search published documents. Returns evidence_text (hierarchical "
            "evidence for LLM consumption), referenced_chunks (cited chunk "
            "metadata for follow-up queries), and decision_trace (navigation "
            "decisions including stop/failure reasons). "
            "Include navigation intent directly in your query text — the "
            "engine will automatically locate the right documents and sections."
        ),
    )
    async def query_documents(
        query: Annotated[
            str,
            Field(description=(
                "What you want to search for. You may include navigation "
                "hints naturally, e.g. 'find tables in chapter 3 of the "
                "safety report'. The engine understands document structure."
            )),
        ],
        top_k: Annotated[
            int,
            Field(description=(
                "Number of candidate chunks for initial discovery. "
                "The final output is budget-controlled; this only affects "
                "the discovery recall pool. Usually no need to adjust."
            )),
        ] = DEFAULT_TOP_K,
        exclude_document_ids: Annotated[
            list[str],
            Field(description=(
                "Document IDs to exclude from this query. "
                "Use document_id values from prior referenced_chunks."
            )),
        ] = [],
        exclude_sections: Annotated[
            list[dict[str, str]],
            Field(description=(
                "Sections to exclude. Each item: "
                '{"document_id": "...", "section_path": "..."}.'
            )),
        ] = [],
        ctx: Context | None = None,
    ) -> dict:
        # TODO(intent-step): When the Intent Understanding step is
        # implemented, it will parse `query` here to extract structured
        # hints (document_hint, scope_hint, content_type_hint) and set
        # data_type / signal_paths / filter_mode / exclude_document_ids
        # automatically before calling run_retrieval_query.
        # See: shared/services/retrieval/intent/ (to be created)
        namespace = resolve_mcp_namespace(ctx=ctx)
        async with db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            response = await run_retrieval_query(
                db=db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=top_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=[item for item in exclude_sections],
                use_agentic=True,
            )
            return to_mcp_query_response(response)

    return server
