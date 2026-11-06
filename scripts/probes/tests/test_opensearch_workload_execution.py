"""Exercise the workload boundary end to end with isolated component doubles."""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from test_opensearch_workload import createArrivals, createManifest


def _loadRunner() -> ModuleType:
    script: Path = Path(__file__).parents[1] / "run-opensearch-workload.py"
    specification = importlib.util.spec_from_file_location("workload_execution_runner", script)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_short_component_campaign_exercises_admission_broker_source_export_and_overlap(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner: ModuleType = _loadRunner()
    support: ModuleType = runner._loadSupport()
    value: dict[str, object] = createManifest()
    runtime: dict[str, object] = cast(dict[str, object], value["runtime"])
    runtime.update({"request_timeout_seconds": 5, "drain_grace_seconds": 2, "resource_sample_interval_seconds": 1})
    manifest = support.WorkloadSupport.parseManifest(value)
    manifestPath: Path = tmp_path / "manifest.json"
    manifestPath.write_text(json.dumps(value))
    reads, writes = createArrivals()
    plan: dict[str, object] = support.WorkloadSupport.buildSchedule(value, reads, writes)
    events: list[dict[str, object]] = cast(list[dict[str, object]], plan["events"])
    write: dict[str, object] = next(event for event in events if event["kind"] == "write")
    plan["events"] = [{**event, "offset_ms": 0} for event in events if event is write or event["kind"] in ("snapshot", "maintenance")]
    signalPath: Path = tmp_path / "admission.json"
    telemetryPath: Path = tmp_path / "telemetry.jsonl"
    source: Path = tmp_path / "callback.py"
    source.write_text("""
import argparse, hashlib, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--marker',type=Path)
parser.add_argument('--telemetry',type=Path)
parser.add_argument('--request',type=Path)
parser.add_argument('--receipt',type=Path)
args=parser.parse_args()
data=json.loads(args.request.read_bytes())
result={k:data[k] for k in ('campaign_id','source_hash')}
result.update(mode='actual',status='passed',errors=[])
interval={'started_at_utc':'2026-01-01T00:00:01+00:00','finished_at_utc':'2026-01-01T00:00:03+00:00'}
kind=data['event']
if kind=='precondition':
 result['cohorts']={'cold':{'mode':'actual'}}
if kind=='admission':
 arrival=data['arrival']
 args.marker.write_text(json.dumps(arrival))
 result.update(admitted=True,job_id=arrival['job_id'],admitted_at_utc=interval['started_at_utc'])
if kind=='snapshot':
 result.update(completed=True,coverage_verified=True,snapshot_interval_utc=interval)
if kind=='maintenance':
 result['completed']=True
if kind=='resource':
 result['complete']=True
if kind=='stage-export':
 identity=json.loads(args.marker.read_bytes())['id']
 args.telemetry.write_text(json.dumps({**result,'id':identity,'indexing_operation_ms':1,'native_bulk_bytes':[101],
  'indexing_intervals_utc':[interval]})+'\\n')
 result.update(telemetry_path=str(args.telemetry),telemetry_sha256=hashlib.sha256(args.telemetry.read_bytes()).hexdigest())
args.receipt.write_text(json.dumps(result))
""")
    binding: dict[str, object] = {"script_path": str(source), "script_sha256": sha256(source.read_bytes()).hexdigest(),
        "argv": [sys.executable, str(source), "--marker", str(signalPath), "--telemetry", str(telemetryPath)], "timeout_seconds": 4}
    endpoint: Path = tmp_path / "broker"
    endpoint.write_text("redis://127.0.0.1:9/0")
    endpoint.chmod(0o600)
    issued: list[str] = []
    producer: ModuleType = ModuleType("celery")
    worker: ModuleType = ModuleType("shared.core.celery_app")
    worker.celery_app = SimpleNamespace(conf=SimpleNamespace(broker_transport_options={"global_keyprefix": "{celery}"},
        result_backend_transport_options={"global_keyprefix": "{celery}"}, task_queue_max_priority=9, task_default_priority=3))

    class Producer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.conf: dict[str, object] = {}

        def send_task(self, name: str, **values: object) -> object:
            assert self.conf["broker_transport_options"] == {"global_keyprefix": "{celery}", "retry_on_timeout": False}
            body: dict[str, object] = cast(list[dict[str, object]], values["args"])[0]
            assert body["event_id"] == write["id"] and body["source_hash"] == runtime["source_hash"]
            assert json.loads(signalPath.read_bytes())["job_id"] == write["job_id"]
            assert values["queue"] == runtime["worker_queue"] and values["ignore_result"] is False
            issued.append(name)
            return SimpleNamespace(get=lambda **_arguments: {"status": "success", "job_state": "done", "unit_count": 1862})

        def close(self) -> None:
            issued.append("closed")

    producer.Celery = Producer
    monkeypatch.setitem(sys.modules, "celery", producer)
    monkeypatch.setitem(sys.modules, "shared.core.celery_app", worker)
    groups: list[int] = []
    output: Path = tmp_path / "run"
    report: dict[str, object] = asyncio.run(runner._run(argparse.Namespace(output=output, manifest=manifestPath, simulate=False),
        support, manifest, plan, {}, {"bindings": {name: binding for name in ("precondition", "admission", "resource", "snapshot", "maintenance", "stage-export")},
            "broker_url_file": str(endpoint), "result_backend_url_file": str(endpoint),
            "job_owners": {str(write["job_id"]): "fixture-owner"}, "prepared_worker_paths": {str(write["case_id"]): "/private/prepared.gz"}},
        recordGroup=groups.append, runWindowMs=350))
    assert issued == ["knowhere.probes.complete_prepared", "closed"]
    assert len(groups) == 6
    assert report["actual_staging_campaign"] is False
    assert cast(dict[str, object], report["qualification"])["qualified"] is False
    observations: list[dict[str, object]] = cast(list[dict[str, object]], report["observations"])
    assert next(row for row in observations if row["id"] == write["id"])["indexing_operation_ms"] == 1
    overlap: dict[str, object] = next(row for row in cast(list[dict[str, object]], report["overlap_results"]) if row["kind"] == "snapshot")
    assert overlap["overlap_witnesses"] == [{"event_id": write["id"], "kind": "indexing"}]
    assert (output / "report.json").is_file()
