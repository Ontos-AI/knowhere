from __future__ import annotations

from typing import Annotated, Any, AsyncContextManager, Callable, Literal

from app.services.auth.current_user_authentication_service import (
    get_current_user_authentication_service,
)
from app.services.documents.inspection_service import (
    DEFAULT_GREP_RESULT_LIMIT,
    MAX_GREP_RESULT_LIMIT,
    DocumentGrepChunksResponse,
    DocumentInspectionService,
    DocumentListResponse,
    DocumentOutlineResponse,
    DocumentReadChunksResponse,
)
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.app_service import run_retrieval_query
from shared.services.retrieval.settings import DEFAULT_TOP_K

DbFactory = Callable[[], AsyncContextManager[AsyncSession]]
KnowhereTargetContent = Literal[
    "all",
    "text",
    "image",
    "table",
    "text_image",
    "text_table",
]
KnowhereFilterMode = Literal["delete", "keep"]
KNOWHERE_NAMESPACE_HEADER = "x-knowhere-namespace"

TARGET_CONTENT_TO_DATA_TYPE: dict[str, int] = {
    "all": 1,
    "text": 2,
    "image": 3,
    "table": 4,
    "text_image": 5,
    "text_table": 6,
}


class KnowhereExcludeSection(BaseModel):
    document_id: str
    section_path: str


class KnowhereSearchSource(BaseModel):
    document_id: str | None = None
    source_file_name: str | None = None
    section_path: str | None = None


class KnowhereSearchResult(BaseModel):
    chunk_type: str | None = None
    content: str | None = None
    score: float | None = None
    asset_url: str | None = None
    source: KnowhereSearchSource = Field(default_factory=KnowhereSearchSource)


class KnowhereReferencedChunk(BaseModel):
    document_id: str | None = None
    chunk_id: str | None = None
    chunk_type: str | None = None
    section_path: str | None = None
    file_path: str | None = None
    source_file_name: str | None = None
    job_id: str | None = None
    score: float | None = None
    asset_url: str | None = None


class KnowhereSearchResponse(BaseModel):
    namespace: str
    query: str
    evidence_text: str
    referenced_chunks: list[KnowhereReferencedChunk] = Field(default_factory=list)
    decision_trace: list[dict[str, object]] = Field(default_factory=list)
    results: list[KnowhereSearchResult] = Field(default_factory=list)
    stop_reason: str | None = None
    failure_reason: str | None = None


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


def resolve_mcp_namespace(
    *,
    ctx: Context | None,
    namespace: str | None = None,
) -> str:
    if namespace is not None:
        return normalize_retrieval_namespace(namespace)
    try:
        request = get_mcp_request(ctx)
    except (RuntimeError, ValueError):
        return normalize_retrieval_namespace(None)
    headers = getattr(request, "headers", {}) or {}
    header_namespace = get_header(headers, KNOWHERE_NAMESPACE_HEADER)
    return normalize_retrieval_namespace(header_namespace)


def to_mcp_search_response(
    response: dict[str, Any],
    *,
    namespace: str,
    query: str,
) -> KnowhereSearchResponse:
    return KnowhereSearchResponse(
        namespace=_read_string(response.get("namespace")) or namespace,
        query=_read_string(response.get("query")) or query,
        evidence_text=_read_string(response.get("evidence_text")) or "",
        referenced_chunks=[
            _to_referenced_chunk(item)
            for item in _read_dict_list(response.get("referenced_chunks"))
        ],
        decision_trace=_read_object_dict_list(response.get("decision_trace")),
        results=[
            _to_search_result(item) for item in _read_dict_list(response.get("results"))
        ],
        stop_reason=_read_string(response.get("stop_reason")),
        failure_reason=_read_string(response.get("failure_reason")),
    )


async def resolve_mcp_user_id(*, ctx: Context | None, db: AsyncSession) -> str:
    request = get_mcp_request(ctx)
    headers = getattr(request, "headers", {}) or {}
    authorization = get_header(headers, "authorization")
    return await get_current_user_authentication_service().authenticate_authorization_header(
        db,
        authorization=authorization,
    )


def create_db_context() -> AsyncContextManager[AsyncSession]:
    from shared.core.database import get_db_context

    return get_db_context()


