"""FastAPI route-fact adapters for the Job Admission workflow."""

from app.services.rate_limit.data_structures import RouteAdmissionContext
from fastapi import Request


def get_route_admission_context(request: Request) -> RouteAdmissionContext:
    """Extract route facts needed by Job Admission from a FastAPI request."""
    return RouteAdmissionContext(
        method=request.method,
        path=_get_route_path(request),
        limit_identifier=_get_route_limit_identifier(request),
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
