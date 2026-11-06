"""Export→full snapshot→independent restore/read→verified point is executable."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest

from test_offline_backup import createExport


def loadWorkflow() -> type:
    directory: Path = Path(__file__).parents[1]
    sys.path.insert(0, str(directory))
    path: Path = directory / "backup_workflow.py"
    assert path.is_file(), "Authoritative independent backup workflow is absent"
    specification = importlib.util.spec_from_file_location("backup_workflow", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.BackupWorkflow)


def test_completed_snapshot_without_restored_payload_receipt_cannot_qualify_point() -> None:
    workflow: type = loadWorkflow()
    calls: list[str] = []
    exported: dict[str, object] = createExport()

    class Source:
        def export(self, **_arguments: object) -> dict[str, object]:
            calls.append("authoritative-export")
            return exported

        def verifyRestored(self, *_arguments: object, **_options: object) -> dict[str, object]:
            raise AssertionError("Injected offline driver returns no data proof")

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        if request.url.path.startswith("/_cluster/state/metadata"):
            return httpx.Response(200, json={"metadata": {"indices": {"knowhere-one": {"state": "close",
                "settings": {"index": {"uuid": "old-one", "number_of_shards": "1"}}}}}})
        if request.url.path.startswith("/_cluster/state/nodes"):
            return httpx.Response(200, json={"cluster_uuid": "source", "nodes": {"source": {"ephemeral_id": "source-node"}}})
        if request.method == "PUT":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/_status"):
            return httpx.Response(200, json={"snapshots": [{"snapshot": "full-one", "uuid": "snapshot-one", "state": "SUCCESS", "indices": {"knowhere-one": {"shards": {"0": {"stage": "DONE"}}}}}]})
        return httpx.Response(200, json={"snapshots": [{"snapshot": "full-one", "uuid": "snapshot-one", "state": "SUCCESS", "indices": ["knowhere-one"],
            "remote_store_index_shallow_copy": False, "shards": {"total": 1, "successful": 1, "failed": 0},
            "metadata": {"inventory_sha256": exported["inventory_sha256"], "boundary_sha256": exported["boundary_sha256"]},
            "start_time_in_millis": 2_000, "end_time_in_millis": 3_000}]})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as sourceClient:
        result = workflow.run(Source(), sourceClient, indexPrefix="knowhere", snapshotName="full-one", expectedEpoch=4,
            expectedClusterUuid="source", expectedIncarnation="source-node", state={}, timeoutSeconds=10,
            verifyIndependent=lambda *_arguments: {"data_verified": False, "status": "incomplete"}, persist=lambda _value: None)
    assert result["data_verified"] is False
    assert result["coverage_verified"] is False
    assert result["status"] == "awaiting-independent-verification"
    assert calls[0] == "authoritative-export"
    assert not any("/_open" in call for call in calls)


def test_changed_source_client_identity_cannot_submit_snapshot() -> None:
    workflow: type = loadWorkflow()

    class Source:
        def export(self, **_arguments: object) -> dict[str, object]:
            return createExport()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.startswith("/_cluster/state")
        return httpx.Response(200, json={"cluster_uuid": "wrong-source", "nodes": {"source": {"ephemeral_id": "source-node"}}})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="source native identity"):
            workflow.run(Source(), client, indexPrefix="knowhere", snapshotName="full-one", expectedEpoch=4,
                expectedClusterUuid="source", expectedIncarnation="source-node", state={}, timeoutSeconds=10,
                verifyIndependent=lambda *_arguments: pytest.fail("Wrong endpoint cannot restore"), persist=lambda _value: None)