def create_retrieval_mcp_server(
    *,
    db_factory: DbFactory | None = None,
    streamable_http_path: str = "/mcp",
    document_inspection_service: DocumentInspectionService | None = None,
):
    effective_db_factory = db_factory or create_db_context
    inspection_service = document_inspection_service or DocumentInspectionService()
    server = FastMCP(
        "knowhere-retrieval",
        instructions=(
            "Use this server to inspect and retrieve from published Knowhere "
            "documents. Use knowhere_search for discovery first when the target "
            "document is unknown, then use knowhere_get_document_outline, "
            "knowhere_read_chunks, or knowhere_grep_chunks with exact document "
            "and chunk identifiers for bounded follow-up."
        ),
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        transport_security=create_public_mcp_transport_security(),
    )

    @server.tool(
        name="knowhere_search",
        description=(
            "Search published Knowhere documents in a namespace. Use this first "
            "when the target document is unknown. It returns hierarchical "
            "evidence_text, structured referenced_chunks, decision_trace, and "
            "ranked result previews for follow-up with document inspection tools."
        ),
        structured_output=True,
    )
    async def knowhere_search(
        query: Annotated[
            str,
            Field(description="Question or semantic search query for Knowhere."),
        ],
        namespace: Annotated[
            str | None,
            Field(description=(
                "Optional retrieval namespace override. If omitted, the "
                "x-knowhere-namespace header is used, then default."
            )),
        ] = None,
        top_k: Annotated[
            int,
            Field(description="Maximum initial discovery candidates."),
        ] = DEFAULT_TOP_K,
        target_content: Annotated[
            KnowhereTargetContent,
            Field(description=(
                "Content type to retrieve: all, text, image, table, text_image, "
                "or text_table."
            )),
        ] = "all",
        signal_paths: Annotated[
            list[str] | None,
            Field(description="Optional path or section keywords to keep or delete."),
        ] = None,
        filter_mode: Annotated[
            KnowhereFilterMode,
            Field(description="How to apply signal_paths: keep or delete."),
        ] = "delete",
        threshold: Annotated[
            float,
            Field(description="Minimum retrieval score threshold.", ge=0.0),
        ] = 0.0,
        exclude_document_ids: Annotated[
            list[str] | None,
            Field(description="Document IDs to exclude from this query."),
        ] = None,
        exclude_sections: Annotated[
            list[KnowhereExcludeSection] | None,
            Field(description=(
                "Sections to exclude. Each item has document_id and section_path."
            )),
        ] = None,
        ctx: Context | None = None,
    ) -> KnowhereSearchResponse:
        effective_namespace = resolve_mcp_namespace(ctx=ctx, namespace=namespace)
        async with effective_db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            response = await run_retrieval_query(
                db=db,
                user_id=user_id,
                namespace=effective_namespace,
                query=query,
                top_k=top_k,
                exclude_document_ids=exclude_document_ids or [],
                exclude_sections=[
                    item.model_dump() for item in exclude_sections or []
                ],
                data_type=TARGET_CONTENT_TO_DATA_TYPE[target_content],
                signal_paths=_normalize_string_list(signal_paths),
                filter_mode=filter_mode,
                threshold=threshold,
                use_agentic=True,
            )
            return to_mcp_search_response(
                response,
                namespace=effective_namespace,
                query=query,
            )

    @server.tool(
        name="knowhere_list_documents",
        description="List active Knowhere documents in the effective namespace.",
        structured_output=True,
    )
    async def knowhere_list_documents(
        namespace: Annotated[
            str | None,
            Field(description=(
                "Optional retrieval namespace override. If omitted, the "
                "x-knowhere-namespace header is used, then default."
            )),
        ] = None,
        ctx: Context | None = None,
    ) -> DocumentListResponse:
        effective_namespace = resolve_mcp_namespace(ctx=ctx, namespace=namespace)
        async with effective_db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            return await inspection_service.list_documents(
                db,
                user_id=user_id,
                namespace=effective_namespace,
            )

    @server.tool(
        name="knowhere_get_document_outline",
        description=(
            "Return metadata, chunk counts, type counts, and ordered section "
            "outline for one active Knowhere document revision."
        ),
        structured_output=True,
    )
    async def knowhere_get_document_outline(
        document_id: Annotated[
            str,
            Field(description="Exact Knowhere document_id returned by search or list."),
        ],
        namespace: Annotated[
            str | None,
            Field(description="Optional retrieval namespace override."),
        ] = None,
        ctx: Context | None = None,
    ) -> DocumentOutlineResponse:
        effective_namespace = resolve_mcp_namespace(ctx=ctx, namespace=namespace)
        async with effective_db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            response = await inspection_service.get_document_outline(
                db,
                user_id=user_id,
                namespace=effective_namespace,
                document_id=document_id,
            )
            if response is None:
                raise ValueError("Document not found or not active in namespace.")
            return response

    @server.tool(
        name="knowhere_read_chunks",
        description=(
            "Read bounded exact chunks from one active Knowhere document by "
            "section path, 1-based chunk range, document_chunk_id, or chunk_id."
        ),
        structured_output=True,
    )
    async def knowhere_read_chunks(
        document_id: Annotated[
            str,
            Field(description="Exact Knowhere document_id returned by search or list."),
        ],
        namespace: Annotated[
            str | None,
            Field(description="Optional retrieval namespace override."),
        ] = None,
        section_path: Annotated[
            str | None,
            Field(description="Optional exact section_path from document outline."),
        ] = None,
        start_chunk: Annotated[
            int | None,
            Field(description="Optional 1-based chunk ordinal to start reading."),
        ] = None,
        end_chunk: Annotated[
            int | None,
            Field(description="Optional 1-based chunk ordinal to stop reading."),
        ] = None,
        document_chunk_id: Annotated[
            str | None,
            Field(description="Optional canonical document_chunks.id value."),
        ] = None,
        chunk_id: Annotated[
            str | None,
            Field(description="Optional parser semantic chunk_id value."),
        ] = None,
        ctx: Context | None = None,
    ) -> DocumentReadChunksResponse:
        effective_namespace = resolve_mcp_namespace(ctx=ctx, namespace=namespace)
        async with effective_db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            response = await inspection_service.read_chunks(
                db,
                user_id=user_id,
                namespace=effective_namespace,
                document_id=document_id,
                section_path=section_path,
                start_chunk=start_chunk,
                end_chunk=end_chunk,
                document_chunk_id=document_chunk_id,
                chunk_id=chunk_id,
            )
            if response is None:
                raise ValueError("Document not found or not active in namespace.")
            return response

    @server.tool(
        name="knowhere_grep_chunks",
        description=(
            "Search one active Knowhere document's chunks by literal text or "
            "regular expression. Results are ordered by chunk sort_order and "
            "returned with exact IDs and snippets."
        ),
        structured_output=True,
    )
    async def knowhere_grep_chunks(
        document_id: Annotated[
            str,
            Field(description="Exact Knowhere document_id returned by search or list."),
        ],
        pattern: Annotated[
            str,
            Field(description=(
                "Literal text by default. Set is_regex=true for regex syntax."
            )),
        ],
        namespace: Annotated[
            str | None,
            Field(description="Optional retrieval namespace override."),
        ] = None,
        is_regex: Annotated[
            bool,
            Field(description="Treat pattern as a Python regular expression."),
        ] = False,
        is_case_sensitive: Annotated[
            bool,
            Field(description="Use case-sensitive matching."),
        ] = False,
        max_results: Annotated[
            int | None,
            Field(description=(
                f"Maximum matches to return. Defaults to {DEFAULT_GREP_RESULT_LIMIT}; "
                f"hard-capped at {MAX_GREP_RESULT_LIMIT}."
            )),
        ] = None,
        chunk_type: Annotated[
            str | None,
            Field(description="Optional chunk type filter, e.g. text, image, table."),
        ] = None,
        section_path_prefix: Annotated[
            str | None,
            Field(description="Optional section path prefix filter."),
        ] = None,
        ctx: Context | None = None,
    ) -> DocumentGrepChunksResponse:
        effective_namespace = resolve_mcp_namespace(ctx=ctx, namespace=namespace)
        async with effective_db_factory() as db:
            user_id = await resolve_mcp_user_id(ctx=ctx, db=db)
            response = await inspection_service.grep_chunks(
                db,
                user_id=user_id,
                namespace=effective_namespace,
                document_id=document_id,
                pattern=pattern,
                is_regex=is_regex,
                is_case_sensitive=is_case_sensitive,
                max_results=max_results,
                chunk_type=chunk_type,
                section_path_prefix=section_path_prefix,
            )
            if response is None:
                raise ValueError("Document not found or not active in namespace.")
            return response

    return server


