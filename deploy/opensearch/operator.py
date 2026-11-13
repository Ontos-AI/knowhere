"""Run reviewed backup, existing-host recovery, release and manifest seeding.

Dry-run is the default. Execution requires a bound plan, cost/operation approval,
one physical issuer systemd unit, an existing maintenance DB role and actual
resource identities. No command creates a cloud resource or sends notifications.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import ssl
import subprocess
import sys
import time
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

import httpx

from authority import OperatorAuthority
from backup import BackupOperations
from backup_workflow import BackupWorkflow, _Source
from execution import OperationExecution, _Issuer
from host_recovery import HostRecovery, _EC2, _SSM
from manage import _nativeClient, _readJson, _requireObject, _validateApproval, _writeJson
from offline_backup import OfflineBackupVerification, _Admission
from operations import OpenSearchOperations
from release import ReleaseOperations
from release_bridge import ReleaseBridge, _ECS, _ELB

_ROOT: Path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "packages/shared-python"))


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cloud(plan: dict[str, object], manifest: dict[str, object], token: str) -> Callable[[str], object]:
    from boto3.session import Session
    from botocore.config import Config

    region: str = str(manifest["region"])
    configuration = Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1, "mode": "standard"},
        user_agent_extra="knowhere-native-issuer/" + UUID(token).hex)
    session = Session(region_name=region)
    account: str = str(session.client("sts", config=configuration).get_caller_identity()["Account"])
    if account != plan.get("account_id"):
        raise ValueError("Actual AWS caller account differs from the reviewed existing resources")

    def client(name: str) -> object:
        return session.client(name, config=configuration)

    return client


def _apiClient(endpoint: str, caPath: object = None) -> httpx.Client:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Use the exact reviewed HTTPS API origin without URL credentials")
    context: ssl.SSLContext = ssl.create_default_context(cafile=str(caPath)) if caPath else ssl.create_default_context()
    return httpx.Client(base_url=endpoint, verify=context, timeout=15, trust_env=False, follow_redirects=False)


def _guardClient(client: httpx.Client, authority: OperatorAuthority) -> None:
    def guard(request: httpx.Request) -> None:
        if request.method not in ("GET", "HEAD"):
            authority.beforeMutation()

    client.event_hooks["request"].append(guard)


def _getPlanOwner(planDirectory: Path, manifest: dict[str, object], plan: dict[str, object]) -> dict[str, object]:
    ownerPath: Path = planDirectory / str(plan["old_issuer_receipt_path"])
    raw: bytes = ownerPath.read_bytes()
    if sha256(raw).hexdigest() != plan["old_issuer_receipt_sha256"]:
        raise ValueError("Reviewed exact old issuer receipt changed")
    owner: dict[str, object] = _requireObject(json.loads(raw))
    identity: dict[str, object] = _requireObject(plan["old_issuer"])
    if (any(owner.get(key) != value for key, value in identity.items())
            or owner.get("logical_cluster_id") != plan.get("logical_cluster_id")
            or plan.get("logical_cluster_id") != str(manifest["region"]) + ":" + str(manifest["cluster_name"])):
        raise ValueError("SSM plan must bind the actual old issuer receipt")
    return owner


def _runFence(arguments: argparse.Namespace, manifest: dict[str, object], plan: dict[str, object],
        progress: dict[str, object], persist: Callable[[dict[str, object]], None]) -> dict[str, object]:
    from shared.core.database_sync import get_sync_session_factory
    from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority
    from shared.services.retrieval.lexical.transition import LexicalTransition

    if LexicalTransition.getSourceHash() != arguments.source_hash:
        raise ValueError("Physical fence command source differs from its reviewed identity")
    owner: dict[str, object] = _getPlanOwner(arguments.plan.parent, manifest, plan)
    identity: dict[str, object] = _requireObject(plan["old_issuer"])
    isReconciling: bool = arguments.operation == "reconcile-fence"
    token: str = str(UUID(arguments.fence_token if isReconciling else arguments.issuer_token))
    if token == owner["invocation_token"] or (isReconciling and arguments.issuer_token == token):
        raise ValueError("Physical fence, read-only reconciler and old issuer must be distinct invocations")
    service: LexicalOperatorAuthority = LexicalOperatorAuthority(sessionFactory=get_sync_session_factory())
    epoch: int = arguments.epoch
    cluster: str = str(identity["cluster_uuid"])
    sourceHash: str = arguments.source_hash

    def assertReserved() -> None:
        service.assertFenceCurrent(fenceToken=token, expectedSourceHash=sourceHash,
            expectedEpoch=epoch, expectedClusterUuid=cluster)

    def recordCommand(kind: str, commandId: str) -> None:
        commands: dict[str, object] = _requireObject(progress.get("ssm_commands", {}))
        progress["ssm_commands"] = {**commands, kind: str(UUID(commandId))}
        persist(progress)

    if isReconciling:
        if progress.get("fence_token") != token or progress.get("status") not in ("fence-intent", "fence-observed", "fenced"):
            raise ValueError("Read-only reconciliation requires the original frozen fence intent")
    else:
        if progress:
            raise ValueError("A prior fence intent can only be reconciled without resubmitting")
        progress.update({"status": "fence-intent", "fence_token": token})
        persist(progress)
        progress["authority"] = service.reserveFence(expectedOwnerReceipt=owner, fenceToken=token,
            expectedSourceHash=sourceHash, expectedEpoch=epoch, expectedClusterUuid=cluster)
        persist(progress)
    previous: object = progress.get("fence_receipt")
    if previous is not None:
        result: dict[str, object] = _requireObject(previous)
    else:
        assertReserved()
        cloud = _cloud(plan, manifest, arguments.issuer_token)
        ec2: _EC2 = cast(_EC2, cloud("ec2"))
        ssm: _SSM = cast(_SSM, cloud("ssm"))
        result = (HostRecovery.reconcileFence(plan, ec2=ec2, ssm=ssm, commandIdentity=token,
            commands=_requireObject(progress.get("ssm_commands", {}))) if isReconciling else HostRecovery.fence(plan,
            ec2=ec2, ssm=ssm, beforeMutation=assertReserved, commandIdentity=token, recordCommand=recordCommand))
        progress.update({"status": "fence-observed", "fence_receipt": result})
        persist(progress)
    progress["authority"] = service.proveFence(fenceToken=token, expectedSourceHash=sourceHash,
        expectedEpoch=epoch, expectedClusterUuid=cluster, fenceReceipt=result)
    progress["status"] = "fenced"
    persist(progress)
    return result


def _releaseEmptyShadow(arguments: argparse.Namespace, manifest: dict[str, object], plan: dict[str, object],
                        progress: dict[str, object], persist: Callable[[dict[str, object]], None]) -> dict[str, object]:
    from shared.core.config import settings
    from shared.core.database_sync import get_sync_session_factory
    from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority
    from shared.services.retrieval.lexical.transition import LexicalTransition

    if LexicalTransition.getSourceHash() != arguments.source_hash:
        raise ValueError("Empty shadow recovery source differs from its reviewed identity")
    if progress.get("status") == "released":
        raise ValueError("Empty shadow authority was already released; use its existing journal")
    owner: dict[str, object] = _getPlanOwner(arguments.plan.parent, manifest, plan)
    token: str = str(UUID(arguments.issuer_token))
    indexPrefix: str = str(manifest["index_prefix"])
    if (token == owner["invocation_token"] or owner["epoch"] != arguments.epoch
        or indexPrefix != settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX):
        raise ValueError("Empty shadow recovery requires a fresh invocation and exact configured epoch and prefix")
    cloud = _cloud(plan, manifest, token)
    stopped: dict[str, object] = HostRecovery.observeStoppedIssuer(plan, ec2=cast(_EC2, cloud("ec2")),
        ssm=cast(_SSM, cloud("ssm")), commandIdentity=token)
    progress.update({"status": "empty-shadow-observed", "stopped_issuer": stopped})
    persist(progress)
    service = LexicalOperatorAuthority(sessionFactory=get_sync_session_factory())
    with _nativeClient(manifest) as client:
        result: dict[str, object] = service.releaseEmptyShadow(fenceToken=token,
            expectedSourceHash=arguments.source_hash, expectedEpoch=arguments.epoch, indexPrefix=indexPrefix,
            stoppedReceipt=stopped, client=client)
    progress.update({"status": "released", "authority": result})
    persist(progress)
    return result


def _backup(arguments: argparse.Namespace, manifest: dict[str, object], plan: dict[str, object],
        authority: OperatorAuthority, progress: dict[str, object], persist: Callable[[dict[str, object]], None]) -> dict[str, object]:
    from shared.core.database_sync import get_sync_session_factory
    from shared.services.retrieval.lexical.backup_inventory import LexicalBackupInventory
    from shared.services.retrieval.lexical.clientconfig import NativeClientConfig
    from shared.services.retrieval.lexical.offline_admission import LexicalOfflineAdmission

    if plan.get("cleanup_verified_clones") is not True:
        raise ValueError("Recurring independent verification requires explicit owned verified-clone cleanup")
    denialPath: Path = arguments.plan.parent / str(plan.get("live_repository_denial_receipt_path", ""))
    if plan.get("offline_target_nonserving") is not True or not denialPath.is_file():
        raise ValueError("Use the reviewed dedicated offline target and actual live-repository denial proof")
    denialRaw: bytes = denialPath.read_bytes()
    denial: dict[str, object] = _requireObject(json.loads(denialRaw))
    if (sha256(denialRaw).hexdigest() != plan.get("live_repository_denial_receipt_sha256")
            or denial.get("mode") != "actual" or denial.get("status") != "denied"
            or denial.get("source_hash") != authority.sourceHash
            or denial.get("target_cluster_uuid") != plan.get("target_cluster_uuid")
            or denial.get("target_incarnation") != plan.get("target_incarnation")
            or denial.get("source_cluster_uuid") != authority.clusterUuid
            or denial.get("bucket") != manifest["live_bucket"]
            or denial.get("prefixes") != [manifest[key] for key in ("segment_prefix", "translog_prefix", "state_prefix")]
            or denial.get("checks") != [{"operation": operation, "prefix": manifest[key], "status": "denied"}
                for key in ("segment_prefix", "translog_prefix", "state_prefix") for operation in ("HeadObject", "ListObjectsV2")]):
        raise ValueError("Independent target live-repository denial evidence differs from this operation")
    endpoint: str = str(plan["target_endpoint"])
    native: NativeClientConfig = NativeClientConfig.resolve(endpoint, environmentPrefix="KNOWHERE_OFFLINE_OPENSEARCH_")
    sourceNative: NativeClientConfig = NativeClientConfig.resolve(f"https://{manifest['endpoint_host']}:9200")
    if endpoint != native.endpoint or not endpoint.startswith("https://") or sourceNative.identity == native.identity:
        raise ValueError("Independent payload verification requires a distinct private TLS service identity")

    def offlineClient(checkedEndpoint: str) -> httpx.Client:
        if checkedEndpoint != native.endpoint:
            raise ValueError("Independent verifier cannot read another native endpoint")
        return httpx.Client(base_url=native.endpoint, auth=native.auth, timeout=10, trust_env=False, follow_redirects=False,
            transport=httpx.HTTPTransport(verify=native.verify, retries=0, trust_env=False))

    factory = get_sync_session_factory()
    source: LexicalBackupInventory = LexicalBackupInventory(sessionFactory=factory, clientFactory=offlineClient)
    admission: _Admission = cast(_Admission, LexicalOfflineAdmission(sessionFactory=factory))
    backupState: dict[str, object] = _requireObject(progress.get("backup", {}))
    pending: object = backupState.get("pending")
    snapshotName: str = str(_requireObject(pending)["snapshot"]) if pending is not None else "hourly-" + str(int(datetime.now(timezone.utc).timestamp() * 1000))

    def saveBackup(state: dict[str, object]) -> None:
        progress["backup"] = state
        persist(progress)

    def saveOffline(state: dict[str, object]) -> None:
        progress["offline"] = state
        persist(progress)

    with _nativeClient(manifest) as sourceClient, offlineClient(endpoint) as targetClient:
        _guardClient(sourceClient, authority)
        _guardClient(targetClient, authority)

        def verifyIndependent(exported: dict[str, object], snapshot: dict[str, object]) -> dict[str, object]:
            snapshotUuid: str = str(snapshot["uuid"])

            def verifyPayload(mapping: dict[str, str], proof: dict[str, object], selected: list[str]) -> dict[str, object]:
                return source.verifyRestored(exported, endpoint=endpoint, indexMapping=mapping,
                    admissionProof=proof, expectedSnapshotUuid=snapshotUuid, selectedIndices=selected)

            offline: dict[str, object] = _requireObject(progress.get("offline", {}))
            if offline and offline.get("snapshot_uuid") != snapshotUuid:
                if offline.get("pending") is not None:
                    raise ValueError("Prior independent target operation remains uncertain")
                offline = {}
            return OfflineBackupVerification.run(targetClient, exported, snapshotName=snapshotName,
                snapshotUuid=snapshotUuid, targetClusterUuid=str(plan["target_cluster_uuid"]),
                targetIncarnation=str(plan["target_incarnation"]), operationId=str(uuid5(UUID(arguments.operation_id), snapshotUuid)),
                batchLimit=int(str(plan["batch_limit"])), timeoutSeconds=float(str(plan["restore_timeout_seconds"])),
                state=offline, verifyPayload=verifyPayload, admission=admission, readIssuer=authority.assertCurrent,
                cleanupClones=True, persist=saveOffline)

        result: dict[str, object] = BackupWorkflow.run(cast(_Source, source), sourceClient,
            indexPrefix=str(manifest["index_prefix"]), snapshotName=snapshotName, expectedEpoch=authority.epoch,
            expectedClusterUuid=authority.clusterUuid, expectedIncarnation=str(plan["source_incarnation"]),
            state=backupState, timeoutSeconds=float(str(plan["snapshot_poll_seconds"])),
            verifyIndependent=verifyIndependent, persist=saveBackup)
        if result.get("data_verified") is True:
            state: dict[str, object] = _requireObject(result["state"])
            points: list[dict[str, object]] = cast(list[dict[str, object]], state["points"])
            retention: dict[str, object] = BackupOperations.applyRetention(sourceClient, points,
                nowMillis=int(datetime.now(timezone.utc).timestamp() * 1000))
            result["retention"] = retention
            state["points"] = [point for point in points if point["snapshot"] not in cast(list[str], retention["deleted"])]
            saveBackup(state)
        return result


def _release(arguments: argparse.Namespace, manifest: dict[str, object], plan: dict[str, object],
        authority: OperatorAuthority, issuer: _Issuer, progress: dict[str, object], persist: Callable[[dict[str, object]], None]) -> dict[str, object]:
    installation: dict[str, object] = _requireObject(OpenSearchOperations.renderBundle(manifest)["installation-receipt.json"])
    phase: str = str(plan["phase"])
    isRehearsal: bool = plan.get("rehearsal") is True
    if isRehearsal and manifest["environment"] != "staging":
        raise ValueError("An isolated release rehearsal cannot execute against a production manifest")
    qualificationInputs: dict[str, object] = {}
    for name, reference in _requireObject(plan["qualification_receipts"]).items():
        artifact: dict[str, object] = _requireObject(reference)
        raw: bytes = (arguments.plan.parent / str(artifact["path"])).read_bytes()
        if sha256(raw).hexdigest() != artifact["sha256"]:
            raise ValueError("Reviewed release qualification artifact changed")
        qualificationInputs[name] = _readJson(arguments.plan.parent / str(artifact["path"]))
    qualification: dict[str, object] = ReleaseOperations.qualify(qualificationInputs,
        sourceHash=authority.sourceHash, manifestSha256=str(installation["manifest_sha256"]), phase=phase)
    deferred: set[str] = {"customer-traffic-gate-closed"}
    if isRehearsal:
        deferred.add("restoration-rehearsal")
        isolation: dict[str, object] = _requireObject(qualificationInputs["staging-rehearsal-isolation"])
        if (isolation.get("mode") != "actual" or isolation.get("status") != "passed" or isolation.get("environment") != "staging"
                or isolation.get("sourceHash") != authority.sourceHash or isolation.get("manifest_sha256") != installation["manifest_sha256"]
                or isolation.get("customer_traffic_present") is not False):
            raise ValueError("A first rehearsal requires actual separately reviewed staging isolation")
    if set(cast(list[str], qualification["missing"])) - deferred:
        raise ValueError("Release driver requires actual source-bound qualification receipts")
    deployments: list[dict[str, object]] = cast(list[dict[str, object]], plan["deployments"])
    if ({row.get("role") for row in deployments} != {"api", "worker"}
            or len(deployments) != 2 or len({(row["cluster"], row["service"]) for row in deployments}) != 2):
        raise ValueError("The reviewed release must deploy both existing API and worker services")
    if phase != "prepare":
        for name in ("smoke", "public_smoke"):
            cases: list[dict[str, object]] = cast(list[dict[str, object]], plan[name])
            if (not any(str(case.get("path")).startswith("/v2/jobs/") and case.get("method") == "GET" for case in cases)
                    or not any(str(case.get("path")).endswith("/retrieval/query") and case.get("quality_path") for case in cases)
                    or any(str(case.get("path")).endswith("/indexing/retry") for case in cases)):
                raise ValueError("Cutover requires private and public phase and cited evidence reads")
        if not isRehearsal:
            restoration: dict[str, object] = _requireObject(qualificationInputs["restoration-rehearsal"])
            budget: dict[str, object] = ReleaseOperations.plan(datetime.now(timezone.utc), restoration, marginSeconds=int(str(plan["margin_seconds"])))
            if budget.get("measured_restoration_seconds") != plan.get("restoration_seconds"):
                raise ValueError("Abort budget must use the exact observed compatible restoration time")
    driver: Path = _ROOT / "scripts/probes/manage-opensearch-transition.py"
    if sha256(driver.read_bytes()).hexdigest() != plan.get("transition_script_sha256"):
        raise ValueError("Transition script differs from its reviewed binding")
    steps: Path = arguments.output.parent / (arguments.output.stem + "-steps")
    steps.mkdir(mode=0o700)
    latest: dict[str, object] = dict(progress)
    deadline: float = time.monotonic() + float(str(plan["maximum_workflow_seconds"]))

    def save(value: dict[str, object]) -> None:
        latest.update(value)
        persist(value)

    def transition(verb: str, target: str) -> dict[str, object]:
        receiptPath: Path = steps / (verb + "-" + uuid4().hex + ".json")
        command: list[str] = [sys.executable, str(driver), verb, "--target-backend", target,
            "--execute", "--epoch", str(authority.epoch), "--expected-source-hash", authority.sourceHash,
            "--limit", str(plan["batch_limit"]), "--documents-per-second", str(plan["documents_per_second"]),
            "--receipt", str(receiptPath), *authority.getCommandArguments()]
        timeout: float = min(float(str(plan["maximum_step_seconds"])), deadline - time.monotonic())
        if phase == "forward" and isinstance(latest.get("abort_plan"), dict):
            cutoff: datetime = datetime.fromisoformat(str(_requireObject(latest["abort_plan"])["latest_abort_at"]))
            timeout = min(timeout, (cutoff - datetime.now(timezone.utc)).total_seconds())
        execution = OperationExecution.run(command, cwd=_ROOT, timeoutSeconds=timeout, issuer=issuer)
        if execution.returncode != 0 or not receiptPath.is_file():
            raise ValueError("Source transition did not return its known successful receipt")
        receiptPath.chmod(0o600)
        receipt: dict[str, object] = _requireObject(_readJson(receiptPath))
        authority.refresh(receipt)
        return receipt

    def verifyClosed() -> dict[str, object]:
        gate: dict[str, object] = _requireObject(plan["gate"])
        path: str = str(plan["public_gate_probe_path"])
        if not path.startswith("/v2/") or path.startswith("//"):
            raise ValueError("Gate probe must traverse the reviewed customer API path")
        expected: str = str(_requireObject(_requireObject(gate["closed_action"])["FixedResponseConfig"])["MessageBody"])
        with _apiClient(str(plan["public_api_endpoint"]), plan.get("public_api_ca_file")) as public:
            response: httpx.Response = public.get(path)
        if response.status_code != 503 or response.text != expected:
            raise ValueError("Actual public route has not observed the expected closed gate")
        return {"status": "closed", "http_status": response.status_code, "response_sha256": sha256(response.content).hexdigest()}

    cloud = _cloud(plan, manifest, authority.token)
    with _apiClient(str(plan["private_api_endpoint"]), plan.get("private_api_ca_file")) as client:
        result: dict[str, object] = ReleaseBridge.run(plan, elb=cast(_ELB, cloud("elbv2")), ecs=cast(_ECS, cloud("ecs")),
            client=client, transition=transition, persist=save, state=progress, beforeMutation=authority.beforeMutation,
            baseDirectory=arguments.plan.parent, verifyClosed=verifyClosed)
    if result.get("customer_gate") == "open":
        try:
            with _apiClient(str(plan["public_api_endpoint"]), plan.get("public_api_ca_file")) as client:
                result["public_smoke"] = ReleaseBridge.smoke(client, cast(list[dict[str, object]], plan["public_smoke"]), baseDirectory=arguments.plan.parent)
        except (httpx.HTTPError, ValueError) as error:
            result.update({"status": "known-abort", "public_smoke_error": type(error).__name__, "service_restored": False})
            persist(result)
            return result
        result["service_restored"] = True
        result["service_restored_at_utc"] = datetime.now(timezone.utc).isoformat()
        result["elapsed_disruption_seconds"] = (datetime.fromisoformat(str(result["service_restored_at_utc"]))
            - datetime.fromisoformat(str(result["first_customer_impact_at"]))).total_seconds()
        if isRehearsal:
            result["timing_evidence_requires_review"] = True
        persist(result)
    return result


def _reconcileRestart(arguments: argparse.Namespace, plan: dict[str, object], authority: OperatorAuthority,
        issuer: _Issuer, persist: Callable[[dict[str, object]], None]) -> dict[str, object]:
    inventoryPath: Path = arguments.plan.parent / str(plan["inventory_path"])
    hostPath: Path = arguments.plan.parent / str(plan["host_recovery_receipt_path"])
    driver: Path = _ROOT / "scripts/probes/manage-opensearch-transition.py"
    if (sha256(inventoryPath.read_bytes()).hexdigest() != plan.get("inventory_sha256")
            or sha256(driver.read_bytes()).hexdigest() != plan.get("transition_script_sha256")):
        raise ValueError("Reviewed restart inventory or source entrypoint changed")
    transitionPath: Path = arguments.output.with_name(arguments.output.stem + "-source.json")
    if transitionPath.exists():
        raise ValueError("Restart reconciliation requires a new immutable source receipt")
    persist({"status": "restart-identity-intent", "host_recovery_receipt_sha256": sha256(hostPath.read_bytes()).hexdigest()})
    authority.beforeMutation()
    command: list[str] = [sys.executable, str(driver), "reconcile-restart", "--inventory", str(inventoryPath),
        "--host-recovery-receipt", str(hostPath), "--receipt", str(transitionPath), "--epoch", str(authority.epoch),
        "--target-backend", str(plan["target_backend"]), "--expected-source-hash", authority.sourceHash,
        *authority.getCommandArguments(), "--execute"]
    execution = OperationExecution.run(command, cwd=_ROOT, timeoutSeconds=float(str(plan["maximum_step_seconds"])), issuer=issuer)
    if execution.returncode != 0 or not transitionPath.is_file():
        raise ValueError("Restart reconciliation did not return an acknowledged source result")
    transitionPath.chmod(0o600)
    receipt: dict[str, object] = _requireObject(_readJson(transitionPath))
    if (receipt.get("status") != "reconciled" or receipt.get("execute") is not True
            or receipt.get("sourceHash") != authority.sourceHash or receipt.get("errors") != []
            or receipt.get("native_requests_mutated") is not False or receipt.get("service_restored") is not False):
        raise ValueError("Restart reconciliation did not retain the gated source boundary")
    persist(receipt)
    return receipt


def _prepareManifestSeed(arguments: argparse.Namespace, manifest: dict[str, object], plan: dict[str, object],
        sourceReceipt: Path) -> tuple[list[str], float]:
    from shared.core.config import settings

    driver: Path = _ROOT / "scripts/probes/manage-opensearch-transition.py"
    if sha256(driver.read_bytes()).hexdigest() != plan.get("transition_script_sha256"):
        raise ValueError("Manifest seed transition script differs from its reviewed binding")
    if (settings.KNOWHERE_LEXICAL_BACKEND != "opensearch"
            or settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX != manifest["index_prefix"]
            or settings.KNOWHERE_OPENSEARCH_URL.rstrip("/") != f"https://{manifest['endpoint_host']}:9200"):
        raise ValueError("Manifest seeding requires its approved OpenSearch backend, endpoint and index prefix")
    limit: object = plan.get("batch_limit")
    timeout: float = float(str(plan.get("maximum_step_seconds")))
    if type(limit) is not int or limit < 1 or not isfinite(timeout) or timeout <= 0 or sourceReceipt.exists():
        raise ValueError("Manifest seeding requires a positive batch limit, bounded duration and new source receipt")
    command: list[str] = [sys.executable, str(driver), "seed-manifests", "--target-backend", "opensearch",
        "--epoch", str(arguments.epoch), "--expected-source-hash", arguments.source_hash,
        "--limit", str(limit), "--receipt", str(sourceReceipt), "--execute"]
    return command, timeout


def _seedManifests(command: list[str], timeout: float, sourceReceipt: Path, authority: OperatorAuthority,
        issuer: _Issuer, persist: Callable[[dict[str, object]], None]) -> dict[str, object]:
    persist({"status": "manifest-seed-intent", "source_receipt": sourceReceipt.name})
    authority.beforeMutation()
    execution: subprocess.CompletedProcess[bytes] = OperationExecution.run([*command, *authority.getCommandArguments()],
        cwd=_ROOT, timeoutSeconds=timeout, issuer=issuer)
    if execution.returncode != 0 or not sourceReceipt.is_file():
        raise ValueError("Manifest seeding did not return an acknowledged source result")
    sourceReceipt.chmod(0o600)
    receipt: dict[str, object] = _requireObject(_readJson(sourceReceipt))
    control: dict[str, object] = _requireObject(receipt.get("control"))
    sourceIssuer: dict[str, object] = _requireObject(receipt.get("issuer"))
    if (receipt.get("verb") != "seed-manifests" or receipt.get("targetBackend") != "opensearch"
            or receipt.get("execute") is not True or receipt.get("errors") != []
            or receipt.get("sourceHash") != authority.sourceHash or receipt.get("expectedSourceHash") != authority.sourceHash
            or (control.get("epoch"), control.get("cluster_uuid"), control.get("backend"), control.get("mode"))
                != (authority.epoch, authority.clusterUuid, "opensearch", "serving")
            or sourceIssuer.get("invocation_token") != authority.token or sourceIssuer.get("status") != "active"
            or type(receipt.get("seededNamespaces")) is not int):
        raise ValueError("Manifest seed result differs from the admitted source and serving epoch")
    authority.assertCurrent()
    result: dict[str, object] = {**receipt, "status": "manifests-seeded", "source_receipt": sourceReceipt.name}
    persist(result)
    return result


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=(
        "fence-host", "reconcile-fence", "release-empty-shadow", "recover-process", "recover-volume",
        "reconcile-restart", "backup", "release", "seed-manifests",
    ))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--source-hash", required=True)
    parser.add_argument("--operation-state-directory", type=Path, default=Path("/var/lib/knowhere-native-ops"))
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--fence-token", help="Original durable intent UUID for read-only SSM reconciliation")
    parser.add_argument("--execute", action="store_true")
    OperatorAuthority.addArguments(parser)
    arguments: argparse.Namespace = parser.parse_args()
    UUID(arguments.operation_id)
    manifest: dict[str, object] = _requireObject(_readJson(arguments.manifest))
    plan: dict[str, object] = _requireObject(_readJson(arguments.plan))
    installation: dict[str, object] = _requireObject(OpenSearchOperations.renderBundle(manifest)["installation-receipt.json"])
    binding: dict[str, object] = {"operation_id": arguments.operation_id, "source_hash": arguments.source_hash,
        "plan_sha256": _digest(plan), "manifest_sha256": installation["manifest_sha256"]}
    if not arguments.execute:
        print(json.dumps({"mode": "dry-run", "operation": arguments.operation, "binding": binding,
            "creates_cloud_resources": False, "formal_acceptance": False, "journal": str(arguments.journal)}))
        return 0
    purpose: str = arguments.operation if arguments.operation in ("backup", "release", "seed-manifests") else "recovery"
    _validateApproval(arguments.approval, manifestSha256=str(installation["manifest_sha256"]), purpose=purpose)
    assert arguments.approval is not None
    approval: dict[str, object] = _requireObject(_readJson(arguments.approval))
    OperationExecution.validateOperatorApproval(approval, arguments.operation_state_directory)
    if approval.get("operation_plan_sha256") != binding["plan_sha256"] or approval.get("source_hash") != arguments.source_hash:
        raise ValueError("Approval must bind this exact operation plan and aggregate source")
    if arguments.output.exists() or not arguments.issuer_token:
        raise ValueError("Use a new receipt and a fresh physical issuer token")
    OperatorAuthority.assertPhysicalUnit(arguments.issuer_token)
    progress: dict[str, object] = _requireObject(_readJson(arguments.journal)) if arguments.journal.exists() else {}
    prior: object = progress.get("binding")
    if prior is not None and prior != binding:
        previous: dict[str, object] = _requireObject(prior)
        canRollback: bool = (progress.get("status") == "known-abort"
            or (plan.get("rehearsal") is True and progress.get("status") == "rehearsal-forward-gated"))
        if (arguments.operation != "release" or plan.get("phase") != "rollback" or not canRollback
                or any(previous.get(key) != binding[key] for key in ("operation_id", "source_hash", "manifest_sha256"))
                or plan.get("forward_plan_sha256") != previous.get("plan_sha256")):
            raise ValueError("Operator resume must retain the frozen workflow or exact approved known rollback identity")

    def persist(value: dict[str, object]) -> None:
        _writeJson(arguments.journal, {**value, "binding": binding}, replace=arguments.journal.exists())

    if arguments.operation in ("fence-host", "reconcile-fence", "release-empty-shadow"):
        lockPath: Path = arguments.journal.with_name(arguments.journal.name + ".lock")
        with lockPath.open("a") as lock:
            lockPath.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current: dict[str, object] = _requireObject(_readJson(arguments.journal)) if arguments.journal.exists() else {}
            if current != progress:
                raise ValueError("Fence journal changed before physical admission; inspect its actual current state")
            execute = _releaseEmptyShadow if arguments.operation == "release-empty-shadow" else _runFence
            result: dict[str, object] = execute(arguments, manifest, plan, progress, persist)
            _writeJson(arguments.output, result)
        return 0
    sourceReceipt: Path = arguments.output.with_name(arguments.output.stem + "-source.json")
    seedInvocation: tuple[list[str], float] | None = None
    if arguments.operation == "seed-manifests":
        if progress:
            raise ValueError("Manifest seed already has a journal; inspect its outcome before issuing another batch")
        seedInvocation = _prepareManifestSeed(arguments, manifest, plan, sourceReceipt)
    with OperatorAuthority.fromArguments(arguments, manifest, operationId=arguments.operation_id,
            sourceHash=arguments.source_hash, epoch=arguments.epoch) as authority:
        with OperationExecution.claim(arguments.operation_state_directory,
                resourceId=str(manifest["region"]) + ":" + str(manifest["cluster_name"]), operationId=arguments.operation_id,
                authorityReceipt=authority.receipt) as issuer:
            current = _requireObject(_readJson(arguments.journal)) if arguments.journal.exists() else {}
            if current != progress:
                raise ValueError("Operator journal changed before physical admission; inspect its actual current state")
            if arguments.operation == "backup":
                result = _backup(arguments, manifest, plan, authority, progress, persist)
            elif arguments.operation == "release":
                result = _release(arguments, manifest, plan, authority, issuer, progress, persist)
            elif seedInvocation is not None:
                seedCommand, seedTimeout = seedInvocation
                result = _seedManifests(seedCommand, seedTimeout, sourceReceipt, authority, issuer, persist)
            elif arguments.operation == "reconcile-restart":
                if progress:
                    raise ValueError("A restart reconciliation intent already exists; inspect before repeating")
                result = _reconcileRestart(arguments, plan, authority, issuer, persist)
            else:
                if arguments.journal.exists():
                    raise ValueError("Host recovery intent already exists; reconcile actual resources before another mutation")
                from shared.core.database_sync import get_sync_session_factory
                from shared.models.database.lexicalcontrol import LexicalBackendControl

                def assertGated() -> None:
                    authority.assertCurrent()
                    with get_sync_session_factory()() as database:
                        control: LexicalBackendControl | None = database.get(LexicalBackendControl, 1)
                        if control is None or control.mode != "gated" or control.epoch != authority.epoch:
                            raise ValueError("Native process/volume recovery requires gated source admission")
                    authority.hasIssuedSideEffects = True

                fence: dict[str, object] | None = (_requireObject(_readJson(arguments.fence_receipt))
                    if arguments.fence_receipt is not None else None)
                if fence is not None and authority.receipt.get("fence_receipt_sha256") != _digest(fence):
                    raise ValueError("Host recovery cannot reuse a physical fence not consumed by this exact takeover")
                cloud = _cloud(plan, manifest, authority.token)
                result = HostRecovery.recover("process" if arguments.operation == "recover-process" else "healthy-volume", plan,
                    ec2=cast(_EC2, cloud("ec2")), ssm=cast(_SSM, cloud("ssm")), persist=persist,
                    beforeMutation=assertGated, fenceReceipt=fence)
                result["issuer"] = authority.assertCurrent()
                persist(result)
            _writeJson(arguments.output, result)
            if result.get("status") not in ("unknown", "awaiting-independent-verification"):
                issuer.complete()
                authority.complete()
    print(json.dumps({"mode": "actual", "status": result.get("status"), "receipt": str(arguments.output)}))
    return 0 if result.get("status") in ("verified", "not-due", "snapshot-in-progress", "recovered-host",
        "serving-smoke-passed", "PG-compatible-shadow-prepared", "rehearsal-forward-gated", "reconciled", "manifests-seeded") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as error:
        print("Operator workflow refused or incomplete; preserve private intent, receipts and any unresolved charges: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2) from None
