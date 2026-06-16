"""In-process API request buckets for anonymous aggregate telemetry."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ApiRequestMetricsSnapshot:
    """Bounded API request aggregate data since the last snapshot."""

    request_count: int
    status_2xx_count: int
    status_3xx_count: int
    status_4xx_count: int
    status_5xx_count: int
    latency_avg_ms: float
    latency_p95_ms: float


class ApiRequestTelemetryMetrics:
    """Process-local request counters flushed as aggregate telemetry."""

    _MAX_LATENCY_SAMPLES = 1_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_count = 0
        self._status_2xx_count = 0
        self._status_3xx_count = 0
        self._status_4xx_count = 0
        self._status_5xx_count = 0
        self._latency_total_ms = 0.0
        self._latency_samples_ms: list[float] = []

    def record(self, *, status_code: int, latency_ms: float) -> None:
        """Record one completed HTTP request without storing request content."""
        with self._lock:
            self._request_count += 1
            self._latency_total_ms += max(latency_ms, 0.0)
            if len(self._latency_samples_ms) < self._MAX_LATENCY_SAMPLES:
                self._latency_samples_ms.append(max(latency_ms, 0.0))
            if 200 <= status_code < 300:
                self._status_2xx_count += 1
            elif 300 <= status_code < 400:
                self._status_3xx_count += 1
            elif 400 <= status_code < 500:
                self._status_4xx_count += 1
            elif status_code >= 500:
                self._status_5xx_count += 1

    def snapshot_and_reset(self) -> ApiRequestMetricsSnapshot:
        """Return current counters and reset the interval bucket."""
        with self._lock:
            request_count = self._request_count
            samples = sorted(self._latency_samples_ms)
            snapshot = ApiRequestMetricsSnapshot(
                request_count=request_count,
                status_2xx_count=self._status_2xx_count,
                status_3xx_count=self._status_3xx_count,
                status_4xx_count=self._status_4xx_count,
                status_5xx_count=self._status_5xx_count,
                latency_avg_ms=(
                    self._latency_total_ms / request_count
                    if request_count > 0
                    else 0.0
                ),
                latency_p95_ms=_calculate_percentile(samples, 0.95),
            )
            self._request_count = 0
            self._status_2xx_count = 0
            self._status_3xx_count = 0
            self._status_4xx_count = 0
            self._status_5xx_count = 0
            self._latency_total_ms = 0.0
            self._latency_samples_ms = []
            return snapshot


def _calculate_percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * percentile))))
    return values[index]