def _normalize_string_list(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    normalized_values = []
    seen_values: set[str] = set()
    for value in values:
        normalized_value = value.strip()
        if not normalized_value or normalized_value in seen_values:
            continue
        seen_values.add(normalized_value)
        normalized_values.append(normalized_value)
    return normalized_values or None


def _to_search_result(item: dict[str, Any]) -> KnowhereSearchResult:
    source = item.get("source")
    source_item = source if isinstance(source, dict) else {}
    return KnowhereSearchResult(
        chunk_type=_read_string(item.get("chunk_type")),
        content=_read_string(item.get("content")),
        score=_read_float(item.get("score")),
        asset_url=_read_string(item.get("asset_url")),
        source=KnowhereSearchSource(
            document_id=_read_string(source_item.get("document_id")),
            source_file_name=_read_string(source_item.get("source_file_name")),
            section_path=_read_string(source_item.get("section_path")),
        ),
    )


def _to_referenced_chunk(item: dict[str, Any]) -> KnowhereReferencedChunk:
    return KnowhereReferencedChunk(
        document_id=_read_string(item.get("document_id")),
        chunk_id=_read_string(item.get("chunk_id")),
        chunk_type=_read_string(item.get("chunk_type")),
        section_path=_read_string(item.get("section_path")),
        file_path=_read_string(item.get("file_path")),
        source_file_name=_read_string(item.get("source_file_name")),
        job_id=_read_string(item.get("job_id")),
        score=_read_float(item.get("score")),
        asset_url=_read_string(item.get("asset_url")),
    )


def _read_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _read_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _read_object_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        dict(item)
        for item in value
        if isinstance(item, dict)
    ]
