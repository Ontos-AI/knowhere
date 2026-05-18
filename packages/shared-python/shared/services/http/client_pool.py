"""
Singleton HTTP clients with connection pooling.
Reduces socket churn under high concurrency by reusing
TCP+TLS connections across requests.
"""

import threading
from typing import Optional

import httpx

_DEFAULT_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    keepalive_expiry=30,
)

_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=300.0,
    write=60.0,
    pool=30.0,
)

# --- Sync client (gevent-patched threading.Lock) ---
_sync_client: Optional[httpx.Client] = None
_sync_lock = threading.Lock()


def get_sync_client() -> httpx.Client:
    """Return a shared sync httpx.Client with connection pooling."""
    global _sync_client
    if _sync_client is None:
        with _sync_lock:
            if _sync_client is None:
                _sync_client = httpx.Client(
                    limits=_DEFAULT_LIMITS,
                    timeout=_DEFAULT_TIMEOUT,
                    follow_redirects=True,
                )
    return _sync_client


def close_sync_client() -> None:
    """Close the shared sync client. Call on worker shutdown."""
    global _sync_client
    with _sync_lock:
        if _sync_client is not None:
            _sync_client.close()
            _sync_client = None


# --- Async client ---
_async_client: Optional[httpx.AsyncClient] = None
_async_lock = threading.Lock()


def get_async_client() -> httpx.AsyncClient:
    """Return a shared async httpx.AsyncClient with connection pooling."""
    global _async_client
    if _async_client is None:
        with _async_lock:
            if _async_client is None:
                _async_client = httpx.AsyncClient(
                    limits=_DEFAULT_LIMITS,
                    timeout=_DEFAULT_TIMEOUT,
                    follow_redirects=True,
                )
    return _async_client


async def close_async_client() -> None:
    """Close the shared async client. Call on API shutdown."""
    global _async_client
    with _async_lock:
        client = _async_client
        _async_client = None
    if client is not None:
        await client.aclose()
