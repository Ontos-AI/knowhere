from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from time import perf_counter
from typing import AsyncContextManager, TypeVar

from app.services.auth.current_user_authentication_service import (
    get_current_user_authentication_service,
)
from app.services.rate_limit.data_structures import CurrentUser
from app.services.rate_limit.tier_service import TierService
from loguru import logger
from mcp.server.fastmcp import Context
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace


KNOWHERE_NAMESPACE_HEADER = "x-knowhere-namespace"
MCP_TIMING_LOGS_ENABLED_ENV = "MCP_TIMING_LOGS_ENABLED"
MCP_TIMING_LOG_TRUE_VALUES = {"1", "true", "yes", "on"}

ResultT = TypeVar("ResultT")
McpToolOperation = Callable[[AsyncSession, CurrentUser, str], Awaitable[ResultT]]
McpToolResultCounter = Callable[[ResultT], Mapping[str, int | float | str | None]]
DbFactory = Callable[[], AsyncContextManager[AsyncSession]]


class McpToolRuntime:
    """Run MCP tool operations through the shared auth, namespace, and DB seam."""

    def __init__(self, db_factory: DbFactory | None = None) -> None:
        self._db_factory = db_factory or _create_db_context

    async def run(
        self,
        *,
        ctx: Context | None,
        namespace: str | None,
        tool_name: str,
        operation: McpToolOperation[ResultT],
        count_result: McpToolResultCounter[ResultT] | None = None,
    ) -> ResultT:
        total_started_at = perf_counter()
        effective_namespace: str | None = None
        namespace_ms: float | None = None
        auth_ms: float | None = None
        service_ms: float | None = None
        try:
            namespace_started_at = perf_counter()
            effective_namespace = _resolve_mcp_namespace(ctx=ctx, namespace=namespace)
            namespace_ms = _elapsed_ms(namespace_started_at)

            async with self._db_factory() as db:
                auth_started_at = perf_counter()
                current_user = await _resolve_mcp_current_user(ctx=ctx, db=db)
                auth_ms = _elapsed_ms(auth_started_at)

                service_started_at = perf_counter()
                result = await operation(db, current_user, effective_namespace)
                service_ms = _elapsed_ms(service_started_at)
                _log_mcp_tool_timing(
                    tool_name=tool_name,
                    namespace=effective_namespace,
                    total_started_at=total_started_at,
                    namespace_ms=namespace_ms,
                    auth_ms=auth_ms,
                    service_ms=service_ms,
                    result_counts=dict(count_result(result)) if count_result else None,
                )
                return result
        except Exception as exc:
            _log_mcp_tool_timing(
                tool_name=tool_name,
                namespace=effective_namespace,
                total_started_at=total_started_at,
                namespace_ms=namespace_ms,
                auth_ms=auth_ms,
                service_ms=service_ms,
                error=exc,
            )
            raise


def are_mcp_timing_logs_enabled() -> bool:
    raw_value = os.getenv(MCP_TIMING_LOGS_ENABLED_ENV, "false")
    return raw_value.strip().lower() in MCP_TIMING_LOG_TRUE_VALUES


def _create_db_context() -> AsyncContextManager[AsyncSession]:
    from shared.core.database import get_db_context

    return get_db_context()


def _get_header(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for candidate in (name, name.lower(), name.title()):
        value = getter(candidate)
        if value is not None:
            return str(value)
    return None


def _get_mcp_request(ctx: Context | None) -> object:
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    if request is None:
        raise RuntimeError("MCP auth context request is not available")
    return request


def _resolve_mcp_namespace(
    *,
    ctx: Context | None,
    namespace: str | None = None,
) -> str:
    if namespace is not None:
        return normalize_retrieval_namespace(namespace)
    try:
        request = _get_mcp_request(ctx)
    except (RuntimeError, ValueError):
        return normalize_retrieval_namespace(None)
    headers = getattr(request, "headers", None)
    header_namespace = _get_header(headers, KNOWHERE_NAMESPACE_HEADER)
    return normalize_retrieval_namespace(header_namespace)


async def _resolve_mcp_current_user(*, ctx: Context | None, db: AsyncSession) -> CurrentUser:
    request = _get_mcp_request(ctx)
    headers = getattr(request, "headers", None)
    authorization = _get_header(headers, "authorization")
    user_id = await get_current_user_authentication_service().authenticate_authorization_header(
        db,
        authorization=authorization,
    )
    user_tier = await TierService.get_tier_for_session(db, user_id)
    return CurrentUser(user_id=user_id, user_tier=user_tier)


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def _log_mcp_tool_timing(
    *,
    tool_name: str,
    namespace: str | None,
    total_started_at: float,
    namespace_ms: float | None = None,
    auth_ms: float | None = None,
    service_ms: float | None = None,
    result_counts: Mapping[str, int | float | str | None] | None = None,
    error: BaseException | None = None,
) -> None:
    if not are_mcp_timing_logs_enabled():
        return

    logger.bind(
        event="mcp_tool_timing",
        tool_name=tool_name,
        namespace=namespace,
        total_ms=_elapsed_ms(total_started_at),
        namespace_ms=namespace_ms,
        auth_ms=auth_ms,
        service_ms=service_ms,
        status="error" if error is not None else "ok",
        error_type=type(error).__name__ if error is not None else None,
        **dict(result_counts or {}),
    ).info("mcp tool timing")
