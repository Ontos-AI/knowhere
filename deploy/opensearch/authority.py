"""Bind operator RPCs to one durable cross-host physical invocation authority."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from execution import OperationExecution

__all__: list[str] = ["OperatorAuthority"]


class _Service(Protocol):
    def claim(self, **values: object) -> dict[str, object]: ...
    def assertCurrent(self, **values: object) -> dict[str, object]: ...
    def finish(self, **values: object) -> dict[str, object]: ...
    def refresh(self, **values: object) -> dict[str, object]: ...


class OperatorAuthority:
    def __init__(self, service: _Service, *, token: str, host: str, operationId: str,
            sourceHash: str, epoch: int, clusterUuid: str) -> None:
        self.service: _Service = service
        self.token: str = token
        self.host: str = host
        self.operationId: str = operationId
        self.sourceHash: str = sourceHash
        self.epoch: int = epoch
        self.clusterUuid: str = clusterUuid
        self.hasKnownOutcome: bool = False
        self.hasIssuedSideEffects: bool = False
        self.receipt: dict[str, object] = {}

    @staticmethod
    def addArguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--issuer-token", help="Unique physical invocation UUID, matching its transient systemd unit")
        parser.add_argument("--expected-issuer-token", help="Old durable issuer token for an explicitly fenced CAS takeover")
        parser.add_argument("--fence-receipt", type=Path, help="Fresh actual EC2/SSM physical-fence receipt; never a boolean override")

    @staticmethod
    def assertPhysicalUnit(token: str) -> None:
        expected: str = "knowhere-native-issuer-" + UUID(token).hex + ".service"
        cgroup: Path = Path("/proc/self/cgroup")
        if not cgroup.is_file() or not any(line.partition(":")[2].partition(":")[2].startswith(
                "/system.slice/" + expected + "/") or line.endswith(":/system.slice/" + expected)
                for line in cgroup.read_text().splitlines()):
            raise ValueError("Execute the physical invocation in its exact transient systemd issuer unit")

    @staticmethod
    def fromExisting(receipt: dict[str, object]) -> "OperatorAuthority":
        from shared.core.database_sync import get_sync_session_factory
        from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority

        token: str = str(UUID(str(receipt["invocation_token"])))
        OperatorAuthority.assertPhysicalUnit(token)
        host: str = OperationExecution.getHostFingerprint()
        if receipt.get("host_identity") != host:
            raise ValueError("Nested operator command must run in its parent's physical issuer")
        service: _Service = cast(_Service, LexicalOperatorAuthority(sessionFactory=get_sync_session_factory()))
        authority: OperatorAuthority = OperatorAuthority(service, token=token, host=host,
            operationId=str(receipt["operation_id"]), sourceHash=str(receipt["source_hash"]),
            epoch=int(str(receipt["epoch"])), clusterUuid=str(receipt["cluster_uuid"]))
        authority.receipt = authority.assertCurrent()
        if authority.receipt != receipt:
            raise ValueError("Nested operator command receipt differs from actual SQL authority")
        return authority

    @staticmethod
    @contextmanager
    def fromArguments(arguments: argparse.Namespace, manifest: dict[str, object], *, operationId: str,
            sourceHash: str, epoch: int) -> Iterator["OperatorAuthority"]:
        if not arguments.issuer_token:
            raise ValueError("Execution requires a fresh physical --issuer-token")
        token: str = str(UUID(arguments.issuer_token))
        OperatorAuthority.assertPhysicalUnit(token)
        from shared.core.database_sync import get_sync_session_factory
        from shared.models.database.lexicalcontrol import LexicalBackendControl
        from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority

        factory = get_sync_session_factory()
        with factory() as database:
            control: LexicalBackendControl | None = database.get(LexicalBackendControl, 1)
            if control is None or not control.clusterUuid:
                raise ValueError("Operator execution requires an established source-owned control/native identity")
            clusterUuid: str = control.clusterUuid
        service: _Service = cast(_Service, LexicalOperatorAuthority(sessionFactory=factory))
        host: str = OperationExecution.getHostFingerprint()
        authority: OperatorAuthority = OperatorAuthority(service, token=token, host=host,
            operationId=operationId, sourceHash=sourceHash, epoch=epoch, clusterUuid=clusterUuid)
        fence: dict[str, object] | None = None
        if arguments.fence_receipt is not None:
            value: object = json.loads(arguments.fence_receipt.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("Physical fence receipt must be an object")
            fence = cast(dict[str, object], value)
        authority.receipt = service.claim(logicalClusterId=str(manifest["region"]) + ":" + str(manifest["cluster_name"]),
            invocationToken=token, hostIdentity=host, operationId=operationId, expectedSourceHash=sourceHash,
            expectedEpoch=epoch, expectedClusterUuid=clusterUuid, expectedOwnerToken=arguments.expected_issuer_token,
            fenceReceipt=fence)
        hasSucceeded: bool = False
        try:
            yield authority
            hasSucceeded = True
        finally:
            service.finish(invocationToken=token, hostIdentity=host, operationId=operationId,
                isKnownOutcome=(hasSucceeded and authority.hasKnownOutcome) or not authority.hasIssuedSideEffects)

    def assertCurrent(self) -> dict[str, object]:
        return self.service.assertCurrent(invocationToken=self.token, hostIdentity=self.host,
            operationId=self.operationId, expectedSourceHash=self.sourceHash, expectedEpoch=self.epoch)

    def getCommandArguments(self) -> list[str]:
        self.hasIssuedSideEffects = True
        return ["--issuer-token", self.token, "--issuer-host-sha256", self.host,
            "--issuer-operation-id", self.operationId]

    def beforeMutation(self) -> None:
        self.assertCurrent()
        self.hasIssuedSideEffects = True

    def refresh(self, receipt: dict[str, object]) -> None:
        controlValue: object = receipt.get("control")
        if not isinstance(controlValue, dict):
            raise ValueError("An executed operator step must retain actual control identity")
        control: dict[str, object] = cast(dict[str, object], controlValue)
        epoch: int = int(str(control["epoch"]))
        clusterUuid: str = str(control.get("cluster_uuid") or self.clusterUuid)
        if epoch == self.epoch and clusterUuid == self.clusterUuid:
            return
        self.receipt = self.service.refresh(invocationToken=self.token, hostIdentity=self.host,
            operationId=self.operationId, expectedSourceHash=self.sourceHash,
            expectedPreviousEpoch=self.epoch, expectedEpoch=epoch,
            expectedPreviousClusterUuid=self.clusterUuid, expectedClusterUuid=clusterUuid,
            transitionReceipt=receipt)
        self.epoch, self.clusterUuid = epoch, clusterUuid

    def complete(self) -> None:
        self.hasKnownOutcome = True
