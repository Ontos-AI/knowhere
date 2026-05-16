from __future__ import annotations

from fnmatch import fnmatch

from app.services.rate_limit.config import RateLimitConfig
from app.services.rate_limit.limiter import RateLimiter
from app.services.rate_limit.system_limit import find_system_rule
from fastapi import Request

from shared.core.exceptions.domain_exceptions import (
    PermissionDeniedException,
    RateLimitException,
    UnavailableException,
)

_GUEST_API_KEY_ALLOWED_ROUTE_PATTERNS: tuple[str, ...] = (
    "/v1/jobs",
    "/v1/jobs/*",
    "/v1/billing/credits",
    "/v1/retrieval/query",
    "/v1/documents",
    "/v1/documents/*",
    "/mcp",
)
_GUEST_API_KEY_REQUIRED_PERMISSION: str = (
    "jobs_documents_retrieval_mcp_or_billing_credits"
)
_GUEST_API_KEY_SCOPE_MESSAGE: str = (
    "Guest API keys can only access job, document, retrieval, MCP query, "
    "and billing credits APIs"
)
_RETRY_AFTER_SECONDS: int = 15


class JobAdmissionRoutePolicyService:
    async def enforce_user_system_limit(
        self,
        *,
        request: Request,
        config: RateLimitConfig,
        user_id: str,
    ) -> None:
        route_path = self._get_route_path(request)
        rule = find_system_rule(request.method, route_path, config.system_rules)
        limiter = RateLimiter(config)
        await limiter.check_system_limit(
            identifier=user_id,
            limit=rule.limit,
            matched_pattern=rule.api_pattern,
            period=rule.period,
        )

    async def enforce_route_system_limit(self, *, request: Request) -> None:
        config = RateLimitConfig.get_instance()
        if not config.is_enabled:
            return

        route_path = self._get_route_path(request)
        route_identifier = self._get_route_limit_identifier(request)
        rule = find_system_rule(request.method, route_path, config.system_rules)
        limiter = RateLimiter(config)

        try:
            await limiter.check_system_limit(
                identifier=route_identifier,
                limit=rule.limit,
                matched_pattern=rule.api_pattern,
                period=rule.period,
                use_global_key=True,
            )
        except RateLimitException:
            raise
        except Exception as exc:
            raise UnavailableException(
                internal_message=(f"Redis error in route system limit: {exc}"),
                retry_after=_RETRY_AFTER_SECONDS,
                limit=rule.limit,
                period=rule.period,
            )

    def enforce_guest_api_key_scope(
        self,
        *,
        request: Request,
        user_tier: str,
    ) -> None:
        if user_tier != "guest":
            return

        route_path = self._get_route_path(request)
        if self._is_guest_api_key_route_allowed(route_path):
            return

        raise PermissionDeniedException(
            user_message=_GUEST_API_KEY_SCOPE_MESSAGE,
            required_permission=_GUEST_API_KEY_REQUIRED_PERMISSION,
        )

    def _get_route_path(self, request: Request) -> str:
        scope_path = request.scope.get("path", request.url.path)
        root_path = request.scope.get("root_path", "")
        if isinstance(scope_path, str) and isinstance(root_path, str):
            if root_path and scope_path.startswith(root_path):
                return scope_path[len(root_path) :]
            return scope_path
        return request.url.path

    def _get_route_limit_identifier(self, request: Request) -> str:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and route_path:
            return route_path

        route_path_format = getattr(route, "path_format", None)
        if isinstance(route_path_format, str) and route_path_format:
            return route_path_format

        return self._get_route_path(request)

    def _normalize_route_path(self, route_path: str) -> str:
        normalized_path = route_path.rstrip("/")
        return normalized_path or "/"

    def _is_guest_api_key_route_allowed(self, route_path: str) -> bool:
        normalized_path = self._normalize_route_path(route_path)
        return any(
            fnmatch(normalized_path, pattern)
            for pattern in _GUEST_API_KEY_ALLOWED_ROUTE_PATTERNS
        )
