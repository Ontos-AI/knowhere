from __future__ import annotations

from typing import Annotated, Literal

from app.mcp.tool_runtime import McpToolRuntime
from app.services.document_ingestion import DocumentIngestionService
from app.services.jobs import get_job_result_for_user
from app.services.rate_limit.data_structures import CurrentUser
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.job import (
    JobCreate,
    JobResponse,
    JobResultResponse,
    ParsingParams,
)

__all__ = ["register_job_tools"]


class KnowhereParseUrlResponse(BaseModel):
    namespace: str
    job: JobResponse
    interpretation: str = Field(
        default=(
            "Parse job created. Poll knowhere_get_job_status until status is done "
            "or failed."
        )
    )


class KnowhereJobStatusResponse(BaseModel):
    namespace: str | None = None
    job: JobResultResponse
    is_terminal: bool
    is_success: bool
    is_failure: bool
    interpretation: str


def register_job_tools(
    *,
    server: FastMCP,
    runtime: McpToolRuntime,
    document_ingestion_service: DocumentIngestionService | None = None,
) -> None:
    ingestion_service = document_ingestion_service or DocumentIngestionService()

    @server.tool(
        name="knowhere_parse_url",
        description=(
            "Start parsing a remote document URL with Knowhere. This creates a "
            "background ingestion job and returns a job ID. Use "
            "knowhere_get_job_status to poll until the job status is done or "
            "failed. This server-side MCP tool accepts URLs only; it cannot read "
            "local file paths from the client machine."
        ),
        structured_output=True,
    )
    async def knowhere_parse_url(
        url: Annotated[
            str,
            Field(description="Remote http(s) document URL to parse."),
        ],
        namespace: Annotated[
            str | None,
            Field(
                description=(
                    "Optional retrieval namespace override. If omitted, the "
                    "x-knowhere-namespace header is used, then default."
                )
            ),
        ] = None,
        document_id: Annotated[
            str | None,
            Field(description="Optional existing document ID for update flows."),
        ] = None,
        data_id: Annotated[
            str | None,
            Field(description="Optional user-defined correlation ID."),
        ] = None,
        parse_track: Annotated[
            Literal["chunk", "page_memory"],
            Field(description="Parser track. Defaults to chunk."),
        ] = "chunk",
        parsing_params: Annotated[
            ParsingParams | None,
            Field(description="Optional Knowhere parsing parameters."),
        ] = None,
        ctx: Context | None = None,
    ) -> KnowhereParseUrlResponse:
        async def parse_url(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> KnowhereParseUrlResponse:
            response = await ingestion_service.create_job(
                db,
                payload=JobCreate(
                    namespace=effective_namespace,
                    document_id=document_id,
                    source_type="url",
                    source_url=url,
                    file_name=None,
                    data_id=data_id,
                    parse_track=parse_track,
                    parsing_params=parsing_params,
                    webhook=None,
                ),
                current_user=current_user,
            )
            return KnowhereParseUrlResponse(
                namespace=response.namespace or effective_namespace,
                job=response,
                interpretation=(
                    "Parse job created. Poll knowhere_get_job_status until "
                    "status is done or failed."
                ),
            )

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_parse_url",
            operation=parse_url,
            count_result=_count_parse_url_response,
        )

    @server.tool(
        name="knowhere_get_job_status",
        description=(
            "Check one Knowhere parsing job by job ID. Use this to poll jobs "
            "created by knowhere_parse_url. Do not treat a non-terminal job as "
            "stuck just because progress is unchanged; only done and failed are "
            "terminal statuses."
        ),
        structured_output=True,
    )
    async def knowhere_get_job_status(
        job_id: Annotated[
            str,
            Field(description="Knowhere job ID returned by knowhere_parse_url."),
        ],
        namespace: Annotated[
            str | None,
            Field(
                description=(
                    "Optional retrieval namespace override for auth context and "
                    "response consistency. Job access is still scoped by owner."
                )
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> KnowhereJobStatusResponse:
        async def get_job_status(
            db: AsyncSession,
            current_user: CurrentUser,
            effective_namespace: str,
        ) -> KnowhereJobStatusResponse:
            response = await get_job_result_for_user(
                db,
                job_id=job_id,
                user_id=current_user.user_id,
            )
            return _to_job_status_response(
                response,
                fallback_namespace=effective_namespace,
            )

        return await runtime.run(
            ctx=ctx,
            namespace=namespace,
            tool_name="knowhere_get_job_status",
            operation=get_job_status,
            count_result=_count_job_status_response,
        )


def _to_job_status_response(
    response: JobResultResponse,
    *,
    fallback_namespace: str,
) -> KnowhereJobStatusResponse:
    is_success = response.status == "done"
    is_failure = response.status == "failed"
    is_terminal = is_success or is_failure
    if is_success:
        interpretation = "completed"
    elif is_failure:
        interpretation = (
            "failed. Surface the returned error to the user and do not fall back "
            "to another parser for supported documents."
        )
    else:
        interpretation = (
            "still running. Poll again later; do not call the job stuck unless "
            "Knowhere returns a terminal failure."
        )

    return KnowhereJobStatusResponse(
        namespace=response.namespace or fallback_namespace,
        job=response,
        is_terminal=is_terminal,
        is_success=is_success,
        is_failure=is_failure,
        interpretation=interpretation,
    )


def _count_parse_url_response(
    response: KnowhereParseUrlResponse,
) -> dict[str, int | float | str | None]:
    return {
        "job_id": response.job.job_id,
        "job_status": response.job.status,
        "document_id": response.job.document_id,
    }


def _count_job_status_response(
    response: KnowhereJobStatusResponse,
) -> dict[str, int | float | str | None]:
    return {
        "job_id": response.job.job_id,
        "job_status": response.job.status,
        "is_terminal": str(response.is_terminal).lower(),
    }
