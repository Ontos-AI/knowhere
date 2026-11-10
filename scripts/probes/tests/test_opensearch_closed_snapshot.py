"""Owned two-index filesystem probe stays dry-run until explicitly executed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest


def loadProbe() -> type:
    path: Path = Path(__file__).parents[1] / "verify-opensearch-closed-snapshot.py"
    assert path.is_file(), "Closed-index full snapshot qualification probe is absent"
    specification = importlib.util.spec_from_file_location("closed_snapshot_probe", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return cast(type, module.ClosedSnapshotProbe)


def test_probe_defaults_to_dryrun_without_native_requests(tmp_path: Path) -> None:
    probe: type = loadProbe()
    assert probe
    command: Path = Path(__file__).parents[1] / "verify-opensearch-closed-snapshot.py"
    result = subprocess.run([sys.executable, str(command), "--endpoint", "http://127.0.0.1:32786",
        "--repository-root", "/tmp/knowhere-kw01-backups", "--receipt", str(tmp_path / "receipt.json")], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["maximum_probe_indices"] == 2
    assert plan["aws_operations"] is False
    assert not (tmp_path / "receipt.json").exists()


@pytest.mark.parametrize("omitClosed", [False, True])
def test_closed_catalog_requires_done_shards_and_actual_restored_content(omitClosed: bool) -> None:
    probe: type = loadProbe()
    prefix: str = "kwclosed-" + "a" * 16
    names: list[str] = [prefix + "-hot", prefix + "-cold"]
    requests: list[httpx.Request] = []
    hasRestored: bool = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal hasRestored
        requests.append(request)
        path: str = request.url.path
        if path == "/":
            return httpx.Response(200, json={"version": {"number": "3.9.0", "build_hash": "4ee42a94e87f66fbf1e62a9871b1b87f91e02472"}})
        if path.startswith("/_cat/"):
            return httpx.Response(200, json=[{"index": name, "uuid": ("new-" if hasRestored else "old-") + name, "status": "open" if hasRestored or name.endswith("hot") else "close"} for name in names])
        if path.endswith("/_restore"):
            hasRestored = True
            return httpx.Response(200, json={"snapshot": {"shards": {"total": 2, "successful": 2, "failed": 0}}})
        if path.endswith("/_status"):
            return httpx.Response(200, json={"snapshots": [{"snapshot": "full", "uuid": "snapshot-id", "state": "SUCCESS", "indices": {name: {"shards": {"0": {"stage": "DONE"}}} for name in names}}]})
        if path.startswith("/_snapshot/") and request.method == "GET":
            return httpx.Response(200, json={"snapshots": [{"snapshot": "full", "uuid": "snapshot-id", "state": "SUCCESS", "indices": names[:1] if omitClosed else names, "remote_store_index_shallow_copy": False, "shallow_snapshot_v2": False, "shards": {"total": 2, "successful": 2, "failed": 0}}]})
        if path.endswith("/_doc/proof") and request.method == "GET":
            return httpx.Response(200, json={"found": True, "_source": {"proof": path.split("/")[1]}})
        return httpx.Response(200, json={"acknowledged": True, "shards_acknowledged": True})

    with httpx.Client(base_url="http://127.0.0.1:32786", transport=httpx.MockTransport(respond)) as client:
        if omitClosed:
            with pytest.raises(ValueError, match="catalog"):
                probe.run(client, prefix=prefix, repositoryRoot="/tmp/knowhere-kw01-backups")
            assert not any(request.method == "DELETE" for request in requests)
            return
        result = probe.run(client, prefix=prefix, repositoryRoot="/tmp/knowhere-kw01-backups")
    assert result["closed_full_copy_verified"] is True
    assert result["restored_content_verified"] is True
    assert len(result["identity_mapping"]) == 2
    assert not any(request.url.path.endswith("/_open") for request in requests)
    snapshotRequest = next(request for request in requests if request.url.path.endswith("/full") and request.method == "PUT")
    assert json.loads(snapshotRequest.content)["expand_wildcards"] == "all"
    assert {request.url.path for request in requests if request.method == "DELETE"} == {"/" + ",".join(names)}
