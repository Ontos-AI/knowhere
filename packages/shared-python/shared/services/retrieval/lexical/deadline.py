"""One monotonic budget shared by every stage of an indexing execution."""

from collections.abc import Iterator
from contextlib import contextmanager
import time

from gevent import Timeout

from shared.services.retrieval.lexical.error import LexicalIndexError


class IndexingDeadline:
    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("Indexing execution budget must be positive")
        self._startedAt: float = time.monotonic()
        self._expiresAt: float = self._startedAt + seconds
        self._settlement: IndexingDeadline | None = None

    def getRemainingSeconds(self) -> float:
        remaining: float = self._expiresAt - time.monotonic()
        if remaining <= 0:
            raise LexicalIndexError("Indexing execution deadline exceeded", isRetryable=True, hasUnknownOutcome=True, isTimeout=True)
        return remaining

    def getElapsedSeconds(self) -> float:
        return time.monotonic() - self._startedAt

    def startSettlement(self, seconds: float) -> "IndexingDeadline":
        if self._settlement is None:
            self._settlement = IndexingDeadline(seconds)
        return self._settlement

    def limitRemainingSeconds(self, seconds: float) -> None:
        self._expiresAt = min(self._expiresAt, time.monotonic() + seconds)
        self.getRemainingSeconds()

    @contextmanager
    def enforce(self) -> Iterator[None]:
        # Celery's gevent task cancellation is deliberately disabled. A local
        # cooperative timer bounds this execution without creating another task.
        with Timeout(self.getRemainingSeconds(), LexicalIndexError(
            "Indexing execution deadline exceeded", isRetryable=True, hasUnknownOutcome=True, isTimeout=True
        )):
            yield
            self.getRemainingSeconds()
