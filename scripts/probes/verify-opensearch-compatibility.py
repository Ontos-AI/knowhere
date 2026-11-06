"""Run an isolated remote-store compatibility probe on the approved Oracle host.

This Linux-only probe uses synthetic data and disposable, uniquely named Docker
resources. It refuses to start while the existing comparison reports running.
Moto emulates S3; this is not AWS durability, capacity, or Knowhere acceptance.
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

JsonObject = dict[str, object]
DocumentLedger = dict[str, dict[str, JsonObject]]
BASE_IMAGE: str = "opensearchproject/opensearch@sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e"
BASE_CONFIGURATION: str = (
    "sha256:4fc0ee89d630dfe0d6d330445d651d00ccc066c604f91801cbcdc387901f22f5"
)
MOTO_IMAGE: str = "motoserver/moto@sha256:804360b4774c5c755e776e5cf9200d9385030e6d58d5c36a1786090ddfbece10"
SOURCE_COMMIT: str = "4ee42a94e87f66fbf1e62a9871b1b87f91e02472"
APPROVED_HOSTNAME: str = "instance-20220917-1550"
DOCKER_PREFIX: list[str] = ["docker", "--host", "unix:///var/run/docker.sock"]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def requireObject(value: object) -> JsonObject:
    require(isinstance(value, dict), "Expected a JSON object")
    return cast(JsonObject, value)


def writeJson(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


class CompatibilityProbe:
    def __init__(self, directory: Path) -> None:
        self.directory: Path = directory
        self.prefix: str = "knowhere-compat-" + uuid4().hex[:10]
        self.network: str = self.prefix + "-network"
        self.image: str = self.prefix + ":s3"
        self.containers: list[str] = []
        self.volumes: list[str] = []
        self.cases: list[JsonObject] = []
        self.requests: list[JsonObject] = []
        self.baseline: dict[str, JsonObject] = {}
        self.currentContainer: str | None = None
        self.currentVolume: str | None = None
        self.startedAt: str = datetime.now(UTC).isoformat()
        self.report: JsonObject = {
            "startedAt": self.startedAt,
            "status": "preparing",
            "resourcePrefix": self.prefix,
            "openSearchImage": BASE_IMAGE,
            "openSearchConfigurationDigest": BASE_CONFIGURATION,
            "s3EmulatorImage": MOTO_IMAGE,
            "cases": self.cases,
            "scope": "Synthetic single-node functionality with S3 emulation; no application or AWS acceptance",
        }

    def save(self) -> None:
        writeJson(self.directory / "result.json", self.report)
        writeJson(self.directory / "http-evidence.json", self.requests)

    def recordCase(self, name: str, evidence: JsonObject) -> None:
        result: JsonObject = {"name": name, "passed": True, "evidence": evidence}
        self.cases.append(result)
        self.save()
        print(json.dumps(result), flush=True)

    def runDocker(self, arguments: list[str], *, check: bool = True) -> str:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            DOCKER_PREFIX + arguments, text=True, capture_output=True, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError("Docker operation failed: " + result.stderr[-3000:])
        return result.stdout.strip()

    def captureContainers(self) -> dict[str, JsonObject]:
        identifiers: list[str] = self.runDocker(["ps", "-aq"]).splitlines()
        if not identifiers:
            return {}
        rows: object = json.loads(self.runDocker(["inspect"] + identifiers))
        require(isinstance(rows, list), "Expected container inventory")
        inventory: dict[str, JsonObject] = {}
        for value in cast(list[object], rows):
            row: JsonObject = requireObject(value)
            state: JsonObject = requireObject(row["State"])
            inventory[str(row["Id"])] = {
                "name": row["Name"],
                "running": state["Running"],
                "paused": state["Paused"],
                "status": state["Status"],
            }
        return inventory

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        port: int = 19371,
        timeout: float = 45,
        expected: tuple[int, ...] = (200, 201),
    ) -> JsonObject:
        body: bytes | None = None
        contentType: str = "application/json"
        if isinstance(payload, str):
            body = payload.encode()
            contentType = "application/x-ndjson"
        elif payload is not None:
            body = json.dumps(payload).encode()
        request: Request = Request(
            f"http://127.0.0.1:{port}" + path,
            data=body,
            method=method,
            headers={"Content-Type": contentType},
        )
        started: float = time.monotonic()
        status: int
        data: bytes
        try:
            with urlopen(request, timeout=timeout) as response:
                status, data = response.status, response.read()
        except HTTPError as err:
            status, data = err.code, err.read()
        decoded: object
        try:
            decoded = json.loads(data) if data else {}
        except json.JSONDecodeError:
            decoded = {"text": data.decode(errors="replace")[:2000]}
        result: JsonObject = requireObject(decoded)
        self.requests.append(
            {
                "method": method,
                "path": path,
                "port": port,
                "status": status,
                "elapsedMilliseconds": round((time.monotonic() - started) * 1000, 3),
                "response": result,
            }
        )
        require(
            status in expected, f"{method} {path}: HTTP {status}: {str(result)[:1200]}"
        )
        return result

    def waitForHttp(self, port: int, seconds: float = 150) -> None:
        deadline: float = time.monotonic() + seconds
        lastError: str = "not contacted"
        while time.monotonic() < deadline:
            try:
                self.request("GET", "/", port=port, timeout=3)
                return
            except (URLError, TimeoutError, ConnectionError, AssertionError) as err:
                lastError = str(err)
                time.sleep(0.5)
        raise RuntimeError(f"Local test endpoint {port} did not start: {lastError}")

    def startStore(self, suffix: str, port: int, bucket: str) -> str:
        name: str = self.prefix + "-" + suffix
        self.containers.append(name)
        self.runDocker(
            [
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "knowhere.compatibility=" + self.prefix,
                "--network",
                self.network,
                "--memory",
                "384m",
                "--cpus",
                "0.25",
                "-p",
                f"127.0.0.1:{port}:5000",
                "--entrypoint",
                "moto_server",
                MOTO_IMAGE,
                "-H",
                "0.0.0.0",
                "-p",
                "5000",
            ]
        )
        self.waitForHttp(port, 45)
        self.request("PUT", "/" + bucket, port=port)
        return name

    def prepare(self) -> None:
        self.baseline = self.captureContainers()
        writeJson(self.directory / "baseline-containers.json", self.baseline)
        identity: str = self.runDocker(
            ["image", "inspect", BASE_IMAGE, "--format", "{{.Id}}"]
        )
        require(
            identity == BASE_CONFIGURATION,
            "Existing OpenSearch image differs from the pinned official image",
        )
        for port in [19371, 19372, 19373, 19374]:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
        print("Preparing isolated S3 plugin image and S3 emulator", flush=True)
        dockerfile: Path = self.directory / "Dockerfile"
        dockerfile.write_text(
            "FROM " + BASE_IMAGE + "\n"
            "RUN bin/opensearch-plugin list | grep -qx repository-s3 || "
            "bin/opensearch-plugin install --batch repository-s3\n"
        )
        build: subprocess.CompletedProcess[str] = subprocess.run(
            DOCKER_PREFIX
            + ["build", "--pull=false", "-t", self.image, str(self.directory)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.directory / "image-build.log").write_text(build.stdout)
        require(
            build.returncode == 0,
            "S3 plugin image build failed; inspect image-build.log",
        )
        self.runDocker(["pull", MOTO_IMAGE])
        self.runDocker(["network", "create", self.network])
        self.startStore("live", 19372, "live")
        self.startStore("backup", 19373, "backup")

    def startNode(self, suffix: str, *, isSnapshotTarget: bool = False) -> JsonObject:
        store: str = self.prefix + ("-target" if isSnapshotTarget else "-live")
        configuration: JsonObject = {
            "cluster.name": self.prefix
            + ("-independent" if isSnapshotTarget else "-original"),
            "node.name": self.prefix + "-node",
            "node.roles": ["cluster_manager", "data", "ingest"],
            "discovery.type": "single-node",
            "network.host": "0.0.0.0",
            "plugins.security.disabled": True,
            "node.store.allow_mmap": False,
            "cluster.remote_store.state.enabled": True,
        }
        for client, endpoint in [
            ("default", store),
            ("backup", self.prefix + "-backup"),
        ]:
            for key, value in {
                "endpoint": endpoint + ":5000",
                "protocol": "http",
                "region": "us-east-1",
                "path_style_access": True,
                "max_retries": 0,
            }.items():
                configuration[f"s3.client.{client}.{key}"] = value
        for component in ["segment", "translog", "state"]:
            repository: str = ("fresh-" if isSnapshotTarget else "compat-") + component
            configuration[f"node.attr.remote_store.{component}.repository"] = repository
            configuration[f"node.attr.remote_store.repository.{repository}.type"] = "s3"
            for key, value in {
                "bucket": "fresh" if isSnapshotTarget else "live",
                "base_path": component,
                "region": "us-east-1",
                "client": "default",
                "s3_async_client_type": "netty",
            }.items():
                configuration[
                    f"node.attr.remote_store.repository.{repository}.settings.{key}"
                ] = value
        configPath: Path = self.directory / (suffix + "-opensearch.yml")
        writeJson(configPath, configuration)
        configPath.chmod(0o644)
        name: str = self.prefix + "-" + suffix
        volume: str = name + "-data"
        self.containers.append(name)
        self.volumes.append(volume)
        shell: str = (
            "set -e; for client in default backup; do "
            "printf %s local-test-access | bin/opensearch-keystore add -f --stdin s3.client.$client.access_key; "
            "printf %s local-test-secret | bin/opensearch-keystore add -f --stdin s3.client.$client.secret_key; "
            "done; exec bin/opensearch"
        )
        self.runDocker(
            [
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "knowhere.compatibility=" + self.prefix,
                "--network",
                self.network,
                "--memory",
                "1792m",
                "--cpus",
                "1",
                "--ulimit",
                "nofile=65536:65536",
                "-p",
                "127.0.0.1:19371:9200",
                "-e",
                "OPENSEARCH_JAVA_OPTS=-Xms768m -Xmx768m",
                "-v",
                str(configPath) + ":/usr/share/opensearch/config/opensearch.yml:ro",
                "-v",
                volume + ":/usr/share/opensearch/data",
                "--entrypoint",
                "/bin/bash",
                self.image,
                "-c",
                shell,
            ]
        )
        self.currentContainer, self.currentVolume = name, volume
        self.waitForHttp(19371)
        root: JsonObject = self.request("GET", "/")
        version: JsonObject = requireObject(root["version"])
        require(
            version["number"] == "3.9.0" and version["build_hash"] == SOURCE_COMMIT,
            "Unexpected runtime build",
        )
        writeJson(self.directory / (suffix + "-identity.json"), root)
        return root

    def discardNode(self) -> None:
        require(
            self.currentContainer is not None and self.currentVolume is not None,
            "No test node",
        )
        require(
            self.currentContainer.startswith(self.prefix),
            "Refusing to discard an unrelated container",
        )
        self.runDocker(["kill", "--signal", "KILL", self.currentContainer])
        logs: subprocess.CompletedProcess[str] = subprocess.run(
            DOCKER_PREFIX + ["logs", self.currentContainer],
            text=True,
            capture_output=True,
            check=False,
        )
        (self.directory / (self.currentContainer + ".log")).write_text(
            logs.stdout + logs.stderr
        )
        self.runDocker(["rm", self.currentContainer])
        self.runDocker(["volume", "rm", self.currentVolume])
        self.currentContainer, self.currentVolume = None, None

    def bulk(self, operations: list[JsonObject]) -> JsonObject:
        text: str = "\n".join(json.dumps(row) for row in operations) + "\n"
        result: JsonObject = self.request("POST", "/_bulk?refresh=false", text)
        require(result.get("errors") is False, "Bulk envelope contains failed items")
        items: object = result.get("items")
        require(isinstance(items, list), "Missing bulk items")
        for item in cast(list[object], items):
            entry: JsonObject = requireObject(next(iter(requireObject(item).values())))
            require(
                int(cast(int, entry["status"])) in {200, 201},
                "Bulk item was not acknowledged",
            )
        return result

    def seed(self) -> DocumentLedger:
        ledger: DocumentLedger = {}
        for namespace in ["a", "b", "c"]:
            index: str = "compat-namespace-" + namespace
            self.request(
                "PUT",
                "/" + index,
                {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                        "replication.type": "SEGMENT",
                        "translog.durability": "request",
                        "refresh_interval": "1s",
                        "similarity": {
                            "knowhere_bm25": {"type": "BM25", "k1": 1.5, "b": 0.75}
                        },
                    },
                    "mappings": {
                        "dynamic": "strict",
                        "properties": {
                            "path": {
                                "type": "text",
                                "analyzer": "whitespace",
                                "similarity": "knowhere_bm25",
                            },
                            "content": {
                                "type": "text",
                                "analyzer": "whitespace",
                                "similarity": "knowhere_bm25",
                            },
                            "revision": {"type": "keyword"},
                        },
                    },
                },
            )
            settings: JsonObject = requireObject(
                requireObject(
                    self.request("GET", "/" + index + "/_settings?flat_settings=true")[
                        index
                    ]
                )["settings"]
            )
            require(
                settings.get("index.remote_store.enabled") == "true",
                "Remote storage not enabled",
            )
            require(
                settings.get("index.replication.type") == "SEGMENT",
                "Segment replication not enabled",
            )
            require(
                settings.get("index.translog.durability") == "request",
                "Request durability not enabled",
            )
            ledger[index] = {}
            operations: list[JsonObject] = []
            for number in range(8):
                identifier: str = "unit-" + str(number)
                document: JsonObject = {
                    "path": f"namespace_{namespace} chapter",
                    "content": f"shared original marker_{number}",
                    "revision": "revision-1",
                }
                ledger[index][identifier] = document
                operations.extend(
                    [{"index": {"_index": index, "_id": identifier}}, document]
                )
            self.bulk(operations)
            self.request("POST", "/" + index + "/_refresh")
        self.request("PUT", "/compat-namespace-a/_alias/compat-alias-a")
        self.verifyLedger(ledger)
        return ledger

    def verifyLedger(self, ledger: DocumentLedger) -> None:
        for index, expected in ledger.items():
            result: JsonObject = self.request(
                "POST",
                "/" + index + "/_search?request_cache=false",
                {"size": 100, "query": {"match_all": {}}},
            )
            shards: JsonObject = requireObject(result["_shards"])
            require(shards["failed"] == 0, "Search has failed shards")
            hits: object = requireObject(result["hits"])["hits"]
            require(isinstance(hits, list), "Missing search hits")
            actual: dict[str, JsonObject] = {
                str(requireObject(hit)["_id"]): requireObject(
                    requireObject(hit)["_source"]
                )
                for hit in cast(list[object], hits)
            }
            require(actual == expected, "Recovered documents differ in " + index)
            for field, term in [
                ("path", index.replace("compat-", "").replace("-", "_")),
                ("content", "shared"),
            ]:
                matches: JsonObject = self.request(
                    "POST",
                    "/" + index + "/_search?request_cache=false",
                    {"size": 100, "query": {"match": {field: term}}},
                )
                count: JsonObject = requireObject(
                    requireObject(matches["hits"])["total"]
                )
                require(
                    count["value"] == len(expected),
                    "Lexical evidence missing for " + field,
                )
            excluded: JsonObject = self.request(
                "POST",
                "/" + index + "/_search?request_cache=false",
                {"query": {"match": {"path": "namespace_outside_scope"}}},
            )
            require(
                requireObject(requireObject(excluded["hits"])["total"])["value"] == 0,
                "Unexpected cross-namespace lexical hit",
            )

    def verifyIndexMetadata(
        self, indexes: list[str], *, isSnapshotTarget: bool = False
    ) -> None:
        for index in indexes:
            settings: JsonObject = requireObject(
                requireObject(
                    self.request("GET", "/" + index + "/_settings?flat_settings=true")[
                        index
                    ]
                )["settings"]
            )
            require(
                settings.get("index.translog.durability") == "request",
                "Restored durability differs",
            )
            require(
                settings.get("index.replication.type") == "SEGMENT",
                "Restored replication differs",
            )
            require(
                settings.get("index.remote_store.enabled") == "true",
                "Restored remote store is disabled",
            )
            require(
                str(settings.get("index.similarity.knowhere_bm25.k1")) == "1.5",
                "Restored k1 differs",
            )
            require(
                str(settings.get("index.similarity.knowhere_bm25.b")) == "0.75",
                "Restored b differs",
            )
            expectedRepository: str = (
                "fresh-" if isSnapshotTarget else "compat-"
            ) + "segment"
            require(
                settings.get("index.remote_store.segment.repository")
                == expectedRepository,
                "Wrong restored segment repository",
            )
            mapping: JsonObject = requireObject(
                requireObject(self.request("GET", "/" + index + "/_mapping")[index])[
                    "mappings"
                ]
            )
            properties: JsonObject = requireObject(mapping["properties"])
            for field in ["path", "content"]:
                definition: JsonObject = requireObject(properties[field])
                require(
                    definition.get("analyzer") == "whitespace",
                    "Restored analyzer differs",
                )
                require(
                    definition.get("similarity") == "knowhere_bm25",
                    "Restored field similarity differs",
                )

    def registerBackups(self, *, isReadOnly: bool = False) -> None:
        self.request(
            "PUT",
            "/_snapshot/independent-full",
            {
                "type": "s3",
                "settings": {
                    "bucket": "backup",
                    "base_path": "full",
                    "region": "us-east-1",
                    "client": "backup",
                    "s3_async_client_type": "netty",
                    "readonly": isReadOnly,
                    "remote_store_index_shallow_copy": False,
                    "shallow_snapshot_v2": False,
                },
            },
        )

    def waitForShards(self, indexes: list[str]) -> None:
        deadline: float = time.monotonic() + 150
        while time.monotonic() < deadline:
            result: JsonObject = self.request(
                "GET",
                "/_cluster/health/"
                + ",".join(indexes)
                + "?wait_for_status=green&timeout=3s",
                timeout=8,
            )
            if result.get("status") == "green" and result.get("timed_out") is False:
                return
            time.sleep(0.5)
        raise AssertionError("Business namespace shards did not recover")

    def runCases(self) -> None:
        original: JsonObject = self.startNode("source")
        ledger: DocumentLedger = self.seed()
        self.verifyIndexMetadata(list(ledger))
        self.recordCase(
            "remote-backed writes and lexical visibility",
            {"indexes": list(ledger), "documents": 24, "buildHash": SOURCE_COMMIT},
        )
        self.request("POST", "/compat-namespace-b/_close")
        closed: JsonObject = self.request(
            "POST",
            "/compat-namespace-b/_search",
            {"query": {"match_all": {}}},
            expected=(400,),
        )
        require(
            "index_closed_exception" in json.dumps(closed),
            "Closed index did not block search",
        )
        self.request("POST", "/compat-namespace-b/_open?wait_for_active_shards=1")
        self.verifyLedger(ledger)
        self.request("POST", "/compat-namespace-b/_close")
        self.recordCase(
            "remote-backed close/open and search",
            {"closedReadBlocked": True, "reopenedContentVerified": True},
        )
        self.registerBackups()
        snapshot: JsonObject = requireObject(
            self.request(
                "PUT",
                "/_snapshot/independent-full/before-loss?wait_for_completion=true",
                {
                    "indices": list(ledger),
                    "include_global_state": True,
                    "ignore_unavailable": False,
                    "partial": False,
                },
                timeout=150,
            )["snapshot"]
        )
        require(snapshot["state"] == "SUCCESS", "Full snapshot did not succeed")
        require(
            set(cast(list[str], snapshot["indices"])) == set(ledger),
            "Full snapshot omitted a closed namespace",
        )
        require(
            requireObject(snapshot["shards"])["failed"] == 0,
            "Full snapshot has failed shards",
        )
        snapshotLedger: DocumentLedger = cast(
            DocumentLedger, json.loads(json.dumps(ledger))
        )
        self.recordCase(
            "independent full snapshot includes a closed namespace", snapshot
        )
        self.request(
            "PUT", "/compat-namespace-a/_settings", {"index.refresh_interval": "-1"}
        )
        before: JsonObject = self.request(
            "GET", "/compat-namespace-a/_stats/refresh,flush"
        )
        operations: list[JsonObject] = []
        for number in range(4):
            identifier: str = "tail-" + str(number)
            document: JsonObject = {
                "path": "namespace_a chapter",
                "content": "shared tailmarker",
                "revision": "revision-2",
            }
            ledger["compat-namespace-a"][identifier] = document
            operations.extend(
                [
                    {"index": {"_index": "compat-namespace-a", "_id": identifier}},
                    document,
                ]
            )
        replacement: JsonObject = {
            "path": "namespace_a chapter",
            "content": "shared updatedmarker",
            "revision": "revision-2",
        }
        ledger["compat-namespace-a"]["unit-0"] = replacement
        del ledger["compat-namespace-a"]["unit-1"]
        operations.extend(
            [
                {"index": {"_index": "compat-namespace-a", "_id": "unit-0"}},
                replacement,
                {"delete": {"_index": "compat-namespace-a", "_id": "unit-1"}},
            ]
        )
        acknowledged: JsonObject = self.bulk(operations)
        after: JsonObject = self.request(
            "GET", "/compat-namespace-a/_stats/refresh,flush"
        )
        for field in ["refresh", "flush"]:
            first: JsonObject = requireObject(
                requireObject(requireObject(before["_all"])["primaries"])[field]
            )
            last: JsonObject = requireObject(
                requireObject(requireObject(after["_all"])["primaries"])[field]
            )
            require(
                first["total"] == last["total"],
                "Tail was unexpectedly refreshed or flushed",
            )
        writeJson(
            self.directory / "acknowledged-tail.json",
            {
                "bulk": acknowledged,
                "before": before,
                "after": after,
                "expectedLedger": ledger,
            },
        )
        self.discardNode()
        recovered: JsonObject = self.startNode("replacement")
        require(
            recovered["cluster_uuid"] != original["cluster_uuid"],
            "Replacement reused local cluster metadata",
        )
        metadata: JsonObject = requireObject(
            self.request("GET", "/_cluster/state/metadata")["metadata"]
        )
        indexes: JsonObject = requireObject(metadata["indices"])
        require(set(ledger) <= set(indexes), "Remote state omitted namespace metadata")
        require(
            requireObject(indexes["compat-namespace-b"])["state"] == "close",
            "Closed namespace state was lost",
        )
        self.request(
            "POST",
            "/_remotestore/_restore",
            {"indices": [name for name in ledger if name != "compat-namespace-b"]},
        )
        self.request(
            "POST", "/compat-namespace-b/_open?timeout=3s&wait_for_active_shards=0"
        )
        self.request(
            "POST", "/_remotestore/_restore", {"indices": ["compat-namespace-b"]}
        )
        self.waitForShards(list(ledger))
        self.verifyIndexMetadata(list(ledger))
        self.verifyLedger(ledger)
        self.request("GET", "/_alias/compat-alias-a")
        self.recordCase(
            "only-node local-data loss recovers latest acknowledged state",
            {
                "originalClusterUuid": original["cluster_uuid"],
                "replacementClusterUuid": recovered["cluster_uuid"],
                "documentCounts": {name: len(rows) for name, rows in ledger.items()},
                "tailCreated": 4,
                "tailUpdated": 1,
                "tailDeleted": 1,
                "closedMetadataRestored": True,
            },
        )
        self.request(
            "PUT", "/compat-namespace-a/_settings", {"index.refresh_interval": "1s"}
        )
        self.request(
            "PUT",
            "/compat-namespace-a/_doc/after-recovery?refresh=wait_for",
            {
                "path": "namespace_a chapter",
                "content": "shared after_recovery",
                "revision": "revision-3",
            },
        )
        visible: JsonObject = self.request(
            "POST",
            "/compat-namespace-a/_search?request_cache=false",
            {"query": {"match": {"content": "after_recovery"}}},
        )
        require(
            requireObject(requireObject(visible["hits"])["total"])["value"] == 1,
            "Post-recovery write is not visible",
        )
        self.recordCase(
            "post-remote-recovery write reaches visibility",
            {"acknowledgedWithVisibilityWait": True},
        )
        self.discardNode()
        self.runDocker(["rm", "-f", self.prefix + "-live"])
        isLiveUnavailable: bool = False
        try:
            self.request("GET", "/", port=19372, timeout=2)
        except (URLError, TimeoutError, ConnectionError):
            isLiveUnavailable = True
        require(isLiveUnavailable, "Source live repository still reachable")
        self.startStore("target", 19374, "fresh")
        self.startNode("independent", isSnapshotTarget=True)
        initialMetadata: JsonObject = requireObject(
            self.request("GET", "/_cluster/state/metadata")["metadata"]
        )
        require(
            not set(snapshotLedger).intersection(
                requireObject(initialMetadata["indices"])
            ),
            "Fresh target already contains source metadata",
        )
        self.registerBackups(isReadOnly=True)
        self.request(
            "POST",
            "/_snapshot/independent-full/before-loss/_restore?wait_for_completion=true",
            {
                "indices": ",".join(snapshotLedger),
                "include_global_state": False,
                "include_aliases": True,
                "partial": False,
                "index_settings": {"index.number_of_replicas": 0},
            },
            timeout=150,
        )
        self.waitForShards(list(snapshotLedger))
        self.verifyIndexMetadata(list(snapshotLedger), isSnapshotTarget=True)
        self.verifyLedger(snapshotLedger)
        self.request("GET", "/_alias/compat-alias-a")
        self.recordCase(
            "full snapshot restores without source live repositories",
            {
                "sourceLiveRepositoryUnavailable": True,
                "freshTargetRemoteStore": True,
                "documents": 24,
                "closedNamespaceIncluded": True,
                "postSnapshotChangesAbsentAsExpected": True,
            },
        )
        self.request(
            "PUT",
            "/compat-namespace-a/_doc/after-snapshot?refresh=wait_for",
            {
                "path": "namespace_a chapter",
                "content": "shared after_snapshot",
                "revision": "revision-4",
            },
        )
        visible = self.request(
            "POST",
            "/compat-namespace-a/_search?request_cache=false",
            {"query": {"match": {"content": "after_snapshot"}}},
        )
        require(
            requireObject(requireObject(visible["hits"])["total"])["value"] == 1,
            "Post-snapshot write is not visible",
        )
        self.recordCase(
            "post-snapshot-recovery write reaches visibility",
            {"acknowledgedWithVisibilityWait": True},
        )

    def cleanup(self) -> None:
        for name in reversed(self.containers):
            require(name.startswith(self.prefix), "Refusing unrelated cleanup")
            logs: subprocess.CompletedProcess[str] = subprocess.run(
                DOCKER_PREFIX + ["logs", name],
                text=True,
                capture_output=True,
                check=False,
            )
            if logs.returncode == 0:
                (self.directory / (name + ".log")).write_text(logs.stdout + logs.stderr)
            self.runDocker(["rm", "-f", name], check=False)
        for name in self.volumes:
            self.runDocker(["volume", "rm", name], check=False)
        self.runDocker(["network", "rm", self.network], check=False)
        self.runDocker(["image", "rm", self.image], check=False)
        after: dict[str, JsonObject] = self.captureContainers()
        unchanged: bool = all(
            after.get(identifier) == state
            for identifier, state in self.baseline.items()
        )
        remainingContainers: str = self.runDocker(
            [
                "ps",
                "-a",
                "--filter",
                "label=knowhere.compatibility=" + self.prefix,
                "--format",
                "{{.Names}}",
            ]
        )
        remainingVolumes: str = self.runDocker(
            ["volume", "ls", "--filter", "name=" + self.prefix, "--format", "{{.Name}}"]
        )
        remainingNetworks: str = self.runDocker(
            [
                "network",
                "ls",
                "--filter",
                "name=" + self.prefix,
                "--format",
                "{{.Name}}",
            ]
        )
        self.report["cleanup"] = {
            "baselineContainersUnchanged": unchanged,
            "remainingContainers": remainingContainers,
            "remainingVolumes": remainingVolumes,
            "remainingNetworks": remainingNetworks,
            "sharedVendorImageCacheRetained": True,
        }
        if (
            not unchanged
            or remainingContainers
            or remainingVolumes
            or remainingNetworks
        ):
            self.report["status"] = "cleanup_failed"
        self.report["finishedAt"] = datetime.now(UTC).isoformat()
        self.save()
        require(
            unchanged
            and not remainingContainers
            and not remainingVolumes
            and not remainingNetworks,
            "Cleanup or preservation check failed",
        )


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--benchmark-protocol", type=Path, required=True)
    arguments: argparse.Namespace = parser.parse_args()
    require(
        platform.system() == "Linux" and platform.machine() in {"aarch64", "arm64"},
        "Run only on the approved ARM64 Linux host, never the Mac",
    )
    require(
        socket.gethostname() == APPROVED_HOSTNAME,
        "Run only on the approved Oracle host",
    )
    directory: Path = arguments.work_directory.resolve()
    require(
        directory.parent == Path.home()
        and directory.name.startswith("knowhere-compatibility-"),
        "Use an isolated directory directly under the remote home",
    )
    protocol: JsonObject = requireObject(
        json.loads(arguments.benchmark_protocol.read_text())
    )
    require(
        protocol.get("status") == "measured",
        "Existing comparison is still running; do not interfere",
    )
    directory.mkdir(exist_ok=True)
    probe: CompatibilityProbe = CompatibilityProbe(directory)
    probe.save()
    try:
        probe.prepare()
        probe.report["status"] = "running"
        probe.save()
        probe.runCases()
        probe.report["status"] = "passed"
    except Exception as err:
        probe.report["status"] = "failed"
        probe.report["error"] = {"type": type(err).__name__, "message": str(err)}
        raise
    finally:
        probe.cleanup()


if __name__ == "__main__":
    main()
