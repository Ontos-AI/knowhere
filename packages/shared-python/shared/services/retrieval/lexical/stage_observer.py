"""Opt-in bounded campaign measurements; observation never gates business work.

Configure once after application logging, using a private configuration file.
Durations are measured elapsed time, counters count instrumented calls, and no
query, document content or credential is written. Absent stages remain absent.
"""

from _thread import RLock
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import ClassVar

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

_IDENTITY: str = r"^[A-Za-z0-9_.:/-]{1,128}$"


class _Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str = Field(pattern="^opensearch-stage-observer-v1$")
    campaign_id: str = Field(pattern=_IDENTITY)
    source_hash: str = Field(pattern="^[a-f0-9]{64}$")
    journal_path: str
    maximum_journal_bytes: int = Field(default=512 * 1024 * 1024, ge=4096, le=1024 * 1024 * 1024)


@dataclass
class _Event:
    identity: str
    kind: str
    started: float = field(default_factory=time.perf_counter)
    lock: RLock = field(default_factory=RLock)
    metrics: dict[str, object] = field(default_factory=lambda: {
        "lexical_executions": 0, "path_channel_calls": 0, "content_channel_calls": 0,
        "native_bulk_bytes": [],
    })


_CURRENT: ContextVar[_Event | None] = ContextVar("workload_stage_event", default=None)
_NATIVE_HTTP: ContextVar[bool] = ContextVar("workload_native_http", default=False)


