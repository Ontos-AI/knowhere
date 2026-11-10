"""One reviewed off-node operator owns a durable lease through native issuance.

An OS lock serializes live invocations. Its durable intent additionally blocks a
successor after a crash or unknown outcome; elapsed time never releases it. Every
subcommand runs in a new process group, whose descendants are killed on timeout.
The caller completes the lease only after its native/SQL outcome is known.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
from typing import cast

__all__: list[str] = ["OperationExecution"]


def _writeState(path: Path, value: dict[str, object]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary: Path = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directoryDescriptor: int = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directoryDescriptor)
        finally:
            os.close(directoryDescriptor)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class _Issuer:
    statePath: Path
    operationId: str
    resourceId: str
    invocationToken: str | None = None
    hasCompleted: bool = False
    groups: list[int] = field(default_factory=list)

    def persist(self, status: str) -> None:
        _writeState(self.statePath, {"version": 1, "resource_id": self.resourceId,
            "operation_id": self.operationId, "status": status, "operator_pid": os.getpid(),
            "invocation_token": self.invocationToken,
            "operator_host_sha256": OperationExecution.getHostFingerprint(), "process_groups": list(self.groups)})

    def recordProcessGroup(self, identity: int) -> None:
        self.groups.append(identity)
        self.persist("active")

    def complete(self) -> None:
        self.hasCompleted = True


class OperationExecution:
    @staticmethod
    def getHostFingerprint() -> str:
        machineId: Path = Path("/etc/machine-id")
        identity: bytes = machineId.read_bytes() if machineId.is_file() else socket.gethostname().encode()
        return sha256(identity.strip()).hexdigest()

    @staticmethod
    def validateOperatorApproval(receipt: dict[str, object], directory: Path) -> None:
        if (receipt.get("operator_host_sha256") != OperationExecution.getHostFingerprint()
                or receipt.get("operation_state_directory") != str(directory.resolve())):
            raise ValueError("Approval must bind this single operator host and canonical lease directory")

    @staticmethod
    @contextmanager
    def claim(directory: Path, *, resourceId: str, operationId: str,
            authorityReceipt: dict[str, object] | None = None) -> Iterator[_Issuer]:
        if not resourceId or not operationId:
            raise ValueError("Operator issuer requires exact resource and operation identities")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key: str = sha256(resourceId.encode()).hexdigest()
        state: Path = directory / (key + ".json")
        with (directory / (key + ".lock")).open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another operator issuer owns this resource") from None
            if state.exists():
                previous: object = json.loads(state.read_bytes())
                if not isinstance(previous, dict):
                    raise ValueError("Prior operator issuer needs explicit physical/native reconciliation")
                if previous.get("status") != "released":
                    # The shared source-owned claim is one-use. A fresh active
                    # token proves the previous global owner either released or
                    # was physically fenced through an exact-owner CAS takeover.
                    if (authorityReceipt is None or authorityReceipt.get("status") != "active"
                            or authorityReceipt.get("logical_cluster_id") != resourceId
                            or not previous.get("invocation_token")
                            or authorityReceipt.get("invocation_token") == previous.get("invocation_token")):
                        raise ValueError("Prior operator issuer needs explicit physical/native reconciliation")
            issuer: _Issuer = _Issuer(statePath=state, operationId=operationId, resourceId=resourceId,
                invocationToken=str(authorityReceipt["invocation_token"]) if authorityReceipt is not None else None)
            issuer.persist("active")
            isSuccessful: bool = False
            try:
                yield issuer
                isSuccessful = True
            finally:
                issuer.persist("released" if issuer.hasCompleted and isSuccessful else "unknown")

    @staticmethod
    def run(command: Sequence[str], *, timeoutSeconds: float, cwd: Path | None = None,
            issuer: _Issuer | None = None) -> subprocess.CompletedProcess[bytes]:
        if timeoutSeconds <= 0 or not command:
            raise ValueError("Operator commands require a positive bounded duration")
        process: subprocess.Popen[bytes] = subprocess.Popen(list(command), cwd=cwd, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if issuer is not None:
                issuer.recordProcessGroup(process.pid)
            process.wait(timeout=timeoutSeconds)
        except BaseException as error:
            OperationExecution._terminate(process)
            if isinstance(error, subprocess.TimeoutExpired):
                raise TimeoutError("Operator subprocess outcome is unknown; its command process group was stopped") from None
            raise
        # A successful supervisor exit must not leave an untracked descendant.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return subprocess.CompletedProcess(list(command), cast(int, process.returncode))
        OperationExecution._terminate(process)
        raise ValueError("Operator command left a descendant issuer; reconcile the retained intent")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
