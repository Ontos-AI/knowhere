"""Compatibility wrapper for shared.services.http.pinned_outbound."""

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

__all__ = [
    "PinnedDownloadResult",
    "PinnedHTTPConnection",
    "PinnedHTTPConnectionPool",
    "PinnedHTTPSConnection",
    "PinnedHTTPSConnectionPool",
    "PinnedIPResolver",
    "PinnedOutboundResponse",
    "download_pinned_outbound_file",
    "download_pinned_outbound_file_async",
    "send_pinned_outbound_request",
]
