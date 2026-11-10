"""Bounded two-index CLOSED full-copy/restore probe for an owned loopback node.

The parent operator runs this serially after configuring an existing path.repo.
It retains its two restored indices and FS snapshot for inspection. It never
changes a node setting, opens the CLOSED source, calls AWS, or touches another
index. This is component evidence, not independent S3 recovery qualification.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
from typing import cast
from uuid import uuid4

import httpx

_BUILD: str = "4ee42a94e87f66fbf1e62a9871b1b87f91e02472"


def _request(client: httpx.Client, method: str, path: str, body: object | None = None) -> dict[str, object]:
    response: httpx.Response = client.request(method, path, json=body)
    response.raise_for_status()
    result: object = response.json()
    if not isinstance(result, dict):
        raise ValueError("Expected an object from the owned native node")
    return cast(dict[str, object], result)


def _catalog(client: httpx.Client, names: list[str]) -> list[dict[str, str]]:
    response: httpx.Response = client.get(f"/_cat/indices/{','.join(names)}?format=json&h=index,uuid,status&expand_wildcards=all")
    response.raise_for_status()
    rows: list[dict[str, str]] = response.json()
    if len(rows) != 2 or {row["index"] for row in rows} != set(names):
        raise ValueError("Probe catalog differs from its exact two owned indices")
    return rows


class ClosedSnapshotProbe:
    @staticmethod
    def run(client: httpx.Client, *, prefix: str, repositoryRoot: str) -> dict[str, object]:
        if re.fullmatch(r"kwclosed-[a-f0-9]{16}", prefix) is None or repositoryRoot != "/tmp/knowhere-kw01-backups":
            raise ValueError("Use a unique owned probe prefix and the approved temporary filesystem repository")
        version: object = _request(client, "GET", "/").get("version")
        if not isinstance(version, dict) or version.get("number") != "3.9.0" or version.get("build_hash") != _BUILD:
            raise ValueError("Probe requires the exact reviewed OpenSearch 3.9.0 build")
        names: list[str] = [prefix + "-hot", prefix + "-cold"]
        repository: str = prefix + "-repo"
        for name in names:
            _request(client, "PUT", f"/{name}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
            _request(client, "PUT", f"/{name}/_doc/proof?refresh=wait_for", {"proof": name})
        closed: dict[str, object] = _request(client, "POST", f"/{names[1]}/_close?wait_for_active_shards=all&timeout=10s")
        if closed.get("acknowledged") is not True or closed.get("shards_acknowledged") is not True:
            raise ValueError("Source CLOSED transition was not acknowledged")
        before: list[dict[str, str]] = _catalog(client, names)
        if next(row for row in before if row["index"] == names[1])["status"] != "close":
            raise ValueError("Cold source was not CLOSED before snapshot")
        _request(client, "PUT", f"/_snapshot/{repository}", {"type": "fs", "settings": {
            "location": repositoryRoot + "/" + prefix, "remote_store_index_shallow_copy": False, "shallow_snapshot_v2": False,
        }})
        _request(client, "PUT", f"/_snapshot/{repository}/full?wait_for_completion=true", {
            "indices": names, "expand_wildcards": "all", "ignore_unavailable": False,
            "include_global_state": False, "partial": False,
        })
        snapshot: dict[str, object] = cast(list[dict[str, object]], _request(client, "GET", f"/_snapshot/{repository}/full")["snapshots"])[0]
        if (snapshot.get("state") != "SUCCESS" or set(cast(list[str], snapshot.get("indices", []))) != set(names)
                or snapshot.get("shards") != {"total": 2, "successful": 2, "failed": 0}
                or snapshot.get("remote_store_index_shallow_copy") is not False or snapshot.get("shallow_snapshot_v2", False) is not False):
            raise ValueError("Full snapshot did not cover the exact OPEN/CLOSED catalog")
        statuses: list[dict[str, object]] = cast(list[dict[str, object]], _request(client, "GET", f"/_snapshot/{repository}/full/_status")["snapshots"])
        status: dict[str, object] = statuses[0]
        shards: dict[str, dict[str, object]] = cast(dict[str, dict[str, object]], status["indices"])
        if (status.get("uuid") != snapshot.get("uuid") or status.get("state") != "SUCCESS" or set(shards) != set(names)
                or any(set(cast(dict[str, object], shards[name]["shards"])) != {"0"}
                    or cast(dict[str, dict[str, object]], shards[name]["shards"])["0"].get("stage") != "DONE" for name in names)):
            raise ValueError("CLOSED snapshot primary shard is missing or unfinished")
        after: list[dict[str, str]] = _catalog(client, names)
        if sorted(before, key=lambda row: row["index"]) != sorted(after, key=lambda row: row["index"]):
            raise ValueError("Snapshot changed the source UUID/state or opened the CLOSED source")
        # Only these two unique probe sources are deleted; restore proves that
        # the completed full FS snapshot contains their data without source shards.
        deleted: dict[str, object] = _request(client, "DELETE", "/" + ",".join(names))
        if deleted.get("acknowledged") is not True:
            raise ValueError("Owned source deletion was not acknowledged")
        restored: dict[str, object] = _request(client, "POST", f"/_snapshot/{repository}/full/_restore?wait_for_completion=true", {
            "indices": names, "include_global_state": False, "include_aliases": False, "partial": False, "storage_type": "local",
        })
        restoreSnapshot: dict[str, object] = cast(dict[str, object], restored.get("snapshot", {}))
        if restoreSnapshot.get("shards") != {"total": 2, "successful": 2, "failed": 0}:
            raise ValueError("Owned full restore did not complete every shard")
        for name in names:
            document: dict[str, object] = _request(client, "GET", f"/{name}/_doc/proof")
            if document.get("found") is not True or document.get("_source") != {"proof": name}:
                raise ValueError("Restored full-copy content differs from the original proof")
        final: list[dict[str, str]] = _catalog(client, names)
        old: dict[str, str] = {row["index"]: row["uuid"] for row in before}
        return {"mode": "actual", "status": "passed", "build_hash": _BUILD, "maximum_probe_indices": 2,
            "closed_full_copy_verified": True, "restored_content_verified": True,
            "aws_operations": False, "independent_s3_recovery_verified": False,
            "repository": repository, "snapshot": "full", "snapshot_uuid": snapshot["uuid"],
            "retained_probe_indices": names, "repository_location": repositoryRoot + "/" + prefix,
            "identity_mapping": [{"index_name": row["index"], "old_index_uuid": old[row["index"]], "restored_index_uuid": row["uuid"]} for row in final]}


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    endpoint: httpx.URL = httpx.URL(arguments.endpoint)
    if endpoint.scheme != "http" or not ipaddress.ip_address(endpoint.host).is_loopback or endpoint.userinfo or endpoint.path not in ("", "/"):
        raise ValueError("Only an explicitly owned literal loopback node is supported")
    if arguments.receipt.exists() or arguments.repository_root != "/tmp/knowhere-kw01-backups":
        raise ValueError("Use a new private receipt and the preconfigured approved path.repo")
    prefix: str = "kwclosed-" + uuid4().hex[:16]
    if not arguments.execute:
        print(json.dumps({"mode": "dry-run", "maximum_probe_indices": 2, "primary_shards_per_index": 1,
            "documents_per_index": 1, "aws_operations": False, "prefix": prefix,
            "requires_existing_path_repo": arguments.repository_root, "retains_two_restored_indices_and_snapshot": True}))
        return 0
    arguments.receipt.parent.mkdir(parents=True, exist_ok=True)
    with arguments.receipt.open("x") as output:
        arguments.receipt.chmod(0o600)
        output.write(json.dumps({"mode": "actual", "status": "intent", "prefix": prefix, "endpoint": str(endpoint)}))
    with httpx.Client(base_url=endpoint, timeout=60, trust_env=False) as client:
        result: dict[str, object] = ClosedSnapshotProbe.run(client, prefix=prefix, repositoryRoot=arguments.repository_root)
    arguments.receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError, httpx.HTTPError):
        print("Closed-snapshot probe incomplete; inspect its intent receipt and retain its exact owned resources.", file=sys.stderr)
        raise SystemExit(2) from None
