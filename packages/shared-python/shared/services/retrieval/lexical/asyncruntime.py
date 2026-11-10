"""Pool native HTTP connections within one API event loop."""

import asyncio
import os
from typing import ClassVar
from weakref import WeakKeyDictionary

import httpx

from shared.services.retrieval.lexical.clientconfig import NativeClientConfig
from shared.services.retrieval.lexical.error import LexicalIndexError


class AsyncOpenSearchRuntime:
    _instances: ClassVar[WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str], "AsyncOpenSearchRuntime"]]] = WeakKeyDictionary()

    def __init__(self, endpoint: str, *, configuration: NativeClientConfig | None = None) -> None:
        try:
            configuration = configuration or NativeClientConfig.resolve(endpoint)
        except ValueError:
            raise LexicalIndexError("Native client configuration is unavailable", isRetryable=True) from None
        self._processId: int = os.getpid()
        # Preserve the previous 32-channel ceiling instead of using additional
        # backend concurrency to hide a caller-side execution change.
        limits: httpx.Limits = httpx.Limits(max_connections=32, max_keepalive_connections=20)
        self.client: httpx.AsyncClient = httpx.AsyncClient(base_url=configuration.endpoint, timeout=30,
            auth=configuration.auth, trust_env=False,
            transport=httpx.AsyncHTTPTransport(verify=configuration.verify, limits=limits, retries=0, trust_env=False))

    @classmethod
    def resolve(cls, endpoint: str) -> "AsyncOpenSearchRuntime":
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        runtimes: dict[tuple[str, str], AsyncOpenSearchRuntime] = cls._instances.setdefault(loop, {})
        try:
            configuration: NativeClientConfig = NativeClientConfig.resolve(endpoint)
        except ValueError:
            raise LexicalIndexError("Native client configuration is unavailable", isRetryable=True) from None
        key: tuple[str, str] = (configuration.endpoint, configuration.identity)
        existing: AsyncOpenSearchRuntime | None = runtimes.get(key)
        if existing is None or existing._processId != os.getpid():
            if sum(identity[0] == key[0] and runtime._processId == os.getpid()
                   for identity, runtime in runtimes.items()) >= 2:
                raise LexicalIndexError("Native credential rotation requires a coordinated restart before another generation", isRetryable=True)
            existing = cls(key[0], configuration=configuration)
            runtimes[key] = existing
        return existing

    @classmethod
    async def closeCurrent(cls) -> None:
        runtimes: dict[tuple[str, str], AsyncOpenSearchRuntime] = cls._instances.pop(asyncio.get_running_loop(), {})
        for runtime in runtimes.values():
            await runtime.client.aclose()
