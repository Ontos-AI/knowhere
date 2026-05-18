"""Public URL and outbound HTTP policy service exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.services.http.client_pool import (
        close_async_client,
        close_sync_client,
        get_async_client,
        get_sync_client,
    )
    from shared.services.http.pinned_outbound import (
        PinnedDownloadResult,
        PinnedHTTPConnection,
        PinnedHTTPConnectionPool,
        PinnedHTTPSConnection,
        PinnedHTTPSConnectionPool,
        PinnedIPResolver,
        PinnedOutboundResponse,
        download_pinned_outbound_file,
        download_pinned_outbound_file_async,
        send_pinned_outbound_request,
    )
    from shared.services.http.url_file_type import (
        CONTENT_TYPE_TO_EXTENSION,
        MAX_SAFE_REDIRECTS,
        REDIRECT_STATUS_CODES,
        URL_VALIDATION_DESCRIPTIONS,
        resolve_file_extension_async,
        resolve_file_extension_sync,
    )
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
    "CONTENT_TYPE_TO_EXTENSION",
    "DEVELOPMENT_ENVIRONMENTS",
    "HTTP_SCHEMES",
    "HTTPURLValidationResult",
    "MAX_SAFE_REDIRECTS",
    "PinnedDownloadResult",
    "PinnedHTTPConnection",
    "PinnedHTTPConnectionPool",
    "PinnedHTTPSConnection",
    "PinnedHTTPSConnectionPool",
    "PinnedIPResolver",
    "PinnedOutboundResponse",
    "REDIRECT_STATUS_CODES",
    "SafePublicHTTPURL",
    "URLValidationFailureReason",
    "URL_VALIDATION_DESCRIPTIONS",
    "close_async_client",
    "close_sync_client",
    "download_pinned_outbound_file",
    "download_pinned_outbound_file_async",
    "get_async_client",
    "get_sync_client",
    "resolve_file_extension_async",
    "resolve_file_extension_sync",
    "send_pinned_outbound_request",
    "validate_http_url_and_resolve_ip",
    "validate_http_url_and_resolve_ip_async",
]

_EXPORT_MODULES: dict[str, str] = {
    "BLOCKED_HOSTNAMES": "shared.services.http.url_security",
    "CONTENT_TYPE_TO_EXTENSION": "shared.services.http.url_file_type",
    "DEVELOPMENT_ENVIRONMENTS": "shared.services.http.url_security",
    "HTTP_SCHEMES": "shared.services.http.url_security",
    "HTTPURLValidationResult": "shared.services.http.url_security",
    "MAX_SAFE_REDIRECTS": "shared.services.http.url_file_type",
    "PinnedDownloadResult": "shared.services.http.pinned_outbound",
    "PinnedHTTPConnection": "shared.services.http.pinned_outbound",
    "PinnedHTTPConnectionPool": "shared.services.http.pinned_outbound",
    "PinnedHTTPSConnection": "shared.services.http.pinned_outbound",
    "PinnedHTTPSConnectionPool": "shared.services.http.pinned_outbound",
    "PinnedIPResolver": "shared.services.http.pinned_outbound",
    "PinnedOutboundResponse": "shared.services.http.pinned_outbound",
    "REDIRECT_STATUS_CODES": "shared.services.http.url_file_type",
    "SafePublicHTTPURL": "shared.services.http.url_security",
    "URLValidationFailureReason": "shared.services.http.url_security",
    "URL_VALIDATION_DESCRIPTIONS": "shared.services.http.url_file_type",
    "close_async_client": "shared.services.http.client_pool",
    "close_sync_client": "shared.services.http.client_pool",
    "download_pinned_outbound_file": "shared.services.http.pinned_outbound",
    "download_pinned_outbound_file_async": "shared.services.http.pinned_outbound",
    "get_async_client": "shared.services.http.client_pool",
    "get_sync_client": "shared.services.http.client_pool",
    "resolve_file_extension_async": "shared.services.http.url_file_type",
    "resolve_file_extension_sync": "shared.services.http.url_file_type",
    "send_pinned_outbound_request": "shared.services.http.pinned_outbound",
    "validate_http_url_and_resolve_ip": "shared.services.http.url_security",
    "validate_http_url_and_resolve_ip_async": "shared.services.http.url_security",
}


def __getattr__(name: str) -> Any:
    """Load HTTP service exports on first access."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return public HTTP package exports."""
    return sorted([*globals(), *__all__])
