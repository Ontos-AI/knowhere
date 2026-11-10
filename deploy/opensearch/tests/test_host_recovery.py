"""Actual driver logic executes against local EC2/SSM protocol doubles."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import cast
from uuid import UUID

import pytest


def loadHostRecovery() -> type:
    directory: Path = Path(__file__).parents[1]
    sys.path.insert(0, str(directory))
    path: Path = directory / "host_recovery.py"
    assert path.is_file(), "Concrete existing-host recovery driver is absent"
    specification = importlib.util.spec_from_file_location("host_recovery_driver", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.HostRecovery)


def createPlan() -> dict[str, object]:
    return {"version": 1, "region": "us-east-1", "account_id": "123456789012", "logical_cluster_id": "us-east-1:knowhere-staging",
        "old_operator_instance_id": "i-0123456789abcdef0", "old_native_instance_id": "i-1123456789abcdef0",
        "target_native_instance_id": "i-2123456789abcdef0", "data_volume_id": "vol-0123456789abcdef0",
        "availability_zone": "us-east-1a", "filesystem_uuid": "12345678-1234-1234-1234-123456789abc",
        "installation_sha256": "f" * 64, "timeout_seconds": 30,
        "old_issuer": {"invocation_token": "11111111-1111-1111-1111-111111111111", "host_identity": "a" * 64,
            "operation_id": "restore-one", "source_hash": "b" * 64, "epoch": 4, "cluster_uuid": "cluster-one"}}


class FakeCloud:
    def __init__(self, *, failFence: bool = False) -> None:
        self.calls: list[str] = []
        self.failFence: bool = failFence
        self.instances: dict[str, str] = {"i-1123456789abcdef0": "running", "i-2123456789abcdef0": "running"}
        self.attachment: str | None = "i-1123456789abcdef0"
        self.commands: list[dict[str, object]] = []

    def describe_instances(self, **values: object) -> dict[str, object]:
        return {"Reservations": [{"OwnerId": "123456789012", "Instances": [{"InstanceId": identity, "Placement": {"AvailabilityZone": "us-east-1a"},
            "State": {"Name": self.instances.get(identity, "running")}} for identity in cast(list[str], values["InstanceIds"])]}]}

    def stop_instances(self, **values: object) -> dict[str, object]:
        self.calls.append("stop-old-native")
        for identity in cast(list[str], values["InstanceIds"]):
            self.instances[identity] = "stopped"
        return {}

    def describe_volumes(self, **_values: object) -> dict[str, object]:
        return {"Volumes": [{"VolumeId": "vol-0123456789abcdef0", "AvailabilityZone": "us-east-1a", "Encrypted": True,
            "Attachments": [{"InstanceId": self.attachment, "State": "attached"}] if self.attachment else [], "State": "in-use" if self.attachment else "available"}]}

    def detach_volume(self, **values: object) -> dict[str, object]:
        assert self.instances["i-1123456789abcdef0"] == "stopped"
        self.calls.append("detach")
        self.attachment = None
        return {}

    def attach_volume(self, **values: object) -> dict[str, object]:
        assert self.attachment is None
        self.calls.append("attach")
        self.attachment = str(values["InstanceId"])
        return {}

    def send_command(self, **values: object) -> dict[str, object]:
        self.commands.append(values)
        self.calls.append("ssm")
        return {"Command": {"CommandId": str(UUID(int=len(self.commands)))}}

    def get_command_invocation(self, **values: object) -> dict[str, object]:
        command = self.commands[UUID(str(values["CommandId"])).int - 1]
        kind: str = str(command["Comment"]).split(":")[-1]
        result: dict[str, object] = {"kind": kind, "host_sha256": "a" * 64, "active_state": "inactive", "main_pid": 0, "cgroup_empty": True}
        if kind == "observe-stopped-issuer":
            result["load_state"] = "not-found"
        if kind == "recover-native":
            result.update({"active_state": "active", "main_pid": 42, "installation_sha256": "f" * 64,
                "filesystem_uuid": "12345678-1234-1234-1234-123456789abc"})
        return {"Status": "Failed" if self.failFence else "Success", "ResponseCode": 1 if self.failFence else 0, "StandardOutputContent": json.dumps(result)}

    def list_commands(self, **values: object) -> dict[str, object]:
        return {"Commands": [{"CommandId": str(UUID(int=index)), "Comment": command["Comment"], "DocumentName": command["DocumentName"]}
            for index, command in enumerate(self.commands, start=1)
            if values.get("CommandId", str(UUID(int=index))) == str(UUID(int=index))]}


def test_healthy_volume_fences_old_host_before_detach_attach_and_verified_start() -> None:
    recovery: type = loadHostRecovery()
    cloud: FakeCloud = FakeCloud()
    result = recovery.recover("healthy-volume", createPlan(), ec2=cloud, ssm=cloud)
    assert result["status"] == "recovered-host"
    assert cloud.calls.index("stop-old-native") < cloud.calls.index("detach") < cloud.calls.index("attach")
    assert result["fence"]["native_issuer_fenced"] is True
    assert result["service_restored"] is False
    assert all(command["DocumentName"] == "AWS-RunShellScript" for command in cloud.commands)
    assert not any("mkfs" in str(command) for command in cloud.commands)


def test_failed_fence_never_moves_volume_or_starts_replacement() -> None:
    recovery: type = loadHostRecovery()
    cloud: FakeCloud = FakeCloud(failFence=True)
    with pytest.raises(ValueError):
        recovery.recover("healthy-volume", createPlan(), ec2=cloud, ssm=cloud)
    assert "detach" not in cloud.calls and "attach" not in cloud.calls


def test_fenced_same_host_recovery_reuses_the_proven_fence_and_never_moves_volume() -> None:
    recovery: type = loadHostRecovery()
    cloud: FakeCloud = FakeCloud()
    plan: dict[str, object] = createPlan()
    plan["target_native_instance_id"] = plan["old_native_instance_id"]
    identity: str = "22222222-2222-2222-2222-222222222222"
    commands: dict[str, object] = {}
    fence = recovery.fence(plan, ec2=cloud, ssm=cloud, commandIdentity=identity,
        recordCommand=lambda kind, command: commands.update({kind: command}))
    assert len(cloud.commands) == 2 and len(commands) == 2
    observed = recovery.reconcileFence(plan, ec2=cloud, ssm=cloud, commandIdentity=identity, commands=commands)
    assert len(cloud.commands) == 2
    assert observed["external"]["command_id"] == fence["external"]["command_id"]
    assert observed["native_fence"]["command_id"] == fence["native_fence"]["command_id"]
    result = recovery.recover("process", plan, ec2=cloud, ssm=cloud, fenceReceipt=fence)
    assert result["status"] == "recovered-host" and result["service_restored"] is False
    assert len(cloud.commands) == 3 and cloud.commands[-1]["Comment"] == "recover-native"
    assert cloud.calls == ["ssm", "ssm", "ssm"]


def test_lost_fence_ownership_before_native_stop_prevents_the_second_ssm_send() -> None:
    recovery: type = loadHostRecovery()
    cloud: FakeCloud = FakeCloud()
    calls: list[str] = []

    def assertReservation() -> None:
        if calls:
            raise ValueError("Fence ownership changed")
        calls.append("reserved")

    with pytest.raises(ValueError, match="ownership changed"):
        recovery.fence(createPlan(), ec2=cloud, ssm=cloud, beforeMutation=assertReservation)
    assert len(cloud.commands) == 1 and cloud.commands[0]["Comment"] == "fence-operator"



def test_observe_stopped_issuer_never_requests_native_fencing() -> None:
    recovery: type = loadHostRecovery()
    cloud: FakeCloud = FakeCloud()
    receipt = recovery.observeStoppedIssuer(createPlan(), ec2=cloud, ssm=cloud, commandIdentity=str(UUID(int=99)))
    assert receipt["status"] == "stopped"
    assert receipt["driver"] == "aws-ssm-stopped-issuer-v1"
    assert receipt["external"]["cgroup_empty"] is True
    assert receipt["external"]["load_state"] == "not-found"
    assert len(cloud.commands) == 1
    assert str(cloud.commands[0]["Comment"]).endswith(":observe-stopped-issuer")
    assert cloud.commands[0]["InstanceIds"] == ["i-0123456789abcdef0"]
    assert cloud.calls == ["ssm"]
