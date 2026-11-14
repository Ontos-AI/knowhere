"""The host metrics recorder reads only the node and its log, and keeps every run's record."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

PROD_PREFIX: str = "knowhere-prod-lexical-v1"
STAGING_PREFIX: str = "knowhere-staging-lexical-v1"
CLUSTER: str = "knowhere-opensearch-shared"
NOW: datetime = datetime(2026, 11, 15, 8, 5, tzinfo=timezone.utc)


RECORDER_PATH: Path = Path(__file__).parents[1] / "metrics_recorder.py"


def loadRecorder() -> ModuleType:
    specification = importlib.util.spec_from_file_location("metrics_recorder", RECORDER_PATH)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


recorder: ModuleType = loadRecorder()
Recorder = recorder.NodeMetricsRecorder


def namespaceIndex(prefix: str, namespace: str) -> str:
    # Matches OpenSearchLexicalIndex: prefix plus the first 48 hex characters of the scope fingerprint.
    return prefix + "-" + hashlib.sha256(namespace.encode()).hexdigest()[:48]


def logLine(moment: datetime, message: str, component: str = "o.o.c.m.MetadataIndexStateService") -> str:
    stamp: str = moment.strftime("%Y-%m-%dT%H:%M:%S,") + "%03d" % (moment.microsecond // 1000) + "+0000"
    return json.dumps({"type": "server", "timestamp": stamp, "level": "INFO", "component": component,
        "cluster.name": CLUSTER, "node.name": "native-1", "message": message}) + "\n"


def openLine(moment: datetime, *names: str) -> str:
    return logLine(moment, "opening indices [" + ",".join("[%s/uuid%s]" % (name, index) for index, name in enumerate(names)) + "]")


def closedLine(moment: datetime, *names: str) -> str:
    return logLine(moment, "completed closing of indices [" + ", ".join(names) + "]")


class FakeOpenSearch:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows: list[dict[str, str]] = rows
        self.paths: list[str] = []

    def get(self, path: str) -> object:
        self.paths.append(path)
        if path.startswith("/_nodes/"):
            return {"cluster_name": CLUSTER, "nodes": {"node-id": {
                "jvm": {"uptime_in_millis": 1000, "mem": {"heap_used_in_bytes": 5, "heap_max_in_bytes": 16,
                    "pools": {"old": {"used_in_bytes": 2}}},
                    "gc": {"collectors": {"young": {"collection_count": 7, "collection_time_in_millis": 70},
                        "old": {"collection_count": 0, "collection_time_in_millis": 0}}}},
                "breakers": {"parent": {"estimated_size_in_bytes": 4, "limit_size_in_bytes": 15, "tripped": 0}},
                "os": {"cpu": {"percent": 3}},
                "fs": {"total": {"total_in_bytes": 100, "available_in_bytes": 99}},
                "thread_pool": {"search": {"queue": 0, "rejected": 0}, "write": {"rejected": 0}},
                "indices": {"search": {"query_total": 10, "query_time_in_millis": 20},
                    "indexing": {"index_total": 30, "index_time_in_millis": 40}}}}}
        if path.startswith("/_cluster/health"):
            return {"status": "green", "active_shards": 186, "number_of_pending_tasks": 0}
        if path.startswith("/_cat/indices"):
            return self.rows
        raise AssertionError("unexpected request " + path)


def test_every_node_request_is_a_filtered_read() -> None:
    for path in (recorder.NODE_PATH, recorder.HEALTH_PATH, recorder.INDEXES_PATH):
        assert "filter_path=" in path or path.startswith("/_cat/indices?h=")
    source: str = RECORDER_PATH.read_text()
    assert source.count("method=") == 1 and 'method="GET"' in source


def test_indexes_are_grouped_by_prefix_and_active_namespaces_need_a_changed_search_count() -> None:
    searched, idle, reopened, unqueried = (namespaceIndex(PROD_PREFIX, name) for name in ("a", "b", "c", "d"))
    closed: str = namespaceIndex(STAGING_PREFIX, "e")
    rows: list[dict[str, str]] = [
        {"index": searched, "status": "open", "search.query_total": "12"},
        {"index": idle, "status": "open", "search.query_total": "5"},
        {"index": reopened, "status": "open", "search.query_total": "2"},
        {"index": unqueried, "status": "open", "search.query_total": "0"},
        {"index": closed, "status": "close", "search.query_total": ""},
        {"index": PROD_PREFIX + "-deployment-probe", "status": "open", "search.query_total": "9"},
        {"index": ".opendistro_security", "status": "open", "search.query_total": "1"},
    ]
    groups, other, totals = Recorder.summarizeIndexes(rows, {searched: 10, idle: 5, reopened: 8})
    assert groups[PROD_PREFIX] == {"openIndexes": 4, "closedIndexes": 0, "activeNamespaces": 2}
    assert groups[STAGING_PREFIX] == {"openIndexes": 0, "closedIndexes": 1, "activeNamespaces": 0}
    assert other == {"openIndexes": 2, "closedIndexes": 0}
    assert totals == {searched: 12, idle: 5, reopened: 2, unqueried: 0}


def test_first_run_has_no_active_count_to_compare() -> None:
    groups, _, _ = Recorder.summarizeIndexes(
        [{"index": namespaceIndex(PROD_PREFIX, "a"), "status": "open", "search.query_total": "3"}], None)
    assert groups[PROD_PREFIX]["activeNamespaces"] is None


def test_log_lines_count_only_completed_opens_and_closes() -> None:
    first, second = namespaceIndex(PROD_PREFIX, "a"), namespaceIndex(PROD_PREFIX, "b")
    assert [(event.action, event.indexName) for event in Recorder.parseLifecycleLine(openLine(NOW, first, second))] == [
        ("open", first), ("open", second)]
    assert [(event.action, event.indexName) for event in Recorder.parseLifecycleLine(closedLine(NOW, first, second))] == [
        ("close", first), ("close", second)]
    assert Recorder.parseLifecycleLine(logLine(NOW, "closing indices [%s/uuid]" % first)) == []
    assert Recorder.parseLifecycleLine(logLine(NOW, "opening indices [[x/u]]", component="o.o.c.s.Other")) == []
    assert Recorder.parseLifecycleLine("not json MetadataIndexStateService") == []


def test_bursts_and_counts_are_kept_per_prefix_and_for_the_shared_queue() -> None:
    prod, staging = namespaceIndex(PROD_PREFIX, "a"), namespaceIndex(STAGING_PREFIX, "b")
    events = [event for line in (
        openLine(NOW, prod), openLine(NOW + timedelta(seconds=2), staging), openLine(NOW + timedelta(seconds=4), prod),
        openLine(NOW + timedelta(seconds=30), prod), openLine(NOW + timedelta(seconds=31), "knowhere-capacity-probe-0001"),
        closedLine(NOW + timedelta(seconds=40), prod, staging),
    ) for event in Recorder.parseLifecycleLine(line)]
    groups, totals = Recorder.countLifecycle(events)
    assert groups[PROD_PREFIX] == {"opens": 3, "closes": 1, "maxOpensIn10Seconds": 2}
    assert groups[STAGING_PREFIX] == {"opens": 1, "closes": 1, "maxOpensIn10Seconds": 1}
    assert totals == {"opens": 5, "closes": 2, "maxOpensIn10Seconds": 3}


def test_log_reading_resumes_after_the_last_complete_line(tmp_path: Path) -> None:
    log: Path = tmp_path / (CLUSTER + "_server.json")
    index: str = namespaceIndex(PROD_PREFIX, "a")
    log.write_text(openLine(NOW, index))
    events, position, isComplete = Recorder.readLifecycle(tmp_path, CLUSTER, None, NOW)
    assert (events, isComplete) == ([], False)
    with log.open("a") as stream:
        stream.write(openLine(NOW + timedelta(seconds=1), index) + openLine(NOW + timedelta(seconds=2), index)[:40])
    events, position, isComplete = Recorder.readLifecycle(tmp_path, CLUSTER, position, NOW + timedelta(minutes=30))
    assert (len(events), isComplete) == (1, True)
    with log.open("a") as stream:
        stream.write(openLine(NOW + timedelta(seconds=2), index)[40:])
    events, _, _ = Recorder.readLifecycle(tmp_path, CLUSTER, position, NOW + timedelta(minutes=60))
    assert len(events) == 1


def test_rotated_log_is_read_by_timestamp_before_the_new_file(tmp_path: Path) -> None:
    index: str = namespaceIndex(PROD_PREFIX, "a")
    log: Path = tmp_path / (CLUSTER + "_server.json")
    log.write_text(openLine(NOW, index))
    _, position, _ = Recorder.readLifecycle(tmp_path, CLUSTER, None, NOW)
    rotated: Path = tmp_path / (CLUSTER + "-2026-11-15-1.json.gz")
    with gzip.open(rotated, "wt") as stream:
        stream.write(openLine(NOW - timedelta(seconds=1), index) + openLine(NOW + timedelta(seconds=5), index))
    rotatedAt: float = (NOW + timedelta(seconds=10)).timestamp()
    os.utime(rotated, (rotatedAt, rotatedAt))
    # Create the new file before the old one goes away so it cannot reuse its inode.
    fresh: Path = tmp_path / "fresh"
    fresh.write_text(closedLine(NOW + timedelta(seconds=9), index))
    os.replace(fresh, log)
    events, nextPosition, isComplete = Recorder.readLifecycle(tmp_path, CLUSTER, position, NOW + timedelta(minutes=30))
    assert [event.action for event in events] == ["open", "close"]
    assert isComplete is True
    assert nextPosition.inode == log.stat().st_ino


def test_missing_rotated_copy_marks_the_window_incomplete(tmp_path: Path) -> None:
    log: Path = tmp_path / (CLUSTER + "_server.json")
    log.write_text("")
    position = recorder._LogPosition(inode=log.stat().st_ino + 1, offset=0, readAt=NOW)
    _, _, isComplete = Recorder.readLifecycle(tmp_path, CLUSTER, position, NOW + timedelta(minutes=30))
    assert isComplete is False


def test_failed_upload_keeps_the_record_for_the_next_run(tmp_path: Path) -> None:
    destination = Recorder.readDestination({"KNOWHERE_METRICS_BUCKET": "example-metrics", "KNOWHERE_METRICS_REGION": "us-east-1"})
    outbox: Path = tmp_path / "outbox"
    outbox.mkdir()
    (tmp_path / "records").mkdir()
    for name in ("20261115T073500Z.json.gz", "20261115T080500Z.json.gz"):
        (outbox / name).write_bytes(b"record")
    calls: list[list[str]] = []

    def failSecond(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0 if len(calls) == 1 else 255, "", "AccessDenied")

    uploaded, error = Recorder.uploadOutbox(tmp_path, destination, runner=failSecond)
    assert (uploaded, error) == (1, "AccessDenied")
    assert calls[0][calls[0].index("--key") + 1] == "opensearch/shared/metrics/2026/11/15/20261115T073500Z.json.gz"
    assert calls[0][:3] == ["aws", "s3api", "put-object"]
    assert sorted(path.name for path in outbox.iterdir()) == ["20261115T080500Z.json.gz"]
    assert [path.name for path in (tmp_path / "records").iterdir()] == ["20261115T073500Z.json.gz"]


@pytest.mark.parametrize("environment", [
    {"KNOWHERE_METRICS_REGION": "us-east-1"},
    {"KNOWHERE_METRICS_BUCKET": "example-metrics"},
    {"KNOWHERE_METRICS_BUCKET": "example-metrics", "KNOWHERE_METRICS_REGION": "us-east-1", "KNOWHERE_METRICS_PREFIX": "/metrics/"},
])
def test_destination_requires_a_bucket_region_and_clean_prefix(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Recorder.readDestination(environment)


def test_node_client_requires_its_certificate_paths() -> None:
    with pytest.raises(ValueError, match="KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE"):
        recorder._OpenSearch.fromEnvironment({"KNOWHERE_OPENSEARCH_CA_FILE": "/etc/ca.pem",
            "KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE": "/etc/client.pem"})


def test_pruning_removes_old_records_and_then_the_oldest_over_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for folder in ("records", "outbox"):
        (tmp_path / folder).mkdir()
    old: str = (NOW - timedelta(days=31)).strftime(recorder.NAME_FORMAT) + ".json.gz"
    names: list[str] = [(NOW - timedelta(hours=hours)).strftime(recorder.NAME_FORMAT) + ".json.gz" for hours in (3, 2, 1)]
    (tmp_path / "records" / old).write_bytes(b"x" * 10)
    for name in names[:2]:
        (tmp_path / "records" / name).write_bytes(b"x" * 10)
    (tmp_path / "outbox" / names[2]).write_bytes(b"x" * 10)
    monkeypatch.setattr(recorder, "MAXIMUM_LOCAL_BYTES", 25)
    assert Recorder.pruneRecords(tmp_path, NOW) == 2
    remaining: list[str] = sorted(path.name for folder in ("records", "outbox") for path in (tmp_path / folder).iterdir())
    assert remaining == names[1:]


def test_run_writes_a_record_and_then_compares_against_it(tmp_path: Path) -> None:
    logs: Path = tmp_path / "logs"
    logs.mkdir()
    index: str = namespaceIndex(PROD_PREFIX, "a")
    log: Path = logs / (CLUSTER + "_server.json")
    log.write_text("")
    opensearch = FakeOpenSearch([{"index": index, "status": "open", "search.query_total": "3"}])
    state: Path = tmp_path / "state"
    assert Recorder.run(directory=state, destination=None, opensearch=opensearch, logDirectory=logs, now=NOW) == 0
    with log.open("a") as stream:
        stream.write(openLine(NOW + timedelta(minutes=1), index))
    opensearch.rows = [{"index": index, "status": "open", "search.query_total": "4"}]
    later: datetime = NOW + timedelta(minutes=30)
    assert Recorder.run(directory=state, destination=None, opensearch=opensearch, logDirectory=logs, now=later) == 0
    record: dict[str, object] = json.loads(gzip.decompress((state / "outbox" / "20261115T083500Z.json.gz").read_bytes()))
    assert record["schemaVersion"] == "knowhere-opensearch-metrics-v1"
    assert record["previousRecordedAt"] == "2026-11-15T08:05:00Z"
    assert record["namespaceIndexes"] == {PROD_PREFIX: {"openIndexes": 1, "closedIndexes": 0, "activeNamespaces": 1,
        "opens": 1, "closes": 0, "maxOpensIn10Seconds": 1}}
    assert record["lifecycle"] == {"opens": 1, "closes": 0, "maxOpensIn10Seconds": 1, "isComplete": True}
    assert record["counters"]["gc"]["young"] == {"count": 7, "millis": 70}  # type: ignore[index]
    assert record["cluster"] == {"status": "green", "activeShards": 186, "pendingTasks": 0}


def test_unreadable_node_is_recorded_and_keeps_the_last_good_state(tmp_path: Path) -> None:
    class DownOpenSearch:
        def get(self, path: str) -> object:
            raise ConnectionRefusedError("node is down for " + path)

    logs: Path = tmp_path / "logs"
    logs.mkdir()
    (logs / (CLUSTER + "_server.json")).write_text("")
    state: Path = tmp_path / "state"
    assert Recorder.run(directory=state, destination=None, opensearch=FakeOpenSearch([]), logDirectory=logs, now=NOW) == 0
    saved: bytes = (state / "state.json").read_bytes()
    assert Recorder.run(directory=state, destination=None, opensearch=DownOpenSearch(), logDirectory=logs,
        now=NOW + timedelta(minutes=30)) == 1
    record: dict[str, object] = json.loads(gzip.decompress((state / "outbox" / "20261115T083500Z.json.gz").read_bytes()))
    assert str(record["error"]).startswith("ConnectionRefusedError")
    assert (state / "state.json").read_bytes() == saved
    assert not [path for path in os.listdir(state) if path.endswith(".tmp")]
