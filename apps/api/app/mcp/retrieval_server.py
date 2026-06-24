from __future__ import annotations

from typing import Annotated, Any, Literal

from app.mcp.job_tools import register_job_tools
from app.mcp.tool_runtime import (
    DbFactory,
    McpToolRuntime,
    are_mcp_timing_logs_enabled as _are_mcp_timing_logs_enabled,
)
from app.services.document_ingestion import DocumentIngestionService
from app.services.documents.inspection_service import (
    DEFAULT_GREP_RESULT_LIMIT,
    MAX_GREP_RESULT_LIMIT,
    DocumentGrepChunksResponse,
    DocumentInspectionService,
    DocumentListResponse,
    DocumentOutlineResponse,
    DocumentReadChunksResponse,
)
from app.services.rate_limit.data_structures import CurrentUser
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.app_service import run_retrieval_query
from shared.services.retrieval.settings import DEFAULT_TOP_K

KnowhereTargetContent = Literal[
    "all",
    "text",
    "image",
    "table",
    "text_image",
    "text_table",
]
KnowhereFilterMode = Literal["delete", "keep"]

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


def are_mcp_timing_logs_enabled() -> bool:
    return _are_mcp_timing_logs_enabled()


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


def create_retrieval_mcp_server(
    *,
    db_factory: DbFactory | None = None,
    streamable_http_path: str = "/mcp",
    document_inspection_service: DocumentInspectionService | None = None,
    document_ingestion_service: DocumentIngestionService | None = None,
):
    runtime = McpToolRuntime(db_factory=db_factory)
    inspection_service = document_inspection_service or DocumentInspectionService()
    ingestion_service = document_ingestion_service or DocumentIngestionService()
    server = FastMCP(
        "knowhere-retrieval",
        instructions=(
            "Use this server to inspect and retrieve from published Knowhere "
            "documents and to start URL parse jobs. Use knowhere_parse_url to "
            "parse a remote document URL, then poll knowhere_get_job_status "
            "until the job is done or failed. Use knowhere_search for discovery "
            "when the target document is unknown, then use "
            "knowhere_get_document_outline, knowhere_read_chunks, or "
            "knowhere_grep_chunks with exact document and chunk identifiers for "
            "bounded follow-up."
        ),
        streamable_http_path=streamable_http_path,
        json_response=True,
        stateless_http=True,
        transport_security=create_public_mcp_transport_security(),
    )
    register_job_tools(
        server=server,
        runtime=runtime,
        document_ingestion_service=ingestion_service,
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
        async def run_search(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> KnowhereSearchResponse:
            response = await run_retrieval_query(
                db=db,
                user_id=current_user.user_id,
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

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_search",
            operation=run_search,
            count_result=_count_search_response,
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
        async def list_documents(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> DocumentListResponse:
            return await inspection_service.list_documents(
                db,
                user_id=current_user.user_id,
                namespace=effective_namespace,
            )

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_list_documents",
            operation=list_documents,
            count_result=_count_document_list_response,
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
        async def get_document_outline(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> DocumentOutlineResponse:
            response = await inspection_service.get_document_outline(
                db,
                user_id=current_user.user_id,
                namespace=effective_namespace,
                document_id=document_id,
            )
            if response is None:
                raise ValueError("Document not found or not active in namespace.")
            return response

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_get_document_outline",
            operation=get_document_outline,
            count_result=_count_document_outline_response,
        )

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
            Field(description="Optional 1-based chunk position to start reading."),
        ] = None,
        end_chunk: Annotated[
            int | None,
            Field(description="Optional 1-based chunk position to stop reading."),
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
        async def read_chunks(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> DocumentReadChunksResponse:
            response = await inspection_service.read_chunks(
                db,
                user_id=current_user.user_id,
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

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_read_chunks",
            operation=read_chunks,
            count_result=_count_read_chunks_response,
        )

    @server.tool(
        name="knowhere_grep_chunks",
        description=(
            "Search one active Knowhere document's chunks by literal text or "
            "regular expression. Results are ordered by chunk position and "
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
            Field(description="Treat pattern as a regular expression."),
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
        async def grep_chunks(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> DocumentGrepChunksResponse:
            response = await inspection_service.grep_chunks(
                db,
                user_id=current_user.user_id,
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

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_grep_chunks",
            operation=grep_chunks,
            count_result=_count_grep_chunks_response,
        )

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


def _count_search_response(
    response: KnowhereSearchResponse,
) -> dict[str, int | float | str | None]:
    return {
        "referenced_chunk_count": len(response.referenced_chunks),
        "result_count": len(response.results),
    }


def _count_document_list_response(
    response: DocumentListResponse,
) -> dict[str, int | float | str | None]:
    return {"document_count": len(response.documents)}


def _count_document_outline_response(
    response: DocumentOutlineResponse,
) -> dict[str, int | float | str | None]:
    return {
        "section_count": len(response.sections),
        "total_chunks": response.total_chunks,
    }


def _count_read_chunks_response(
    response: DocumentReadChunksResponse,
) -> dict[str, int | float | str | None]:
    return {"chunk_count": len(response.chunks)}


def _count_grep_chunks_response(
    response: DocumentGrepChunksResponse,
) -> dict[str, int | float | str | None]:
    return {
        "match_count": len(response.matches),
        "scanned_chunks": response.scanned_chunks,
    }


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
