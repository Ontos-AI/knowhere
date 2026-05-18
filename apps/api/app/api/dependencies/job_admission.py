"""FastAPI adapters for the Job Admission workflow."""

from typing import AsyncGenerator

from app.api.dependencies.auth import get_current_user_id
from app.services.rate_limit.data_structures import (
    CurrentUser,
    RouteAdmissionContext,
)
from app.services.rate_limit.job_admission_service import JobAdmissionService
from fastapi import Depends, Request

_job_admission_service = JobAdmissionService()


def get_route_admission_context(request: Request) -> RouteAdmissionContext:
    """Extract route facts needed by Job Admission from a FastAPI request."""
    return RouteAdmissionContext(
        method=request.method,
        path=_get_route_path(request),
        limit_identifier=_get_route_limit_identifier(request),
    )


async def with_current_user(
    route_context: RouteAdmissionContext = Depends(get_route_admission_context),
    user_id: str = Depends(get_current_user_id),
) -> AsyncGenerator[CurrentUser, None]:
    current_user = await _job_admission_service.resolve_current_user(
        route_context=route_context,
        user_id=user_id,
    )
    yield current_user


async def require_billing_limits(
    current_user: CurrentUser = Depends(with_current_user),
) -> AsyncGenerator[CurrentUser, None]:
    await _job_admission_service.enforce_billing_limits(current_user=current_user)
    yield current_user


async def require_route_system_limit(
    route_context: RouteAdmissionContext = Depends(get_route_admission_context),
) -> None:
    await _job_admission_service.enforce_route_system_limit(
        route_context=route_context,
    )


def _get_route_path(request: Request) -> str:
    scope_path = request.scope.get("path", request.url.path)
    root_path = request.scope.get("root_path", "")
    if isinstance(scope_path, str) and isinstance(root_path, str):
        if root_path and scope_path.startswith(root_path):
            return scope_path[len(root_path) :]
        return scope_path
    return request.url.path


def _get_route_limit_identifier(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path

    route_path_format = getattr(route, "path_format", None)
    if isinstance(route_path_format, str) and route_path_format:
        return route_path_format

    return _get_route_path(request)
