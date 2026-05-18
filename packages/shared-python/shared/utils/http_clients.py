"""Compatibility wrapper for shared.services.http.client_pool."""

from shared.services.http.client_pool import (
    close_async_client,
    close_sync_client,
    get_async_client,
    get_sync_client,
)

__all__ = [
    "close_async_client",
    "close_sync_client",
    "get_async_client",
    "get_sync_client",
]
