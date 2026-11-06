"""Renew authority without retaining a SQL session while remote work runs."""

from collections.abc import Callable
from threading import Event, Thread
from types import TracebackType

from shared.services.retrieval.lexical.error import LexicalIndexError


class LeaseHeartbeat:
    def __init__(self, renew: Callable[[], bool], intervalSeconds: float = 15) -> None:
        self._renew: Callable[[], bool] = renew
        self._interval: float = intervalSeconds
        self._stop: Event = Event()
        self._lost: Event = Event()
        self._thread: Thread = Thread(target=self._run, daemon=True)

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.stop()
        self._thread.join(timeout=5)

    def stop(self) -> None:
        self._stop.set()

    def assertOwned(self) -> None:
        if self._lost.is_set():
            raise LexicalIndexError("Lexical operation lost its lease", isRetryable=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if self._renew():
                    continue
            except Exception:
                pass
            self._lost.set()
            return
