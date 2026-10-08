"""Bounded reuse of pure lexical preparation during canonical demo publication."""

from __future__ import annotations

import sys
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from hashlib import sha256

from shared.utils.text_utils import tokenize_for_retrieval

_PREPARATION_FORMAT_VERSION = 1
_MAX_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 4096
_Scope = tuple[str, str, int, int]
_Key = tuple[_Scope, str, bool, bytes]
_active_scope: ContextVar[_Scope | None] = ContextVar(
    "demo_publication_preparation_scope", default=None
)


@dataclass(frozen=True)
class _PreparedValue:
    tokens: tuple[str, ...] = ()
    frequencies: tuple[tuple[str, int], ...] = ()
    size_bytes: int = 0


class PublicationPreparationCache:
    """Reuse token sequences and frequencies without storing owner-specific rows.

    Only the canonical materializer opens a scope. Ordinary ingestion, backfills
    and retrieval call the same authoritative tokenizer without cache access.
    The exact text digest and tokenization mode are part of every key; source
    and algorithm versions isolate changed artifacts and preparation semantics.
    """

    _entries: OrderedDict[_Key, _PreparedValue] = OrderedDict()
    _size_bytes: int = 0
    _lock: threading.RLock = threading.RLock()

    @classmethod
    @contextmanager
    def scope(
        cls, *, source_id: str, content_version: str, index_format_version: int
    ) -> Iterator[None]:
        if not source_id or not content_version:
            raise ValueError("Demo preparation reuse requires source and content versions")
        scope: _Scope = (
            source_id, content_version, index_format_version, _PREPARATION_FORMAT_VERSION
        )
        token: Token[_Scope | None] = _active_scope.set(scope)
        try:
            yield
        finally:
            _active_scope.reset(token)

    @classmethod
    def tokenize(cls, text: str, *, dedupe: bool = False) -> list[str]:
        key: _Key | None = cls._build_key(text, operation="tokens", dedupe=dedupe)
        cached: _PreparedValue | None = cls._get(key)
        if cached is not None:
            return list(cached.tokens)
        tokens: list[str] = tokenize_for_retrieval(
            text, stopwords=[], dedupe=dedupe, min_token_length=2
        )
        if key is not None:
            immutable_tokens: tuple[str, ...] = tuple(tokens)
            size_bytes: int = sys.getsizeof(immutable_tokens) + sum(
                sys.getsizeof(item) for item in immutable_tokens
            )
            cls._store(key, _PreparedValue(tokens=immutable_tokens, size_bytes=size_bytes))
        return tokens

    @classmethod
    def count_tokens(cls, text: str) -> Counter[str]:
        key: _Key | None = cls._build_key(text, operation="frequencies", dedupe=False)
        cached: _PreparedValue | None = cls._get(key)
        if cached is not None:
            return Counter(dict(cached.frequencies))
        frequencies: Counter[str] = Counter(text.split())
        if key is not None:
            immutable_frequencies: tuple[tuple[str, int], ...] = tuple(frequencies.items())
            size_bytes: int = sys.getsizeof(immutable_frequencies) + sum(
                sys.getsizeof(item) + sys.getsizeof(item[0]) + sys.getsizeof(item[1])
                for item in immutable_frequencies
            )
            cls._store(
                key,
                _PreparedValue(frequencies=immutable_frequencies, size_bytes=size_bytes),
            )
        return frequencies

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._entries.clear()
            cls._size_bytes = 0

    @staticmethod
    def _build_key(text: str, *, operation: str, dedupe: bool) -> _Key | None:
        scope: _Scope | None = _active_scope.get()
        if scope is None:
            return None
        return scope, operation, dedupe, sha256(text.encode("utf-8")).digest()

    @classmethod
    def _get(cls, key: _Key | None) -> _PreparedValue | None:
        if key is None:
            return None
        with cls._lock:
            value: _PreparedValue | None = cls._entries.get(key)
            if value is not None:
                cls._entries.move_to_end(key)
            return value

    @classmethod
    def _store(cls, key: _Key, value: _PreparedValue) -> None:
        # Include conservative per-entry overhead for keys, scope and LRU nodes.
        size_bytes: int = value.size_bytes + 1024 + sum(
            sys.getsizeof(part) for part in key[0]
        )
        if size_bytes > _MAX_BYTES:
            return
        value = _PreparedValue(
            tokens=value.tokens, frequencies=value.frequencies, size_bytes=size_bytes
        )
        with cls._lock:
            previous: _PreparedValue | None = cls._entries.pop(key, None)
            if previous is not None:
                cls._size_bytes -= previous.size_bytes
            while cls._entries and (
                cls._size_bytes + size_bytes > _MAX_BYTES
                or len(cls._entries) >= _MAX_ENTRIES
            ):
                _, evicted = cls._entries.popitem(last=False)
                cls._size_bytes -= evicted.size_bytes
            cls._entries[key] = value
            cls._size_bytes += size_bytes
