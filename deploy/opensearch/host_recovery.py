"""Manually admitted recovery on existing EC2/SSM resources; no provisioning.

The driver fences the exact old transient issuer unit and native service before
moving a retained volume. AWS responses and SSM observations produce receipts;
an approval boolean cannot substitute for those observations. No route formats a
volume, creates an instance/network, rewinds PG or reopens customer traffic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
import shlex
import time
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__: list[str] = ["HostRecovery"]


class _EC2(Protocol):
    def describe_instances(self, **values: object) -> dict[str, object]: ...
    def stop_instances(self, **values: object) -> dict[str, object]: ...
    def describe_volumes(self, **values: object) -> dict[str, object]: ...
    def detach_volume(self, **values: object) -> dict[str, object]: ...
    def attach_volume(self, **values: object) -> dict[str, object]: ...


class _SSM(Protocol):
    def send_command(self, **values: object) -> dict[str, object]: ...
    def get_command_invocation(self, **values: object) -> dict[str, object]: ...
    def list_commands(self, **values: object) -> dict[str, object]: ...


class _OldIssuer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invocation_token: UUID
    host_identity: str = Field(pattern="^[a-f0-9]{64}$")
    operation_id: str = Field(min_length=1)
    source_hash: str = Field(pattern="^[a-f0-9]{64}$")
    epoch: int = Field(ge=1)
    cluster_uuid: str = Field(min_length=1)


class _HostPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    region: Literal["us-east-1"]
    account_id: str = Field(pattern="^[0-9]{12}$")
    logical_cluster_id: str = Field(min_length=1)
    old_operator_instance_id: str = Field(pattern="^i-[a-f0-9]{8,17}$")
    old_native_instance_id: str = Field(pattern="^i-[a-f0-9]{8,17}$")
    target_native_instance_id: str = Field(pattern="^i-[a-f0-9]{8,17}$")
    data_volume_id: str = Field(pattern="^vol-[a-f0-9]{8,17}$")
    availability_zone: str = Field(pattern="^us-east-1[a-z]$")
    filesystem_uuid: str = Field(pattern="^[a-fA-F0-9-]{8,64}$")
    installation_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    timeout_seconds: float = Field(gt=0, le=3600)
    old_issuer: _OldIssuer
    old_issuer_receipt_path: str | None = None
    old_issuer_receipt_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Cloud driver response must be an object")
    return cast(dict[str, object], value)


def _remoteScript(kind: str, plan: _HostPlan) -> str:
    # Literal reviewed Python executes through AWS-RunShellScript. Every resource
    # value is JSON data, shell quoted once; no secret enters commands or logs.
    program: str = "\n".join([
        "import hashlib,json,pathlib,subprocess,sys,time",
        "p=json.loads(sys.argv[1]); kind=sys.argv[2]",
        "def run(argv,check=True): return subprocess.run(argv,check=check,capture_output=True,text=True)",
        "host=hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes().strip()).hexdigest()",
        "unit='knowhere-native-issuer-'+p['old_issuer']['invocation_token'].replace('-','')+'.service' if kind in ('fence-operator','observe-stopped-issuer') else 'knowhere-opensearch.service'",
        "if kind in ('fence-operator','observe-stopped-issuer') and host!=p['old_issuer']['host_identity']: raise RuntimeError('Old operator host differs')",
        "if kind.startswith('fence-'):",
        " run(['systemctl','kill','--kill-who=all','--signal=SIGKILL',unit],False)",
        " run(['systemctl','stop',unit],False)",
        "elif kind=='recover-native':",
        " receipt=pathlib.Path('/opt/knowhere-opensearch/knowhere-installation.json')",
        " if hashlib.sha256(receipt.read_bytes()).hexdigest()!=p['installation_sha256']: raise RuntimeError('Pinned installation differs')",
        " devices=json.loads(run(['lsblk','-dn','-o','NAME,SERIAL','--json']).stdout)['blockdevices']",
        " matches=[d for d in devices if str(d.get('serial','')).replace('-','')==p['data_volume_id'].replace('-','')]",
        " if len(matches)!=1: raise RuntimeError('Retained volume identity differs')",
        " device='/dev/'+matches[0]['name']",
        " if run(['blkid','-s','UUID','-o','value',device]).stdout.strip()!=p['filesystem_uuid']: raise RuntimeError('Filesystem identity differs')",
        " mounted=run(['findmnt','-n','-o','UUID','--mountpoint','/var/lib/knowhere-opensearch'],False)",
        " if mounted.returncode==0 and mounted.stdout.strip()!=p['filesystem_uuid']: raise RuntimeError('Mount point already belongs to another volume')",
        " if mounted.returncode!=0: run(['mount','--uuid',p['filesystem_uuid'],'--target','/var/lib/knowhere-opensearch'])",
        " run(['systemctl','start','knowhere-opensearch.service'])",
        "shown=run(['systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','MainPID','-p','ControlGroup'],False)",
        "fields=dict(row.split('=',1) for row in shown.stdout.splitlines())",
        "active=fields['ActiveState']; load=fields['LoadState']",
        "if kind=='observe-stopped-issuer' and load!='not-found': raise RuntimeError('Old unit is still registered')",
        "if kind!='observe-stopped-issuer' and shown.returncode!=0: raise RuntimeError('Cannot read system unit state')",
        "if load not in ('loaded','not-found') or (load=='not-found' and kind not in ('fence-operator','observe-stopped-issuer')): raise RuntimeError('Unexpected system unit load state')",
        "pid=int(fields.get('MainPID','0') if load=='not-found' else fields['MainPID'])",
        "group=fields.get('ControlGroup','') or '/system.slice/'+unit",
        "if group!='/system.slice/'+unit: raise RuntimeError('Unexpected system unit control group')",
        "root=pathlib.Path('/sys/fs/cgroup')/group.lstrip('/')",
        "empty=not root.exists() or (not (root/'cgroup.procs').read_text().strip() and all(not f.read_text().strip() for f in root.rglob('cgroup.procs')))",
        "if (kind.startswith('fence-') or kind=='observe-stopped-issuer') and (active not in ('inactive','failed') or pid!=0 or not empty): raise RuntimeError('Issuer still active')",
        "if kind=='recover-native' and (active!='active' or pid<=0): raise RuntimeError('Native service did not start')",
        "print(json.dumps({'kind':kind,'host_sha256':host,'active_state':active or 'inactive','load_state':load,'main_pid':pid,'cgroup_empty':empty,'installation_sha256':p['installation_sha256'],'filesystem_uuid':p['filesystem_uuid']}))",
    ])
    return "python3 -c " + shlex.quote(program) + " " + shlex.quote(json.dumps(plan.model_dump(mode="json"), separators=(",", ":"))) + " " + shlex.quote(kind)


class HostRecovery:
    @staticmethod
    def _invoke(ssm: _SSM, plan: _HostPlan, instanceId: str, kind: str, deadline: float,
            *, now: Callable[[], float], sleep: Callable[[float], None], commandIdentity: str | None = None,
            recordCommand: Callable[[str, str], None] = lambda _kind, _command: None) -> tuple[dict[str, object], str]:
        if now() >= deadline:
            raise TimeoutError("Recovery budget elapsed before native issuer request")
        response: dict[str, object] = ssm.send_command(InstanceIds=[instanceId], DocumentName="AWS-RunShellScript",
            Comment=kind if commandIdentity is None else "knowhere:" + UUID(commandIdentity).hex + ":" + kind,
            Parameters={"commands": [_remoteScript(kind, plan)], "executionTimeout": [str(max(1, int(deadline - now())))]},
            TimeoutSeconds=max(30, int(deadline - now())))
        commandId: str = str(_object(response["Command"])["CommandId"])
        recordCommand(kind, commandId)
        return HostRecovery._observe(ssm, commandId, instanceId, kind, deadline, now=now, sleep=sleep), commandId

    @staticmethod
    def _observe(ssm: _SSM, commandId: str, instanceId: str, kind: str, deadline: float,
            *, now: Callable[[], float], sleep: Callable[[float], None]) -> dict[str, object]:
        while now() < deadline:
            try:
                result: dict[str, object] = ssm.get_command_invocation(CommandId=commandId, InstanceId=instanceId)
            except Exception as error:
                if type(error).__name__ != "InvocationDoesNotExist":
                    raise
                sleep(min(1, max(0, deadline - now())))
                continue
            if result.get("Status") == "Success" and result.get("ResponseCode") == 0:
                observed: dict[str, object] = _object(json.loads(str(result.get("StandardOutputContent", ""))))
                if observed.get("kind") != kind:
                    raise ValueError("SSM output does not identify the issued operation")
                return observed
            if result.get("Status") not in ("Pending", "InProgress", "Delayed"):
                raise ValueError("SSM operation failed; retain unknown ownership and reconcile")
            sleep(min(1, max(0, deadline - now())))
        raise TimeoutError("SSM issuer remains uncertain; never repeat or send an opposite operation")

    @staticmethod
    def observeStoppedIssuer(value: object, *, ec2: _EC2, ssm: _SSM, commandIdentity: str,
                             now: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
        plan: _HostPlan = _HostPlan.model_validate(value)
        HostRecovery._instances(ec2, plan)
        observed, command = HostRecovery._invoke(
            ssm, plan, plan.old_operator_instance_id, "observe-stopped-issuer",
            now() + plan.timeout_seconds, now=now, sleep=sleep, commandIdentity=commandIdentity,
        )
        if observed.get("load_state") != "not-found":
            raise ValueError("Old transient issuer unit is still registered")
        external: dict[str, object] = HostRecovery._getStoppedIssuerEvidence(plan, observed, command)
        return {"version": 1, "mode": "actual", "status": "stopped", "driver": "aws-ssm-stopped-issuer-v1",
            "logical_cluster_id": plan.logical_cluster_id, "old_issuer": plan.old_issuer.model_dump(mode="json"),
            "verified_at_utc": datetime.now(timezone.utc).isoformat(), "external": {**external, "load_state": "not-found"}}

    @staticmethod
    def fence(value: object, *, ec2: _EC2, ssm: _SSM, now: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, beforeMutation: Callable[[], None] = lambda: None,
            commandIdentity: str | None = None, recordCommand: Callable[[str, str], None] = lambda _kind, _command: None) -> dict[str, object]:
        plan: _HostPlan = _HostPlan.model_validate(value)
        HostRecovery._instances(ec2, plan)
        deadline: float = now() + plan.timeout_seconds
        beforeMutation()
        operator, command = HostRecovery._invoke(ssm, plan, plan.old_operator_instance_id, "fence-operator", deadline,
            now=now, sleep=sleep, commandIdentity=commandIdentity, recordCommand=recordCommand)
        if (operator.get("host_sha256") != plan.old_issuer.host_identity or operator.get("active_state") not in ("inactive", "failed")
                or operator.get("main_pid") != 0 or operator.get("cgroup_empty") is not True):
            raise ValueError("Actual old operator host/unit was not fenced")
        beforeMutation()
        native, nativeCommand = HostRecovery._invoke(ssm, plan, plan.old_native_instance_id, "fence-native", deadline,
            now=now, sleep=sleep, commandIdentity=commandIdentity, recordCommand=recordCommand)
        return HostRecovery._fenceReceipt(plan, operator, command, native, nativeCommand)

    @staticmethod
    def reconcileFence(value: object, *, ec2: _EC2, ssm: _SSM, commandIdentity: str, commands: dict[str, object],
            now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
        plan: _HostPlan = _HostPlan.model_validate(value)
        HostRecovery._instances(ec2, plan)
        deadline: float = now() + plan.timeout_seconds
        observed: list[tuple[dict[str, object], str]] = []
        for kind, instance in (("fence-operator", plan.old_operator_instance_id), ("fence-native", plan.old_native_instance_id)):
            comment: str = "knowhere:" + UUID(commandIdentity).hex + ":" + kind
            command: object = commands.get(kind)
            if command:
                response: dict[str, object] = ssm.list_commands(CommandId=command)
            else:
                response = ssm.list_commands(InstanceId=instance, MaxResults=50)
            rows: list[dict[str, object]] = [row for row in cast(list[dict[str, object]], response.get("Commands", []))
                if row.get("Comment") == comment and row.get("DocumentName") == "AWS-RunShellScript"]
            if len(rows) != 1 or response.get("NextToken") or (command and rows[0].get("CommandId") != command):
                raise ValueError("Cannot identify one previously submitted fence command; never reissue")
            identity: str = str(UUID(str(rows[0]["CommandId"])))
            observed.append((HostRecovery._observe(ssm, identity, instance, kind, deadline, now=now, sleep=sleep), identity))
        (operator, command), (native, nativeCommand) = observed
        return HostRecovery._fenceReceipt(plan, operator, command, native, nativeCommand)

    @staticmethod
    def _getStoppedIssuerEvidence(plan: _HostPlan, operator: dict[str, object], command: str) -> dict[str, object]:
        if (operator.get("host_sha256") != plan.old_issuer.host_identity or operator.get("active_state") not in ("inactive", "failed")
                or operator.get("main_pid") != 0 or operator.get("cgroup_empty") is not True):
            raise ValueError("Actual old operator host/unit was not fenced")
        return {"account_id": plan.account_id, "region": plan.region, "instance_id": plan.old_operator_instance_id,
            "command_id": command, "document_name": "AWS-RunShellScript", "status": "Success", "response_code": 0,
            "unit_name": "knowhere-native-issuer-" + plan.old_issuer.invocation_token.hex + ".service",
            "observed_host_sha256": operator["host_sha256"], "active_state": operator["active_state"], "main_pid": 0,
            "cgroup_empty": True}

    @staticmethod
    def _fenceReceipt(plan: _HostPlan, operator: dict[str, object], command: str,
            native: dict[str, object], nativeCommand: str) -> dict[str, object]:
        external: dict[str, object] = HostRecovery._getStoppedIssuerEvidence(plan, operator, command)
        if native.get("active_state") not in ("inactive", "failed") or native.get("main_pid") != 0 or native.get("cgroup_empty") is not True:
            raise ValueError("Actual old native issuer was not fenced")
        return {"version": 1, "mode": "actual", "status": "fenced", "driver": "aws-ssm-systemd-v1",
            "logical_cluster_id": plan.logical_cluster_id, "old_issuer": plan.old_issuer.model_dump(mode="json"),
            "verified_at_utc": datetime.now(timezone.utc).isoformat(), "external": external,
            "native_issuer_fenced": True, "native_fence": {"instance_id": plan.old_native_instance_id, "command_id": nativeCommand,
                "status": "Success", "response_code": 0, "unit": "knowhere-opensearch.service", "active_state": native["active_state"], "main_pid": 0}}

    @staticmethod
    def _instances(ec2: _EC2, plan: _HostPlan) -> dict[str, dict[str, object]]:
        names: set[str] = {plan.old_native_instance_id, plan.target_native_instance_id, plan.old_operator_instance_id}
        response: dict[str, object] = ec2.describe_instances(InstanceIds=sorted(names))
        rows: dict[str, dict[str, object]] = {}
        for reservation in cast(list[dict[str, object]], response.get("Reservations", [])):
            if reservation.get("OwnerId") != plan.account_id:
                raise ValueError("An existing instance belongs to another AWS account")
            for instance in cast(list[dict[str, object]], reservation.get("Instances", [])):
                rows[str(instance["InstanceId"])] = instance
        if set(rows) != names or any(_object(rows[name]["Placement"]).get("AvailabilityZone") != plan.availability_zone for name in (plan.old_native_instance_id, plan.target_native_instance_id)):
            raise ValueError("Recovery requires exact existing same-AZ native instances")
        return rows

    @staticmethod
    def recover(route: Literal["process", "healthy-volume"], value: object, *, ec2: _EC2, ssm: _SSM,
            now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
            persist: Callable[[dict[str, object]], None] | None = None,
            beforeMutation: Callable[[], None] = lambda: None, fenceReceipt: dict[str, object] | None = None) -> dict[str, object]:
        plan: _HostPlan = _HostPlan.model_validate(value)
        if route == "process" and plan.old_native_instance_id != plan.target_native_instance_id:
            raise ValueError("Process recovery must use the original healthy host/volume")
        result: dict[str, object] = {"version": 1, "mode": "actual", "status": "intent", "route": route,
            "plan_sha256": sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "service_restored": False}
        if persist is not None:
            persist(result)
        deadline: float = now() + plan.timeout_seconds
        HostRecovery._instances(ec2, plan)
        if fenceReceipt is not None and (fenceReceipt.get("logical_cluster_id") != plan.logical_cluster_id
                or fenceReceipt.get("old_issuer") != plan.old_issuer.model_dump(mode="json")
                or _object(fenceReceipt.get("native_fence")).get("instance_id") != plan.old_native_instance_id
                or _object(fenceReceipt.get("external")).get("instance_id") != plan.old_operator_instance_id):
            raise ValueError("Admitted physical fence does not bind the exact recovery hosts and old issuer")
        fence: dict[str, object] = fenceReceipt if fenceReceipt is not None else HostRecovery.fence(value,
            ec2=ec2, ssm=ssm, now=now, sleep=sleep, beforeMutation=beforeMutation)
        result["fence"] = fence
        if persist is not None:
            persist(result)
        if route == "healthy-volume":
            if plan.old_native_instance_id == plan.target_native_instance_id:
                raise ValueError("Healthy-volume recovery requires a separately approved existing replacement")
            beforeMutation()
            ec2.stop_instances(InstanceIds=[plan.old_native_instance_id])
            while now() < deadline:
                old: dict[str, object] = HostRecovery._instances(ec2, plan)[plan.old_native_instance_id]
                if _object(old["State"]).get("Name") == "stopped":
                    break
                sleep(min(1, max(0, deadline - now())))
            else:
                raise TimeoutError("Old native host is not stopped; no volume movement is allowed")
            volume: dict[str, object] = cast(list[dict[str, object]], ec2.describe_volumes(VolumeIds=[plan.data_volume_id])["Volumes"])[0]
            attachments: list[dict[str, object]] = cast(list[dict[str, object]], volume.get("Attachments", []))
            if (volume.get("VolumeId") != plan.data_volume_id or volume.get("AvailabilityZone") != plan.availability_zone
                    or volume.get("Encrypted") is not True or len(attachments) != 1 or attachments[0].get("InstanceId") != plan.old_native_instance_id):
                raise ValueError("Retained encrypted volume ownership/AZ differs from the approved source")
            beforeMutation()
            ec2.detach_volume(VolumeId=plan.data_volume_id, InstanceId=plan.old_native_instance_id, Force=False)
            while now() < deadline:
                detached: dict[str, object] = cast(list[dict[str, object]], ec2.describe_volumes(VolumeIds=[plan.data_volume_id])["Volumes"])[0]
                if detached.get("State") == "available" and detached.get("Attachments") == []:
                    break
                sleep(min(1, max(0, deadline - now())))
            else:
                raise TimeoutError("Volume detach is uncertain; do not attach it elsewhere")
            beforeMutation()
            ec2.attach_volume(VolumeId=plan.data_volume_id, InstanceId=plan.target_native_instance_id, Device="/dev/sdf")
            while now() < deadline:
                attached: dict[str, object] = cast(list[dict[str, object]], ec2.describe_volumes(VolumeIds=[plan.data_volume_id])["Volumes"])[0]
                attachments = cast(list[dict[str, object]], attached.get("Attachments", []))
                if len(attachments) == 1 and attachments[0].get("InstanceId") == plan.target_native_instance_id and attachments[0].get("State") == "attached":
                    break
                sleep(min(1, max(0, deadline - now())))
            else:
                raise TimeoutError("Replacement volume attachment remains uncertain")
        if now() >= deadline:
            raise TimeoutError("Recovery budget elapsed before replacement start")
        beforeMutation()
        observed, commandId = HostRecovery._invoke(ssm, plan, plan.target_native_instance_id, "recover-native", deadline, now=now, sleep=sleep)
        if observed.get("active_state") != "active" or observed.get("installation_sha256") != plan.installation_sha256 or observed.get("filesystem_uuid") != plan.filesystem_uuid:
            raise ValueError("Replacement runtime or retained filesystem verification failed")
        result.update({"status": "recovered-host", "command_id": commandId, "host": observed,
            "requires_native_identity_coverage_and_API_verification": True})
        if persist is not None:
            persist(result)
        return result