class StageObserver:
    _configuration: ClassVar[_Configuration | None] = None
    _hasWarned: ClassVar[bool] = False

    @classmethod
    def install(cls) -> None:
        path: str | None = os.environ.get("KNOWHERE_WORKLOAD_OBSERVER_CONFIG")
        if not path or cls._configuration is not None:
            return
        try:
            from shared.core.config import settings
            from shared.services.retrieval.lexical.sourcehash import getSourceHash
            metadata: os.stat_result = Path(path).stat()
            if (settings.ENVIRONMENT != "staging" or settings.APP_ENV != "staging"
                or not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536):
                raise ValueError("Campaign observer requires private bounded staging configuration")
            configuration = _Configuration.model_validate_json(Path(path).read_bytes())
            if configuration.source_hash != getSourceHash():
                raise ValueError("Campaign observer source differs from reviewed source")
            cls._configuration = configuration
            from shared.services.retrieval.lexical.stage_pool_observer import installPoolObservers
            installPoolObservers(cls)
            cls._write({"kind": "observer_install", "pid": os.getpid(),
                "counter_semantics": "cumulative completed-stage duration/byte counters since observer install",
                "capabilities": ["pg_pool_wait_ms", "pg_transaction_ms", "native_http_pool_wait_ms",
                    "native_response_bytes", "native_response_processing_ms", "inline_indexing_ms", "manual_retry_ms", "bulk_ms", "visibility_ms"]})
        except Exception as error:
            cls._configuration = None
            cls._warn(error)

    @classmethod
    def _warn(cls, error: Exception) -> None:
        if not cls._hasWarned:
            cls._hasWarned = True
            logger.warning("Campaign stage observation unavailable: error_type={}", type(error).__name__)

    @classmethod
    def _write(cls, value: dict[str, object]) -> None:
        configuration = cls._configuration
        if configuration is None:
            return
        descriptor: int | None = None
        try:
            data: bytes = (json.dumps({"mode": "actual", "campaign_id": configuration.campaign_id,
                "source_hash": configuration.source_hash, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                **value}, separators=(",", ":"), allow_nan=False) + "\n").encode()
            if len(data) > 8192:
                raise ValueError("Campaign measurement record exceeds its fixed bound")
            descriptor = os.open(configuration.journal_path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata: os.stat_result = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_size + len(data) > configuration.maximum_journal_bytes):
                raise ValueError("Campaign journal is not private or exceeds its fixed bound")
            if os.write(descriptor, data) != len(data):
                raise OSError("Campaign journal record was incomplete")
        except Exception as error:
            cls._warn(error)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    @contextmanager
    def bindEvent(cls, campaignId: str, eventId: str, *, kind: str) -> Iterator[None]:
        configuration = cls._configuration
        if (configuration is None or campaignId != configuration.campaign_id
            or re.fullmatch(_IDENTITY, eventId) is None or kind not in ("read", "write", "resource", "admission")
            or _CURRENT.get() is not None):
            yield
            return
        event: _Event = _Event(eventId, kind)
        token = _CURRENT.set(event)
        hasFailed: bool = False
        try:
            yield
        except BaseException:
            hasFailed = True
            raise
        finally:
            _CURRENT.reset(token)
            cls._write({"kind": kind, "id": eventId, "status": "failed" if hasFailed else "observed",
                "duration_ms": (time.perf_counter() - event.started) * 1000, **event.metrics})

    @staticmethod
    def add(name: str, value: float | int = 1) -> None:
        event = _CURRENT.get()
        if event is not None:
            with event.lock:
                previous: object = event.metrics.get(name, 0)
                if isinstance(previous, (int, float)):
                    event.metrics[name] = previous + value

    @staticmethod
    def record(name: str, value: str | int | float | list[int]) -> None:
        event = _CURRENT.get()
        if event is not None:
            with event.lock:
                event.metrics[name] = value

    @staticmethod
    def recordBulk(size: int) -> None:
        event = _CURRENT.get()
        if event is not None:
            with event.lock:
                sizes = event.metrics.get("native_bulk_bytes")
                if isinstance(sizes, list) and len(sizes) < 256:
                    sizes.append(size)
                elif isinstance(sizes, list):
                    event.metrics["native_bulk_observation_overflow"] = 1

    @staticmethod
    def hasEvent() -> bool:
        return _CURRENT.get() is not None

    @staticmethod
    def recordInterval(name: str, started: str, finished: str) -> None:
        event = _CURRENT.get()
        if event is not None:
            with event.lock:
                intervals: object = event.metrics.setdefault(name, [])
                if isinstance(intervals, list) and len(intervals) < 16:
                    intervals.append({"started_at_utc": started, "finished_at_utc": finished})

    @staticmethod
    def isNativeRequest() -> bool:
        return _NATIVE_HTTP.get()

    @staticmethod
    @contextmanager
    def bindNativeRequest() -> Iterator[None]:
        if _CURRENT.get() is None:
            yield
            return
        token = _NATIVE_HTTP.set(True)
        try:
            yield
        finally:
            _NATIVE_HTTP.reset(token)

    @staticmethod
    def startNativeRequest(path: str, textBody: str | None) -> float | None:
        if _CURRENT.get() is None:
            return None
        if "/_bulk" in path and textBody is not None:
            StageObserver.recordBulk(len(textBody.encode()))
        return time.perf_counter()

    @staticmethod
    def finishNativeResponse(response: httpx.Response, path: str, started: float | None) -> None:
        if started is None:
            return
        StageObserver.add("native_response_bytes", len(response.content))
        metric: str = "bulk_ms" if "/_bulk" in path else "visibility_ms" if "/_count" in path else "native_http_ms"
        StageObserver.add(metric, (time.perf_counter() - started) * 1000)

    @staticmethod
    @contextmanager
    def measure(name: str, *, intervalName: str | None = None) -> Iterator[None]:
        if _CURRENT.get() is None:
            yield
            return
        started: float = time.perf_counter()
        startedUtc: str | None = datetime.now(timezone.utc).isoformat() if intervalName else None
        try:
            yield
        finally:
            StageObserver.add(name, (time.perf_counter() - started) * 1000)
            if intervalName is not None and startedUtc is not None:
                StageObserver.recordInterval(intervalName, startedUtc, datetime.now(timezone.utc).isoformat())

    @classmethod
    @contextmanager
    def bindRequest(cls, requestId: str, *, isRetry: bool = False) -> Iterator[None]:
        configuration = cls._configuration
        prefix: str = configuration.campaign_id + ":" if configuration is not None else ""
        if configuration is None or not prefix or not requestId.startswith(prefix):
            yield
            return
        with cls.bindEvent(configuration.campaign_id, requestId[len(prefix):], kind="write" if isRetry else "read"):
            if isRetry:
                with cls.measure("manual_retry_ms"):
                    yield
            else:
                yield
