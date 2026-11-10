"""Check every compiler checkpoint and periodically let other work run."""

import asyncio
import time

import gevent

from shared.services.retrieval.lexical.deadline import IndexingDeadline

_YIELD_INTERVAL_SECONDS: float = 0.001


class CompilationCheckpoint:
    def __init__(self, deadline: IndexingDeadline | None) -> None:
        self._deadline: IndexingDeadline | None = deadline
        self._nextYield: float = 0

    def _checkDeadline(self) -> None:
        if self._deadline is not None:
            self._deadline.getRemainingSeconds()

    def _shouldYield(self) -> bool:
        self._checkDeadline()
        now: float = time.monotonic()
        if now < self._nextYield:
            return False
        self._nextYield = now + _YIELD_INTERVAL_SECONDS
        return True

    def check(self) -> None:
        if self._shouldYield():
            gevent.sleep(0)
            self._checkDeadline()

    async def checkAsync(self) -> None:
        if self._shouldYield():
            await asyncio.sleep(0)
            self._checkDeadline()
