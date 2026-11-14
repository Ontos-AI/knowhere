"""Record shared OpenSearch node metrics for later analysis; it never alerts or changes the node.

A systemd timer runs this on the host every 30 minutes with the system Python 3.9
and only the standard library, so the node needs no extra package. Each run makes
three filtered GET requests with short timeouts, reads only the server log lines
written since the previous run, and writes one small gzip JSON record. The record
is uploaded to S3 with the host's AWS CLI; a failed upload stays in the outbox and
is retried on the next run. Local records are pruned by age and total size.

Counters are cumulative and recorded raw: subtract consecutive records, and treat
a smaller jvmUptimeMillis as a node restart. Namespace indexes are grouped by
their prefix (the name before the 48-character scope fingerprint), so a new prefix
needs no edit here. Opens and closes are counted from the cluster manager's own
log lines because concurrent opens queue there one at a time;
maxOpensIn10Seconds is the largest such burst in the window.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from typing import Callable, Final, Mapping, Protocol
import urllib.request

SCHEMA_VERSION: Final[str] = "knowhere-opensearch-metrics-v1"
ENDPOINT: Final[str] = "https://localhost:9200"
LOG_DIRECTORY: Final[Path] = Path("/var/log/knowhere-opensearch")
STATE_DIRECTORY: Final[Path] = Path("/var/lib/knowhere-metrics")
DEFAULT_KEY_PREFIX: Final[str] = "opensearch/shared/metrics"
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
UPLOAD_TIMEOUT_SECONDS: Final[float] = 30.0
MAXIMUM_UPLOADS_PER_RUN: Final[int] = 96
RETENTION: Final[timedelta] = timedelta(days=30)
MAXIMUM_LOCAL_BYTES: Final[int] = 200 * 1024 * 1024
MAXIMUM_LOG_BYTES_PER_RUN: Final[int] = 64 * 1024 * 1024
BURST_SECONDS: Final[float] = 10.0
NAME_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"
LIFECYCLE_COMPONENT: Final[str] = "o.o.c.m.MetadataIndexStateService"
NAMESPACE_INDEX: Final[re.Pattern[str]] = re.compile(r"^(?P<prefix>.+)-[0-9a-f]{48}$")
OPENED_INDEX: Final[re.Pattern[str]] = re.compile(r"\[([^\[\]/,\s]+)/[^\[\]]*\]")
BUCKET_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
KEY_PREFIX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
NODE_PATH: Final[str] = "/_nodes/_local/stats/jvm,breaker,os,fs,thread_pool,indices/search,indexing?filter_path=" + ",".join((
    "cluster_name", "nodes.*.jvm.uptime_in_millis", "nodes.*.jvm.mem.heap_used_in_bytes",
    "nodes.*.jvm.mem.heap_max_in_bytes", "nodes.*.jvm.mem.pools.old.used_in_bytes", "nodes.*.jvm.gc.collectors",
    "nodes.*.breakers.parent", "nodes.*.os.cpu.percent", "nodes.*.fs.total", "nodes.*.thread_pool.search",
    "nodes.*.thread_pool.write.rejected", "nodes.*.indices.search.query_total",
    "nodes.*.indices.search.query_time_in_millis", "nodes.*.indices.indexing.index_total",
    "nodes.*.indices.indexing.index_time_in_millis",
))
HEALTH_PATH: Final[str] = "/_cluster/health?filter_path=status,active_shards,number_of_pending_tasks"
INDEXES_PATH: Final[str] = "/_cat/indices?h=index,status,search.query_total&format=json&expand_wildcards=all"


class _Reader(Protocol):
    def get(self, path: str) -> object: ...


@dataclass(frozen=True)
class _Destination:
    bucket: str
    keyPrefix: str
    region: str


@dataclass(frozen=True)
class _LifecycleEvent:
    occurredAt: datetime
    action: str
    indexName: str


@dataclass(frozen=True)
class _LogPosition:
    inode: int
    offset: int
    readAt: datetime

    def toState(self) -> dict[str, object]:
        return {"inode": self.inode, "offset": self.offset, "readAt": _formatTime(self.readAt)}

    @staticmethod
    def fromState(value: object) -> _LogPosition | None:
        if not isinstance(value, dict):
            return None
        try:
            return _LogPosition(int(value["inode"]), int(value["offset"]), _parseTime(str(value["readAt"])))
        except (KeyError, TypeError, ValueError):
            return None


class _OpenSearch:
    """GET-only client; the certificate it reads with never leaves this host."""

    def __init__(self, endpoint: str, caFile: str, certificateFile: str, keyFile: str) -> None:
        self.endpoint: str = endpoint
        self.caFile: str = caFile
        self.certificateFile: str = certificateFile
        self.keyFile: str = keyFile
        self.context: ssl.SSLContext | None = None

    @staticmethod
    def fromEnvironment(environment: Mapping[str, str]) -> _OpenSearch:
        names: tuple[str, ...] = ("KNOWHERE_OPENSEARCH_CA_FILE", "KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE",
            "KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE")
        missing: list[str] = [name for name in names if not environment.get(name, "").startswith("/")]
        if missing:
            raise ValueError(", ".join(missing) + " must be absolute file paths")
        return _OpenSearch(ENDPOINT, *(environment[name] for name in names))

    def get(self, path: str) -> object:
        if self.context is None:
            self.context = ssl.create_default_context(cafile=self.caFile)
            self.context.load_cert_chain(self.certificateFile, self.keyFile)
        request: urllib.request.Request = urllib.request.Request(self.endpoint + path, method="GET")
        with urllib.request.urlopen(request, context=self.context, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.load(response)


class NodeMetricsRecorder:
    """Write one record per run. Only run() reads the node, the clock or the environment."""

    @staticmethod
    def readDestination(environment: Mapping[str, str]) -> _Destination:
        bucket: str = environment.get("KNOWHERE_METRICS_BUCKET", "")
        region: str = environment.get("KNOWHERE_METRICS_REGION", "")
        keyPrefix: str = environment.get("KNOWHERE_METRICS_PREFIX", DEFAULT_KEY_PREFIX)
        if not BUCKET_NAME.match(bucket):
            raise ValueError("KNOWHERE_METRICS_BUCKET must be an S3 bucket name")
        if not REGION_NAME.match(region):
            raise ValueError("KNOWHERE_METRICS_REGION must be an AWS region name")
        if not KEY_PREFIX.match(keyPrefix):
            raise ValueError("KNOWHERE_METRICS_PREFIX must be a key prefix without leading or trailing slashes")
        return _Destination(bucket=bucket, keyPrefix=keyPrefix, region=region)

    @staticmethod
    def summarizeIndexes(rows: object, previousTotals: dict[str, int] | None
                         ) -> tuple[dict[str, dict[str, int | None]], dict[str, int], dict[str, int]]:
        """Count indexes per namespace prefix and the namespaces searched since the last run.

        A namespace is active when its open index's search count changed and is not
        zero; a reopened index restarts its count, which also counts as a change. The
        first run has nothing to compare, so its active counts are None.
        """
        if not isinstance(rows, list):
            raise ValueError("Index listing must be a JSON list")
        groups: dict[str, dict[str, int | None]] = {}
        other: dict[str, int] = {"openIndexes": 0, "closedIndexes": 0}
        totals: dict[str, int] = {}
        for row in rows:
            name: str = str(row["index"])
            isOpen: bool = row["status"] == "open"
            match: re.Match[str] | None = NAMESPACE_INDEX.match(name)
            if match is None:
                other["openIndexes" if isOpen else "closedIndexes"] += 1
                continue
            group: dict[str, int | None] = groups.setdefault(match.group("prefix"), _emptyGroup(previousTotals is not None))
            if not isOpen:
                group["closedIndexes"] = int(group["closedIndexes"] or 0) + 1
                continue
            group["openIndexes"] = int(group["openIndexes"] or 0) + 1
            total: int = int(row.get("search.query_total") or 0)
            totals[name] = total
            if previousTotals is not None and total > 0 and total != previousTotals.get(name):
                group["activeNamespaces"] = int(group["activeNamespaces"] or 0) + 1
        return groups, other, totals

    @staticmethod
    def parseLifecycleLine(line: str) -> list[_LifecycleEvent]:
        """Read one JSON server log line; only completed opens and closes count."""
        if LIFECYCLE_COMPONENT not in line:
            return []
        try:
            entry: object = json.loads(line)
        except ValueError:
            return []
        if not isinstance(entry, dict) or entry.get("component") != LIFECYCLE_COMPONENT:
            return []
        message: object = entry.get("message")
        timestamp: object = entry.get("timestamp")
        if not isinstance(message, str) or not isinstance(timestamp, str):
            return []
        try:
            occurredAt: datetime = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S,%f%z")
        except ValueError:
            return []
        if message.startswith("opening indices "):
            return [_LifecycleEvent(occurredAt, "open", name) for name in OPENED_INDEX.findall(message)]
        if message.startswith("completed closing of indices "):
            names: str = message[message.find("[") + 1:message.rfind("]")]
            return [_LifecycleEvent(occurredAt, "close", name.strip()) for name in names.split(",") if name.strip()]
        return []

    @staticmethod
    def readLifecycle(directory: Path, clusterName: str, position: _LogPosition | None, now: datetime
                      ) -> tuple[list[_LifecycleEvent], _LogPosition, bool]:
        """Return events since the last run, the next position and whether nothing was skipped."""
        current: Path = directory / (clusterName + "_server.json")
        inode: int = current.stat().st_ino
        if position is None:
            return [], _LogPosition(inode, current.stat().st_size, now), False
        if inode == position.inode and current.stat().st_size >= position.offset:
            events, offset, isComplete = _readNewLines(current, position.offset)
            return events, _LogPosition(inode, offset, now), isComplete
        # The log rotated into gzip copies, so byte offsets no longer apply: read each
        # copy written since the last run by timestamp, then the new file from its start.
        events = []
        isComplete = True
        rotated: list[Path] = sorted((path for path in directory.glob(clusterName + "-*.json.gz")
            if path.stat().st_mtime >= position.readAt.timestamp()), key=lambda path: path.stat().st_mtime)
        for path in rotated:
            try:
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
                    events.extend(event for line in stream for event in NodeMetricsRecorder.parseLifecycleLine(line)
                        if event.occurredAt > position.readAt)
            except (OSError, EOFError):
                isComplete = False
        newEvents, offset, isNewComplete = _readNewLines(current, 0)
        return events + newEvents, _LogPosition(inode, offset, now), bool(rotated) and isComplete and isNewComplete

    @staticmethod
    def countLifecycle(events: list[_LifecycleEvent]) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
        """Count opens and closes per namespace prefix and for the whole node, which shares one queue."""
        groups: dict[str, dict[str, int]] = {}
        opensByPrefix: dict[str, list[datetime]] = {}
        allOpens: list[datetime] = []
        totals: dict[str, int] = {"opens": 0, "closes": 0}
        for event in events:
            key: str = "opens" if event.action == "open" else "closes"
            totals[key] += 1
            if event.action == "open":
                allOpens.append(event.occurredAt)
            match: re.Match[str] | None = NAMESPACE_INDEX.match(event.indexName)
            if match is None:
                continue
            prefix: str = match.group("prefix")
            groups.setdefault(prefix, {"opens": 0, "closes": 0, "maxOpensIn10Seconds": 0})[key] += 1
            if event.action == "open":
                opensByPrefix.setdefault(prefix, []).append(event.occurredAt)
        for prefix, moments in opensByPrefix.items():
            groups[prefix]["maxOpensIn10Seconds"] = _largestBurst(moments)
        totals["maxOpensIn10Seconds"] = _largestBurst(allOpens)
        return groups, totals

    @staticmethod
    def uploadOutbox(directory: Path, destination: _Destination,
                     runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> tuple[int, str | None]:
        """Upload pending records oldest first and stop at the first failure, which the next run retries."""
        records: Path = directory / "records"
        uploaded: int = 0
        for path in sorted((directory / "outbox").glob("*.json.gz"))[:MAXIMUM_UPLOADS_PER_RUN]:
            key: str = "/".join((destination.keyPrefix, path.name[0:4], path.name[4:6], path.name[6:8], path.name))
            try:
                completed: subprocess.CompletedProcess[str] = runner(["aws", "s3api", "put-object",
                    "--bucket", destination.bucket, "--key", key, "--body", str(path),
                    "--content-type", "application/gzip", "--region", destination.region,
                    "--output", "json", "--no-cli-pager"],
                    capture_output=True, text=True, timeout=UPLOAD_TIMEOUT_SECONDS, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                return uploaded, type(error).__name__
            if completed.returncode != 0:
                return uploaded, (completed.stderr or "").strip()[-300:] or "exit " + str(completed.returncode)
            os.replace(path, records / path.name)
            uploaded += 1
        return uploaded, None

    @staticmethod
    def pruneRecords(directory: Path, now: datetime) -> int:
        """Delete records older than the retention, then the oldest ones while over the size cap."""
        files: list[Path] = sorted((path for folder in ("records", "outbox")
            for path in (directory / folder).glob("*.json.gz")), key=lambda path: path.name)
        total: int = sum(path.stat().st_size for path in files)
        cutoff: str = (now - RETENTION).strftime(NAME_FORMAT)
        removed: int = 0
        for path in files:
            if path.name[:len(cutoff)] >= cutoff and total <= MAXIMUM_LOCAL_BYTES:
                break
            total -= path.stat().st_size
            path.unlink()
            removed += 1
        return removed

    @staticmethod
    def observe(opensearch: _Reader, logDirectory: Path, state: dict[str, object] | None, now: datetime
                ) -> tuple[dict[str, object], dict[str, object]]:
        """Return the record body and the state the next run compares against."""
        clusterName, gauges, counters = _readNode(opensearch.get(NODE_PATH))
        health: object = opensearch.get(HEALTH_PATH)
        if not isinstance(health, dict):
            raise ValueError("Cluster health must be a JSON object")
        previousTotals: object = state.get("queryTotals") if state is not None else None
        groups, other, totals = NodeMetricsRecorder.summarizeIndexes(opensearch.get(INDEXES_PATH),
            {str(name): int(total) for name, total in previousTotals.items()} if isinstance(previousTotals, dict) else None)
        position: _LogPosition | None = _LogPosition.fromState(state.get("log")) if state is not None else None
        events, nextPosition, isComplete = NodeMetricsRecorder.readLifecycle(logDirectory, clusterName, position, now)
        lifecycleGroups, lifecycleTotals = NodeMetricsRecorder.countLifecycle(events)
        for prefix in set(groups) | set(lifecycleGroups):
            group: dict[str, int | None] = groups.setdefault(prefix, _emptyGroup(isinstance(previousTotals, dict)))
            group.update(lifecycleGroups.get(prefix, {"opens": 0, "closes": 0, "maxOpensIn10Seconds": 0}))
        body: dict[str, object] = {
            "clusterName": clusterName, "node": gauges, "counters": counters,
            "cluster": {"status": health["status"], "activeShards": health["active_shards"],
                "pendingTasks": health["number_of_pending_tasks"]},
            "namespaceIndexes": groups, "otherIndexes": other,
            "lifecycle": {**lifecycleTotals, "isComplete": isComplete},
        }
        return body, {"recordedAt": _formatTime(now), "log": nextPosition.toState(), "queryTotals": totals}

    @staticmethod
    def run(directory: Path, destination: _Destination | None, opensearch: _Reader, logDirectory: Path,
            now: datetime) -> int:
        for folder in ("outbox", "records"):
            (directory / folder).mkdir(parents=True, exist_ok=True)
        statePath: Path = directory / "state.json"
        state: dict[str, object] | None = _loadState(statePath)
        record: dict[str, object] = {"schemaVersion": SCHEMA_VERSION, "recordedAt": _formatTime(now),
            "previousRecordedAt": state.get("recordedAt") if state is not None else None}
        nextState: dict[str, object] | None = None
        try:
            body, nextState = NodeMetricsRecorder.observe(opensearch, logDirectory, state, now)
            record.update(body)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # An unreadable node is itself a data point; keep the old state so the next
            # run compares against the last good one.
            record["error"] = type(error).__name__ + ": " + str(error)[:300]
        name: str = now.strftime(NAME_FORMAT) + ".json.gz"
        _writeAtomically(directory / "outbox" / name, gzip.compress(json.dumps(record, sort_keys=True).encode() + b"\n"))
        if nextState is not None:
            _writeAtomically(statePath, json.dumps(nextState, sort_keys=True).encode())
        uploaded, uploadError = (0, "upload disabled") if destination is None else NodeMetricsRecorder.uploadOutbox(directory, destination)
        removed: int = NodeMetricsRecorder.pruneRecords(directory, now)
        print(json.dumps({"event": "recorded", "record": name, "uploaded": uploaded, "uploadError": uploadError,
            "pruned": removed, "error": record.get("error")}))
        return 1 if "error" in record else 0


def _emptyGroup(hasPrevious: bool) -> dict[str, int | None]:
    return {"openIndexes": 0, "closedIndexes": 0, "activeNamespaces": 0 if hasPrevious else None}


def _readNode(body: object) -> tuple[str, dict[str, object], dict[str, object]]:
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), dict) or len(body["nodes"]) != 1:
        raise ValueError("Node stats must describe exactly the local node")
    node: dict[str, dict[str, dict[str, object]]] = next(iter(body["nodes"].values()))
    jvm: dict = node["jvm"]
    parent: dict = node["breakers"]["parent"]
    threadPool: dict = node["thread_pool"]
    indices: dict = node["indices"]
    gauges: dict[str, object] = {
        "jvmUptimeMillis": jvm["uptime_in_millis"], "heapMaxBytes": jvm["mem"]["heap_max_in_bytes"],
        "heapUsedBytes": jvm["mem"]["heap_used_in_bytes"], "oldUsedBytes": jvm["mem"]["pools"]["old"]["used_in_bytes"],
        "breakerEstimatedBytes": parent["estimated_size_in_bytes"], "breakerLimitBytes": parent["limit_size_in_bytes"],
        "cpuPercent": node["os"]["cpu"]["percent"], "searchQueue": threadPool["search"]["queue"],
        "dataDiskTotalBytes": node["fs"]["total"]["total_in_bytes"],
        "dataDiskAvailableBytes": node["fs"]["total"]["available_in_bytes"],
    }
    counters: dict[str, object] = {
        "breakerTripped": parent["tripped"], "searchQueryTotal": indices["search"]["query_total"],
        "searchQueryMillis": indices["search"]["query_time_in_millis"],
        "indexingTotal": indices["indexing"]["index_total"], "indexingMillis": indices["indexing"]["index_time_in_millis"],
        "searchRejected": threadPool["search"]["rejected"], "writeRejected": threadPool["write"]["rejected"],
        "gc": {name: {"count": collector["collection_count"], "millis": collector["collection_time_in_millis"]}
            for name, collector in jvm["gc"]["collectors"].items()},
    }
    return str(body["cluster_name"]), gauges, counters


def _readNewLines(path: Path, offset: int) -> tuple[list[_LifecycleEvent], int, bool]:
    """Read complete lines from offset; a line still being written waits for the next run."""
    events: list[_LifecycleEvent] = []
    with path.open("rb") as stream:
        size: int = os.fstat(stream.fileno()).st_size
        isComplete: bool = size - offset <= MAXIMUM_LOG_BYTES_PER_RUN
        consumed: int = offset if isComplete else size - MAXIMUM_LOG_BYTES_PER_RUN
        stream.seek(consumed)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            consumed += len(line)
            events.extend(NodeMetricsRecorder.parseLifecycleLine(line.decode("utf-8", "replace")))
    return events, consumed, isComplete


def _largestBurst(moments: list[datetime]) -> int:
    ordered: list[datetime] = sorted(moments)
    largest: int = 0
    start: int = 0
    for end, moment in enumerate(ordered):
        while (moment - ordered[start]).total_seconds() >= BURST_SECONDS:
            start += 1
        largest = max(largest, end - start + 1)
    return largest


def _loadState(path: Path) -> dict[str, object] | None:
    try:
        value: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _writeAtomically(path: Path, data: bytes) -> None:
    temporary: Path = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _formatTime(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parseTime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _main(argv: list[str]) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Record shared OpenSearch node metrics once.")
    parser.add_argument("--directory", type=Path, default=STATE_DIRECTORY, help="Local records and state")
    parser.add_argument("--no-upload", action="store_true", help="Keep records in the outbox, for a manual check")
    arguments: argparse.Namespace = parser.parse_args(argv)
    destination: _Destination | None = None if arguments.no_upload else NodeMetricsRecorder.readDestination(os.environ)
    return NodeMetricsRecorder.run(directory=arguments.directory, destination=destination,
        opensearch=_OpenSearch.fromEnvironment(os.environ), logDirectory=LOG_DIRECTORY,
        now=datetime.now(timezone.utc).replace(microsecond=0))


if __name__ == "__main__":
    try:
        raise SystemExit(_main(sys.argv[1:]))
    except ValueError as error:
        print("OpenSearch metrics recorder configuration failed: " + str(error), file=sys.stderr)
        raise SystemExit(2) from None
