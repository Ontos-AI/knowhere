"""Review/install an approved bundle and operate its private native backups.

All native/service mutations default to a printed dry-run. Cloud resources are
never provisioned by this command. Supply a manifest-bound explicit approval
receipt and --execute for an approved installation or snapshot operation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256, sha512
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import cast

import httpx

from operations import OpenSearchOperations


def _readJson(path: Path) -> object:
    return json.loads(path.read_bytes())


def _requireObject(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, object], value)


def _writeJson(path: Path, value: object, *, replace: bool = False) -> None:
    if not replace:
        descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temporaryDescriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".pending", dir=path.parent)
        temporary: Path = Path(name)
        try:
            with os.fdopen(temporaryDescriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    directoryDescriptor: int = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directoryDescriptor)
    finally:
        os.close(directoryDescriptor)


def _emit(value: object, output: Path | None) -> None:
    if output is None:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        _writeJson(output, value)


def _validateApproval(path: Path | None, *, manifestSha256: str, purpose: str) -> None:
    if path is None:
        raise ValueError("Execution requires an explicit manifest-bound approval receipt")
    receipt: dict[str, object] = _requireObject(_readJson(path))
    expires: datetime = datetime.fromisoformat(str(receipt.get("expires_at", "")))
    if (receipt.get("manifest_sha256") != manifestSha256 or receipt.get("approved") is not True
            or purpose not in cast(list[str], receipt.get("purposes", []))
            or not receipt.get("user_approval_reference") or not receipt.get("bounded_cost_plan_sha256")
            or expires.tzinfo is None or expires <= datetime.now(timezone.utc)):
        raise ValueError("Approval does not cover this exact resource manifest, purpose and bounded cost window")


def _validateBundle(directory: Path) -> dict[str, object]:
    manifest: dict[str, object] = _requireObject(_readJson(directory / "bundle-manifest.json"))
    files: dict[str, str] = cast(dict[str, str], manifest["files"])
    for name, digest in files.items():
        path: Path = (directory / name).resolve()
        if path.parent != directory.resolve() or sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Bundle has changed since its immutable receipt was written")
    return manifest


def _render(manifestPath: Path, directory: Path) -> None:
    bundle: dict[str, object] = OpenSearchOperations.renderBundle(_readJson(manifestPath))
    directory.mkdir(mode=0o700)
    files: dict[str, str] = {}
    for name, payload in bundle.items():
        path: Path = directory / name
        if isinstance(payload, str):
            path.write_text(payload)
            path.chmod(0o600)
        else:
            _writeJson(path, payload)
        files[name] = sha256(path.read_bytes()).hexdigest()
    receipt: dict[str, object] = _requireObject(bundle["installation-receipt.json"])
    _writeJson(directory / "bundle-manifest.json", {
        "schema_version": "opensearch-rendered-bundle-v1", "manifest_sha256": receipt["manifest_sha256"],
        "files": files, "executed": False,
    })
    print(json.dumps({"directory": str(directory.resolve()), "executed": False, "files": files}))


def _buildInstallPlan(arguments: argparse.Namespace) -> dict[str, object]:
    bundle: Path = arguments.bundle.resolve()
    manifest: dict[str, object] = _validateBundle(bundle)
    receipt: dict[str, object] = _requireObject(_readJson(bundle / "installation-receipt.json"))
    return {"schema_version": "opensearch-host-install-plan-v1", "mode": "execute" if arguments.execute else "dry-run",
        "manifest_sha256": manifest["manifest_sha256"], "bundle": str(bundle),
        "archive_path": receipt["archive_path"], "archive_sha512": receipt["archive_sha512"],
        "plugin_path": receipt["plugin_path"], "plugin_sha512": receipt["plugin_sha512"],
        "data_device": arguments.data_device, "data_volume_id": arguments.data_volume_id,
        "tls_directory": str(arguments.tls_directory.resolve()),
        "configuration_kind": "independent-full-restore-drill" if arguments.full_restore else "normal-durable-node",
        "formats_volume": False, "starts_service": False,
        "requires": ["root-on-approved-ARM64-host", "separately-approved-mounted-data-volume", "immutable-artifacts-and-TLS-files", "explicit-manifest-bound-install-approval"],
        "actions": ["verify-artifact-checksums", "verify-data-volume-serial-and-filesystem", "mount-existing-data-filesystem", "extract-pinned-archive", "install-pinned-repository-s3-plugin", "install-security-config-and-node-TLS", "install-and-enable-systemd-supervision"],
        "remaining_manual_commands": [
            ["systemctl", "start", "knowhere-opensearch"],
            ["/opt/knowhere-opensearch/plugins/opensearch-security/tools/securityadmin.sh", "-cd", "/etc/knowhere-opensearch/opensearch-security", "-h", "APPROVED_PRIVATE_NODE_DNS", "-cacert", "ADMIN_CA_PATH", "-cert", "ADMIN_CERT_PATH", "-key", "ADMIN_KEY_PATH"],
            ["uv", "run", "--offline", "--frozen", "python", "deploy/opensearch/manage.py", "bootstrap", "--manifest", "APPROVED_MANIFEST_PATH", "--approval", "APPROVED_RECEIPT_PATH", "--execute"],
        ],
    }


def _install(plan: dict[str, object], approval: Path | None) -> None:
    _validateApproval(approval, manifestSha256=str(plan["manifest_sha256"]), purpose="install")
    if os.geteuid() != 0 or os.uname().machine not in ("aarch64", "arm64"):
        raise ValueError("Execute installation only as root on the approved ARM64 node")
    device: Path = Path(str(plan["data_device"])).resolve(strict=True)
    if not stat.S_ISBLK(device.stat().st_mode):
        raise ValueError("Installation requires the approved existing data block device")
    volumeId: str = str(plan.get("data_volume_id") or "")
    serial: str = subprocess.run(["lsblk", "-ndo", "SERIAL", str(device)], check=True, capture_output=True, text=True).stdout.strip()
    if not volumeId.startswith("vol-") or serial.replace("-", "") != volumeId.replace("-", ""):
        raise ValueError("Data device serial does not match the approved retained EBS volume ID")
    filesystem: str = subprocess.run(["blkid", "-s", "TYPE", "-o", "value", str(device)], check=True, capture_output=True, text=True).stdout.strip()
    if filesystem not in ("ext4", "xfs"):
        raise ValueError("Create the approved data filesystem separately; this installer never formats a volume")
    mounted = subprocess.run(["findmnt", "-n", "-o", "TARGET", "-S", str(device)], check=False, capture_output=True, text=True)
    mountPath: Path = Path("/var/lib/knowhere-opensearch")
    if mounted.returncode == 0 and mounted.stdout.strip() != str(mountPath):
        raise ValueError("Approved data device is already mounted elsewhere")
    if mounted.returncode not in (0, 1):
        raise ValueError("Cannot safely resolve the approved data device mount")
    filesystemId: str = subprocess.run(["blkid", "-s", "UUID", "-o", "value", str(device)],
        check=True, capture_output=True, text=True).stdout.strip()
    currentMount = subprocess.run(["findmnt", "-n", "-o", "UUID", "--mountpoint", str(mountPath)],
        check=False, capture_output=True, text=True)
    if currentMount.returncode not in (0, 1) or (currentMount.returncode == 0 and (
            not filesystemId or currentMount.stdout.strip() != filesystemId or mounted.returncode != 0)):
        raise ValueError("Data mount point is occupied by a different or unverifiable filesystem")
    target: Path = Path("/opt/knowhere-opensearch")
    if target.exists():
        old: dict[str, object] = _requireObject(_readJson(target / "knowhere-installation.json"))
        if old.get("archive_sha512") != plan["archive_sha512"]:
            raise ValueError("Existing installation uses another artifact; use a reviewed upgrade procedure")
        service = subprocess.run(["systemctl", "show", "knowhere-opensearch.service", "-p", "ActiveState", "--value"],
            check=True, capture_output=True, text=True)
        if service.stdout.strip() not in ("inactive", "failed"):
            raise ValueError("Installation must not alter a live native process")
    for artifact in ("archive", "plugin"):
        path: Path = Path(str(plan[artifact + "_path"]))
        digest = sha512()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != plan[artifact + "_sha512"]:
            raise ValueError("Pinned installation artifact checksum does not match")
    tls: Path = Path(str(plan["tls_directory"]))
    for name in ("ca.pem", "node.pem", "node-key.pem"):
        if not (tls / name).is_file():
            raise ValueError("Installation requires approved CA/node certificates and key")
    try:
        pwd.getpwnam("opensearch")
    except KeyError:
        subprocess.run(["useradd", "--system", "--home-dir", str(mountPath), "--shell", "/usr/sbin/nologin", "opensearch"], check=True)
    mountPath.mkdir(exist_ok=True)
    if mounted.returncode != 0:
        subprocess.run(["mount", str(device), str(mountPath)], check=True)
    mountUnit: str = subprocess.run(["systemd-escape", "--path", "--suffix=mount", str(mountPath)], check=True, capture_output=True, text=True).stdout.strip()
    Path("/etc/systemd/system", mountUnit).write_text("\n".join([
        "[Unit]", "Description=Retained Knowhere native data volume", "Before=knowhere-opensearch.service", "",
        "[Mount]", f"What=UUID={filesystemId}", f"Where={mountPath}", f"Type={filesystem}", "Options=defaults,nodev,nosuid", "",
        "[Install]", "WantedBy=multi-user.target", "",
    ]))
    if not target.exists():
        with tarfile.open(str(plan["archive_path"]), "r:gz") as archive:
            members: list[tarfile.TarInfo] = archive.getmembers()
            roots: set[str] = {Path(member.name).parts[0] for member in members}
            if roots != {"opensearch-3.9.0"}:
                raise ValueError("Pinned archive does not contain the expected version root")
            archive.extractall("/opt", filter="data")
        Path("/opt/opensearch-3.9.0").rename(target)
        _writeJson(target / "knowhere-installation.json", plan)
    plugins = subprocess.run([str(target / "bin/opensearch-plugin"), "list"], check=True, capture_output=True, text=True)
    if "repository-s3" not in plugins.stdout.splitlines():
        subprocess.run([str(target / "bin/opensearch-plugin"), "install", "--batch", Path(str(plan["plugin_path"])).as_uri()], check=True)
    configuration: Path = Path("/etc/knowhere-opensearch")
    (configuration / "tls").mkdir(parents=True, exist_ok=True, mode=0o750)
    (configuration / "opensearch-security").mkdir(exist_ok=True, mode=0o750)
    bundle: Path = Path(str(plan["bundle"]))
    for original in (target / "config/opensearch-security").iterdir():
        if original.is_file():
            shutil.copyfile(original, configuration / "opensearch-security" / original.name)
    shutil.copyfile(bundle / ("full-restore-opensearch.yml" if plan["configuration_kind"] == "independent-full-restore-drill" else "opensearch.yml"), configuration / "opensearch.yml")
    for name in ("config.yml", "roles.yml", "roles_mapping.yml"):
        sourceName: str = "full-restore-" + name if plan["configuration_kind"] == "independent-full-restore-drill" and name != "config.yml" else name
        shutil.copyfile(bundle / sourceName, configuration / "opensearch-security" / name)
    # Retain required distribution security/JVM/logging defaults rather than inventing them.
    for original in (target / "config").iterdir():
        destination: Path = configuration / original.name
        if original.is_file() and not destination.exists():
            shutil.copyfile(original, destination)
    for name in ("ca.pem", "node.pem", "node-key.pem"):
        destination = configuration / "tls" / name
        shutil.copyfile(tls / name, destination)
        destination.chmod(0o600 if name.endswith("key.pem") else 0o640)
    Path("/var/log/knowhere-opensearch").mkdir(exist_ok=True)
    subprocess.run(["chown", "-R", "opensearch:opensearch", str(configuration), str(mountPath), "/var/log/knowhere-opensearch"], check=True)
    shutil.copyfile(bundle / "knowhere-opensearch.service", "/etc/systemd/system/knowhere-opensearch.service")
    subprocess.run(["sysctl", "-w", "vm.max_map_count=262144"], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "knowhere-opensearch"], check=True)
    subprocess.run(["systemctl", "enable", mountUnit], check=True)


def _nativeClient(manifest: dict[str, object]) -> httpx.Client:
    from shared.services.retrieval.lexical.clientconfig import NativeClientConfig

    endpoint: str = f"https://{manifest['endpoint_host']}:9200"
    configuration: NativeClientConfig = NativeClientConfig.resolve(endpoint)
    return httpx.Client(base_url=endpoint, auth=configuration.auth, timeout=10, trust_env=False,
        transport=httpx.HTTPTransport(verify=configuration.verify, retries=0, trust_env=False))


def _request(client: httpx.Client, method: str, path: str, body: object | None = None) -> object:
    response: httpx.Response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def _parseArguments() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    render.add_argument("--manifest", required=True, type=Path)
    render.add_argument("--directory", required=True, type=Path)
    install = commands.add_parser("install")
    install.add_argument("--bundle", required=True, type=Path)
    install.add_argument("--data-device", required=True)
    install.add_argument("--data-volume-id")
    install.add_argument("--tls-directory", required=True, type=Path)
    install.add_argument("--execute", action="store_true")
    install.add_argument("--approval", type=Path)
    install.add_argument("--output", type=Path)
    install.add_argument("--full-restore", action="store_true")
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--manifest", required=True, type=Path)
    preflight.add_argument("--proofs", required=True, type=Path)
    cost = commands.add_parser("cost")
    cost.add_argument("--quote", required=True, type=Path)
    cost.add_argument("--usage", required=True, type=Path)
    snapshot = commands.add_parser("snapshot-submit")
    snapshot.add_argument("--manifest", required=True, type=Path)
    snapshot.add_argument("--inventory", required=True, type=Path)
    snapshot.add_argument("--boundary", required=True, type=Path)
    snapshot.add_argument("--snapshot", required=True)
    snapshot.add_argument("--execute", action="store_true")
    snapshot.add_argument("--approval", type=Path)
    cycle = commands.add_parser("backup-cycle")
    cycle.add_argument("--manifest", required=True, type=Path)
    cycle.add_argument("--inventory", required=True, type=Path)
    cycle.add_argument("--boundary", required=True, type=Path)
    cycle.add_argument("--verified-boundary", type=Path)
    cycle.add_argument("--state", required=True, type=Path)
    cycle.add_argument("--execute", action="store_true")
    cycle.add_argument("--approval", type=Path)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--manifest", required=True, type=Path)
    bootstrap.add_argument("--execute", action="store_true")
    bootstrap.add_argument("--approval", type=Path)
    bootstrap.add_argument("--full-restore", action="store_true")
    verify = commands.add_parser("snapshot-verify")
    verify.add_argument("--inventory", required=True, type=Path)
    verify.add_argument("--observed-inventory", required=True, type=Path)
    verify.add_argument("--snapshot-response", required=True, type=Path)
    verify.add_argument("--shard-status", required=True, type=Path)
    verify.add_argument("--boundary", required=True, type=Path)
    verify.add_argument("--verified-boundary", type=Path)
    retention = commands.add_parser("retention")
    retention.add_argument("--points", required=True, type=Path)
    restore = commands.add_parser("restore-plan")
    restore.add_argument("--indices", required=True, type=Path)
    for name in ("open", "reserved", "uncertain"):
        restore.add_argument("--" + name, required=True, type=int)
    restore.add_argument("--batch-limit", required=True, type=int)
    recovery = commands.add_parser("recovery-plan")
    recovery.add_argument("--route", required=True, choices=("process", "healthy-volume", "live-remote", "full-snapshot"))
    recovery.add_argument("--fence", required=True, type=Path)
    recovery.add_argument("--required-boundary", required=True)
    recovery.add_argument("--restored-boundary")
    alerts = commands.add_parser("alerts")
    alerts.add_argument("--metrics", required=True, type=Path)
    alerts.add_argument("--state", required=True, type=Path)
    alerts.add_argument("--repeat-seconds", required=True, type=int)
    release = commands.add_parser("release-plan")
    release.add_argument("--first-impact", required=True)
    release.add_argument("--restoration-seconds", required=True, type=int)
    release.add_argument("--margin-seconds", required=True, type=int)
    for command in (preflight, cost, snapshot, cycle, bootstrap, verify, retention, restore, recovery, alerts, release):
        command.add_argument("--output", type=Path)
    return parser.parse_args()


def _main() -> int:
    arguments: argparse.Namespace = _parseArguments()
    command: str = arguments.command
    if command == "render":
        _render(arguments.manifest, arguments.directory)
        return 0
    result: dict[str, object]
    if command == "install":
        result = _buildInstallPlan(arguments)
        if arguments.execute:
            _install(result, arguments.approval)
    elif command == "preflight":
        result = OpenSearchOperations.preflight(_readJson(arguments.manifest), proofs=_requireObject(_readJson(arguments.proofs)))
    elif command == "cost":
        result = OpenSearchOperations.planCost(_readJson(arguments.quote), _readJson(arguments.usage))
    elif command == "backup-cycle":
        if arguments.execute:
            raise ValueError("Run the authoritative operator.py backup command to snapshot and qualify data")
        inventory: object = _readJson(arguments.inventory)
        boundaryDigest: str = sha256(arguments.boundary.read_bytes()).hexdigest()
        result = {"mode": "dry-run", "data_verified": False, "retention_authorized": False,
            "request_plan": OpenSearchOperations.planSnapshot(inventory, boundarySha256=boundaryDigest, snapshotName="hourly-next-approved-cycle")}
    elif command in ("snapshot-submit", "bootstrap"):
        manifest: dict[str, object] = _requireObject(_readJson(arguments.manifest))
        bundle: dict[str, object] = OpenSearchOperations.renderBundle(manifest)
        receipt: dict[str, object] = _requireObject(bundle["installation-receipt.json"])
        if command == "snapshot-submit":
            inventory: object = _readJson(arguments.inventory)
            result = OpenSearchOperations.planSnapshot(inventory, boundarySha256=sha256(arguments.boundary.read_bytes()).hexdigest(), snapshotName=arguments.snapshot)
        else:
            result = {"requests": [
                *([] if arguments.full_restore else [{"method": "PUT", "path": "/_index_template/knowhere-durable", "body": bundle["index-template.json"]}]),
                {"method": "PUT", "path": "/_snapshot/knowhere-full", "body": bundle["full-restore-repository.json"] if arguments.full_restore else bundle["snapshot-repository.json"]},
                *[{"method": "POST", "path": f"/_snapshot/{repository}/_verify"} for repository in (("knowhere-full",) if arguments.full_restore else ("knowhere-segments", "knowhere-translogs", "knowhere-state", "knowhere-full"))],
            ]}
        result["mode"] = "execute" if arguments.execute else "dry-run"
        result["manifest_sha256"] = receipt["manifest_sha256"]
        if arguments.execute:
            if command == "snapshot-submit":
                raise ValueError("Execute snapshots through the authoritative physical operator backup workflow")
            _validateApproval(arguments.approval, manifestSha256=str(receipt["manifest_sha256"]), purpose="bootstrap")
            if arguments.output is None or arguments.output.exists():
                raise ValueError("Native bootstrap requires a new private output receipt path")
            with _nativeClient(manifest) as client:
                result["responses"] = [_request(client, str(request["method"]), str(request["path"]), request.get("body"))
                    for request in cast(list[dict[str, object]], result["requests"])]
    elif command == "snapshot-verify":
        result = OpenSearchOperations.verifySnapshot(_readJson(arguments.inventory), _requireObject(_readJson(arguments.snapshot_response)),
            observedInventory=_readJson(arguments.observed_inventory), boundarySha256=sha256(arguments.boundary.read_bytes()).hexdigest(),
            verifiedBoundarySha256=None, shardStatus=_readJson(arguments.shard_status), verifiedAt=int(datetime.now(timezone.utc).timestamp() * 1000),
            boundaryCapturedAtMillis=cast(int | None, _requireObject(_readJson(arguments.boundary)).get("captured_at_millis")))
        result.update({"mode": "catalog-review", "data_verified": False, "retention_authorized": False})
    elif command == "retention":
        result = OpenSearchOperations.planRetention(cast(list[object], _readJson(arguments.points)), nowMillis=int(datetime.now(timezone.utc).timestamp() * 1000))
    elif command == "restore-plan":
        result = OpenSearchOperations.planRestore(cast(list[str], _readJson(arguments.indices)), openCount=arguments.open, reservedCount=arguments.reserved, uncertainCount=arguments.uncertain, batchLimit=arguments.batch_limit)
    elif command == "recovery-plan":
        result = OpenSearchOperations.planRecovery(arguments.route, _readJson(arguments.fence), requiredBoundary=arguments.required_boundary, restoredBoundary=arguments.restored_boundary)
    elif command == "alerts":
        previous: dict[str, object] = _requireObject(_readJson(arguments.state)) if arguments.state.exists() else {}
        result = OpenSearchOperations.evaluateAlerts(_readJson(arguments.metrics), previous, nowSeconds=int(datetime.now(timezone.utc).timestamp()), repeatSeconds=arguments.repeat_seconds)
        _writeJson(arguments.state, result["state"], replace=arguments.state.exists())
    else:
        result = OpenSearchOperations.planRelease(datetime.fromisoformat(arguments.first_impact), restorationSeconds=arguments.restoration_seconds, marginSeconds=arguments.margin_seconds)
    _emit(result, arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError, subprocess.CalledProcessError, httpx.HTTPError):
        # Do not echo URLs, secret paths, response bodies or command payloads.
        print("OpenSearch operation refused or failed; check the supplied manifest, prerequisites and private receipts.", file=sys.stderr)
        raise SystemExit(2) from None
