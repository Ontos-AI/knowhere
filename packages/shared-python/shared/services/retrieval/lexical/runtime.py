"""Reuse native connections and bounded channel workers within one process."""

from __future__ import annotations

import atexit
from contextvars import copy_context
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
import os
from threading import RLock
from typing import ClassVar, TypeVar

import httpx

from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.clientconfig import NativeClientConfig

_Response = TypeVar("_Response")
_CHANNEL_WORKERS: int = 32


class OpenSearchRuntime:
    _instances: ClassVar[dict[tuple[int, str, str], OpenSearchRuntime]] = {}
    _lock: ClassVar[RLock] = RLock()

    def __init__(self, endpoint: str, *, configuration: NativeClientConfig | None = None) -> None:
        try:
            configuration = configuration or NativeClientConfig.resolve(endpoint)
        except ValueError:
            raise LexicalIndexError("Native client configuration is unavailable", isRetryable=True) from None
        self.client: httpx.Client = httpx.Client(base_url=configuration.endpoint, timeout=30,
            auth=configuration.auth, trust_env=False,
            transport=httpx.HTTPTransport(verify=configuration.verify, retries=0, trust_env=False))
        # Matches the previous maximum of two channels per 16 active replay
        # threads, while avoiding a fresh executor for every query.
        self._channels: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=_CHANNEL_WORKERS, thread_name_prefix="lexical-channel")
        atexit.register(self.close)

    @classmethod
    def resolve(cls, endpoint: str) -> OpenSearchRuntime:
        try:
            configuration: NativeClientConfig = NativeClientConfig.resolve(endpoint)
        except ValueError:
            raise LexicalIndexError("Native client configuration is unavailable", isRetryable=True) from None
        key: tuple[int, str, str] = (os.getpid(), configuration.endpoint, configuration.identity)
        with cls._lock:
            existing: OpenSearchRuntime | None = cls._instances.get(key)
            if existing is None:
                if sum(identity[:2] == key[:2] for identity in cls._instances) >= 2:
                    raise LexicalIndexError("Native credential rotation requires a coordinated restart before another generation", isRetryable=True)
                existing = cls(key[1], configuration=configuration)
                cls._instances[key] = existing
            return existing

    def queryChannels(self, query: Callable[[str], _Response]) -> tuple[_Response, _Response]:
        futures: tuple[Future[_Response], Future[_Response]] = (
            self._channels.submit(copy_context().run, query, "pathTokens"),
            self._channels.submit(copy_context().run, query, "contentTokens"),
        )
        # Settle both channels before releasing admission. An unknown outcome
        # takes priority even when the other channel failed with a known reply.
        wait(futures)
        for future in futures:
            error: BaseException | None = future.exception()
            if isinstance(error, LexicalIndexError) and error.hasUnknownOutcome:
                raise error
        return futures[0].result(), futures[1].result()

    @classmethod
    def closeCurrent(cls) -> None:
        with cls._lock:
            keys: tuple[tuple[int, str, str], ...] = tuple(
                key for key in cls._instances if key[0] == os.getpid()
            )
            runtimes: tuple[OpenSearchRuntime, ...] = tuple(
                cls._instances.pop(key) for key in keys
            )
        for runtime in runtimes:
            runtime.close()

    def close(self) -> None:
        with self._lock:
            for key in tuple(self._instances):
                if self._instances[key] is self:
                    self._instances.pop(key)
        self._channels.shutdown(wait=True)
        self.client.close()
