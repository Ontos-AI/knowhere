"""Compatibility wrapper for shared.services.http.url_security."""

from shared.services.http.url_security import (
    BLOCKED_HOSTNAMES,
    DEVELOPMENT_ENVIRONMENTS,
    HTTP_SCHEMES,
    HTTPURLValidationResult,
    SafePublicHTTPURL,
    URLValidationFailureReason,
    validate_http_url_and_resolve_ip,
    validate_http_url_and_resolve_ip_async,
)

__all__ = [
    "BLOCKED_HOSTNAMES",
    "DEVELOPMENT_ENVIRONMENTS",
    "HTTP_SCHEMES",
    "HTTPURLValidationResult",
    "SafePublicHTTPURL",
    "URLValidationFailureReason",
    "validate_http_url_and_resolve_ip",
    "validate_http_url_and_resolve_ip_async",
]
