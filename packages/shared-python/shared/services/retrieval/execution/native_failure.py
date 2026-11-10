"""Retain a request's first native failure across tool and SDK wrappers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from shared.services.retrieval.lexical.error import LexicalIndexError

_ACTIVE_FAILURE: ContextVar[NativeFailureState | None] = ContextVar("native_retrieval_failure", default=None)


class NativeFailureState:
    """A shared fatal outcome, including SDK callbacks running in other threads.

    Tool argument mistakes and empty matches never enter this state. Keeping
    the exception independently of a provider's return value prevents a broad
    provider wrapper from converting a failed request into partial success.
    """

    def __init__(self) -> None:
        self._failure: LexicalIndexError | None = None
        self._lock: Lock = Lock()

    @staticmethod
    def current() -> NativeFailureState | None:
        return _ACTIVE_FAILURE.get()

    @staticmethod
    def raiseCurrent() -> None:
        state: NativeFailureState | None = NativeFailureState.current()
        if state is not None:
            state.raiseIfFailed()

    @staticmethod
    def recordCurrent(error: LexicalIndexError) -> None:
        state: NativeFailureState | None = NativeFailureState.current()
        if state is not None:
            state.record(error)

    @property
    def hasFailed(self) -> bool:
        return self.failure is not None

    @property
    def failure(self) -> LexicalIndexError | None:
        with self._lock:
            return self._failure

    def record(self, error: LexicalIndexError) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error

    def raiseIfFailed(self) -> None:
        failure: LexicalIndexError | None = self.failure
        if failure is not None:
            raise failure

    @contextmanager
    def bind(self) -> Iterator[None]:
        token = _ACTIVE_FAILURE.set(self)
        try:
            yield
        finally:
            _ACTIVE_FAILURE.reset(token)
