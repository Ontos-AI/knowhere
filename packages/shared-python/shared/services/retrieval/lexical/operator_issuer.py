"""Cross-host physical issuer authority; timeout never permits takeover."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicaloperatorfence import LexicalOperatorFence
from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
from shared.models.database.lexicaloperatorinvocation import LexicalOperatorInvocation
from shared.services.retrieval.lexical.error import LexicalIndexError

_HASH_PATTERN: str = r"^[a-f0-9]{64}$"
_INSTANCE_PATTERN: str = r"^i-[a-f0-9]{8,17}$"
_OWNER_BINDING_FIELDS: tuple[str, ...] = ("logical_cluster_id", "invocation_token", "host_identity", "operation_id",
    "source_hash", "epoch", "cluster_uuid", "acquired_at_utc")


class _OldIssuer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_token: str
    host_identity: str = Field(pattern=_HASH_PATTERN)
    operation_id: str
    source_hash: str = Field(pattern=_HASH_PATTERN)
    epoch: int = Field(ge=1)
    cluster_uuid: str


class _ExternalFence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(pattern=r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")
    instance_id: str = Field(pattern=_INSTANCE_PATTERN)
    command_id: str
    document_name: Literal["AWS-RunShellScript"]
    status: Literal["Success"]
    response_code: int = Field(ge=0, le=0)
    unit_name: str
    observed_host_sha256: str = Field(pattern=_HASH_PATTERN)
    active_state: Literal["inactive", "failed"]
    main_pid: int = Field(ge=0, le=0)
    cgroup_empty: bool


class _NativeFence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instance_id: str = Field(pattern=_INSTANCE_PATTERN)
    command_id: str
    status: Literal["Success"]
    response_code: int = Field(ge=0, le=0)
    unit: Literal["knowhere-opensearch.service"]
    active_state: Literal["inactive", "failed"]
    main_pid: int = Field(ge=0, le=0)


class _PhysicalFence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1, le=1)
    mode: Literal["actual"]
    status: Literal["fenced"]
    driver: Literal["aws-ssm-systemd-v1"]
    logical_cluster_id: str
    old_issuer: _OldIssuer
    verified_at_utc: str
    external: _ExternalFence
    native_issuer_fenced: bool
    native_fence: _NativeFence


class _ActiveIssuer(_OldIssuer):
    model_config = ConfigDict(extra="ignore", strict=True)
    version: int = Field(ge=1, le=1)
    mode: Literal["actual"]
    status: Literal["active"]
    logical_cluster_id: str
    acquired_at_utc: str


class _RefreshedControl(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    backend: Literal["postgresql", "opensearch"]
    mode: Literal["serving", "gated"]
    epoch: int = Field(ge=1)
    cluster_uuid: str


class _TransitionReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    version: int = Field(ge=1, le=1)
    verb: Literal["gate", "resume", "abort", "reserve-restore", "verify-restore"]
    sourceHash: str = Field(pattern=_HASH_PATTERN)
    execute: bool
    errors: list[object]
    issuer: _ActiveIssuer
    control: _RefreshedControl
    status: str | None = None


def _requireText(value: str, *, maximum: int, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise LexicalIndexError(f"Operator issuer {name} is invalid")


def _checkToken(token: str) -> None:
    try:
        if str(UUID(token)) != token:
            raise ValueError("Invocation UUID must use its canonical lowercase spelling")
    except (ValueError, TypeError, AttributeError) as error:
        raise LexicalIndexError("Operator invocation requires a unique physical UUID") from error


def _checkIdentity(token: str, host: str, operation: str) -> None:
    _checkToken(token)
    if not isinstance(host, str) or not re.fullmatch(_HASH_PATTERN, host):
        raise LexicalIndexError("Operator issuer host identity must be the observed machine SHA-256")
    _requireText(operation, maximum=128, name="operation identity")


def _checkSource(expected: str) -> None:
    from shared.services.retrieval.lexical.transition import LexicalTransition

    if not isinstance(expected, str) or not re.fullmatch(_HASH_PATTERN, expected) or LexicalTransition.getSourceHash() != expected:
        raise LexicalIndexError("Operator issuer source differs from the reviewed executable source")


def _checkEpoch(control: LexicalBackendControl, expected: int) -> None:
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1 or control.epoch != expected:
        raise LexicalIndexError("Operator issuer lost its exact backend epoch")


def _project(issuer: LexicalOperatorIssuer) -> dict[str, object]:
    return {"version": 1, "mode": "actual", "status": issuer.state,
        "logical_cluster_id": issuer.logicalClusterId, "invocation_token": issuer.invocationToken,
        "host_identity": issuer.hostIdentity, "operation_id": issuer.operationId,
        "source_hash": issuer.sourceHash, "epoch": issuer.epoch, "cluster_uuid": issuer.clusterUuid,
        "acquired_at_utc": issuer.acquiredAt.isoformat(),
        "released_at_utc": issuer.releasedAt.isoformat() if issuer.releasedAt is not None else None,
        "fence_receipt_sha256": issuer.fenceReceiptSha256}


def _checkOwner(issuer: LexicalOperatorIssuer | None, token: str, host: str, operation: str) -> LexicalOperatorIssuer:
    if issuer is None or (issuer.invocationToken, issuer.hostIdentity, issuer.operationId) != (token, host, operation):
        raise LexicalIndexError("Operator issuer lost its physical invocation authority")
    return issuer


def _checkFence(receipt: dict[str, object] | None, issuer: LexicalOperatorIssuer,
                expectedOwnerToken: str | None, now: datetime, *, reservedAt: datetime) -> str:
    if receipt is None or expectedOwnerToken != issuer.invocationToken:
        raise LexicalIndexError("Operator takeover requires an exact old-owner CAS and actual physical fence")
    try:
        proof: _PhysicalFence = _PhysicalFence.model_validate(receipt)
        verifiedAt: datetime = datetime.fromisoformat(proof.verified_at_utc)
        UUID(proof.external.command_id)
        UUID(proof.native_fence.command_id)
    except (ValidationError, ValueError) as error:
        raise LexicalIndexError("Operator takeover physical fence receipt is incomplete") from error
    old: _OldIssuer = proof.old_issuer
    if ((old.invocation_token, old.host_identity, old.operation_id, old.source_hash, old.epoch, old.cluster_uuid)
        != (issuer.invocationToken, issuer.hostIdentity, issuer.operationId, issuer.sourceHash, issuer.epoch, issuer.clusterUuid)
        or proof.logical_cluster_id != issuer.logicalClusterId
        or proof.external.region != issuer.logicalClusterId.split(":", 1)[0]
        or proof.external.unit_name != "knowhere-native-issuer-" + issuer.invocationToken.replace("-", "") + ".service"
        or proof.external.observed_host_sha256 != issuer.hostIdentity
        or proof.external.cgroup_empty is not True or proof.native_issuer_fenced is not True
        or verifiedAt.tzinfo is None or verifiedAt < max(issuer.acquiredAt, reservedAt) or verifiedAt > now):
        raise LexicalIndexError("Operator physical fence does not bind the current owner, native issuer and fresh observation")
    return sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _checkFenceControl(control: LexicalBackendControl, expectedEpoch: int, expectedClusterUuid: str) -> None:
    _checkEpoch(control, expectedEpoch)
    if control.mode != "gated" or control.clusterUuid != expectedClusterUuid:
        raise LexicalIndexError("Physical fencing requires the exact gated backend control identity")


def _getOwnerBindingHash(issuer: LexicalOperatorIssuer) -> str:
    owner: dict[str, object] = _project(issuer)
    data: dict[str, object] = {key: owner[key] for key in _OWNER_BINDING_FIELDS}
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _checkFenceBinding(intent: LexicalOperatorFence, issuer: LexicalOperatorIssuer,
                       control: LexicalBackendControl, expectedSourceHash: str) -> None:
    if (issuer.state != "unknown" or intent.oldInvocationToken != issuer.invocationToken
        or intent.oldBindingSha256 != _getOwnerBindingHash(issuer)
        or intent.sourceHash != expectedSourceHash
        or (intent.epoch, intent.clusterUuid, intent.controlBackend, intent.nodeIncarnation)
        != (control.epoch, control.clusterUuid, control.backend, control.nodeIncarnation)):
        raise LexicalIndexError("Physical fence intent lost its exact old owner/source/control binding")


def _projectFence(intent: LexicalOperatorFence, issuer: LexicalOperatorIssuer) -> dict[str, object]:
    return {"version": 1, "mode": "actual", "status": "reserved" if intent.state == "pending" else intent.state,
        "fence_token": intent.fenceToken, "old_issuer": _project(issuer), "source_hash": intent.sourceHash,
        "epoch": intent.epoch, "cluster_uuid": intent.clusterUuid, "reserved_at_utc": intent.reservedAt.isoformat(),
        "proof_sha256": intent.proofSha256}


class LexicalOperatorAuthority:
    def __init__(self, *, sessionFactory: sessionmaker[Session]) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory

    @contextmanager
    def _lockControl(self) -> Iterator[tuple[Session, LexicalBackendControl]]:
        with self.sessions.begin() as db:
            if not db.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")):
                raise LexicalIndexError("Operator issuer authority requires full private/demo maintenance visibility")
            # These only bound SQL waiting; neither grants expiry-based takeover.
            db.execute(select(func.set_config("lock_timeout", "5000ms", True),
                func.set_config("statement_timeout", "10000ms", True)))
            control = db.scalar(select(LexicalBackendControl).where(LexicalBackendControl.id == 1).with_for_update())
            if control is None:
                raise LexicalIndexError("Operator issuer requires an existing backend control identity")
            yield db, control

    def claim(self, *, logicalClusterId: str, invocationToken: str, hostIdentity: str,
              operationId: str, expectedSourceHash: str, expectedEpoch: int,
              expectedClusterUuid: str, expectedOwnerToken: str | None = None,
              fenceReceipt: dict[str, object] | None = None) -> dict[str, object]:
        _checkIdentity(invocationToken, hostIdentity, operationId)
        _requireText(logicalClusterId, maximum=128, name="logical cluster")
        _requireText(expectedClusterUuid, maximum=64, name="native cluster")
        _checkSource(expectedSourceHash)
        with self._lockControl() as (db, control):
            _checkEpoch(control, expectedEpoch)
            if control.clusterUuid != expectedClusterUuid:
                raise LexicalIndexError("Operator issuer native cluster differs from current control")
            if (db.get(LexicalOperatorInvocation, invocationToken) is not None
                or db.scalar(select(LexicalOperatorFence.fenceToken).where(LexicalOperatorFence.fenceToken == invocationToken)) is not None):
                raise LexicalIndexError("Operator invocation token was already used and cannot acquire authority again")
            issuer = db.scalar(select(LexicalOperatorIssuer).where(LexicalOperatorIssuer.controlId == 1).with_for_update())
            now: datetime = db.scalars(select(func.clock_timestamp())).one()
            proofHash: str | None = None
            if issuer is not None:
                if issuer.logicalClusterId != logicalClusterId:
                    raise LexicalIndexError("Operator control already belongs to another logical cluster key")
                if issuer.invocationToken == invocationToken:
                    raise LexicalIndexError("Operator claim requires a new physical invocation; use assertCurrent within the existing owner")
                if issuer.state in ("active", "unknown"):
                    _checkFenceControl(control, expectedEpoch, expectedClusterUuid)
                    intent = db.get(LexicalOperatorFence, issuer.invocationToken, with_for_update=True)
                    if intent is None or intent.state != "proven":
                        raise LexicalIndexError("Operator takeover requires the current owner's proven durable fence")
                    _checkFenceBinding(intent, issuer, control, expectedSourceHash)
                    proofHash = _checkFence(fenceReceipt, issuer, expectedOwnerToken, now, reservedAt=intent.reservedAt)
                    if proofHash != intent.proofSha256:
                        raise LexicalIndexError("Operator takeover proof differs from the persisted physical fence proof")
                    intent.state = "consumed"
                    intent.consumedAt = now
                elif expectedOwnerToken is not None or fenceReceipt is not None:
                    raise LexicalIndexError("Released operator authority does not require a takeover proof")
            else:
                if expectedOwnerToken is not None or fenceReceipt is not None:
                    raise LexicalIndexError("No previous physical operator exists for takeover")
                issuer = LexicalOperatorIssuer(logicalClusterId=logicalClusterId, controlId=1)
                db.add(issuer)
            issuer.invocationToken = invocationToken
            issuer.hostIdentity = hostIdentity
            issuer.operationId = operationId
            issuer.state = "active"
            issuer.sourceHash = expectedSourceHash
            issuer.epoch = expectedEpoch
            issuer.clusterUuid = expectedClusterUuid
            issuer.acquiredAt = now
            issuer.releasedAt = None
            issuer.fenceReceiptSha256 = proofHash
            db.flush()
            db.add(LexicalOperatorInvocation(invocationToken=invocationToken,
                logicalClusterId=logicalClusterId, recordedAt=now))
            db.flush()
            return _project(issuer)

    def reserveFence(self, *, expectedOwnerReceipt: dict[str, object], fenceToken: str,
                     expectedSourceHash: str, expectedEpoch: int, expectedClusterUuid: str) -> dict[str, object]:
        _checkToken(fenceToken)
        _requireText(expectedClusterUuid, maximum=64, name="native cluster")
        _checkSource(expectedSourceHash)
        if (not isinstance(expectedOwnerReceipt, dict) or type(expectedOwnerReceipt.get("version")) is not int
            or expectedOwnerReceipt.get("version") != 1 or expectedOwnerReceipt.get("mode") != "actual"
            or expectedOwnerReceipt.get("status") not in ("active", "unknown")):
            raise LexicalIndexError("Physical fencing requires the exact actual old issuer receipt")
        with self._lockControl() as (db, control):
            _checkFenceControl(control, expectedEpoch, expectedClusterUuid)
            issuer = db.scalar(select(LexicalOperatorIssuer).where(LexicalOperatorIssuer.controlId == 1).with_for_update())
            if issuer is None or issuer.state not in ("active", "unknown"):
                raise LexicalIndexError("Physical fencing requires an unresolved old issuer")
            actual: dict[str, object] = _project(issuer)
            if any(expectedOwnerReceipt.get(key) != actual[key] for key in _OWNER_BINDING_FIELDS):
                raise LexicalIndexError("Physical fence reservation differs from the exact old issuer")
            if db.get(LexicalOperatorFence, issuer.invocationToken) is not None:
                raise LexicalIndexError("Old issuer already has a durable physical fence; reconcile without reissuing")
            if (db.get(LexicalOperatorInvocation, fenceToken) is not None
                or db.scalar(select(LexicalOperatorFence.fenceToken).where(LexicalOperatorFence.fenceToken == fenceToken)) is not None):
                raise LexicalIndexError("Physical fence token was already used")
            now: datetime = db.scalars(select(func.clock_timestamp())).one()
            intent = LexicalOperatorFence(oldInvocationToken=issuer.invocationToken, fenceToken=fenceToken,
                oldBindingSha256=_getOwnerBindingHash(issuer), sourceHash=expectedSourceHash, epoch=control.epoch,
                clusterUuid=expectedClusterUuid, controlBackend=control.backend, nodeIncarnation=control.nodeIncarnation,
                state="pending", reservedAt=now)
            issuer.state = "unknown"
            db.add(intent)
            db.flush()
            return _projectFence(intent, issuer)

    @contextmanager
    def _lockFence(self, *, fenceToken: str, expectedSourceHash: str,
                   expectedEpoch: int, expectedClusterUuid: str) -> Iterator[tuple[Session, LexicalOperatorIssuer, LexicalOperatorFence]]:
        _checkToken(fenceToken)
        _requireText(expectedClusterUuid, maximum=64, name="native cluster")
        _checkSource(expectedSourceHash)
        with self._lockControl() as (db, control):
            _checkFenceControl(control, expectedEpoch, expectedClusterUuid)
            issuer = db.scalar(select(LexicalOperatorIssuer).where(LexicalOperatorIssuer.controlId == 1).with_for_update())
            if issuer is None:
                raise LexicalIndexError("Physical fence lost its old issuer")
            intent = db.get(LexicalOperatorFence, issuer.invocationToken, with_for_update=True)
            if intent is None or intent.fenceToken != fenceToken or intent.state == "consumed":
                raise LexicalIndexError("Physical fence lost its exact current durable token")
            _checkFenceBinding(intent, issuer, control, expectedSourceHash)
            yield db, issuer, intent

    def assertFenceCurrent(self, *, fenceToken: str, expectedSourceHash: str, expectedEpoch: int,
                           expectedClusterUuid: str) -> dict[str, object]:
        with self._lockFence(fenceToken=fenceToken, expectedSourceHash=expectedSourceHash,
                expectedEpoch=expectedEpoch, expectedClusterUuid=expectedClusterUuid) as (_db, issuer, intent):
            if intent.state != "pending":
                raise LexicalIndexError("A proven physical fence cannot issue another external command")
            return _projectFence(intent, issuer)

    def proveFence(self, *, fenceToken: str, expectedSourceHash: str, expectedEpoch: int,
                   expectedClusterUuid: str, fenceReceipt: dict[str, object]) -> dict[str, object]:
        with self._lockFence(fenceToken=fenceToken, expectedSourceHash=expectedSourceHash,
                expectedEpoch=expectedEpoch, expectedClusterUuid=expectedClusterUuid) as (db, issuer, intent):
            now: datetime = db.scalars(select(func.clock_timestamp())).one()
            proofHash: str = _checkFence(fenceReceipt, issuer, issuer.invocationToken, now, reservedAt=intent.reservedAt)
            if intent.state == "proven":
                if intent.proofSha256 != proofHash:
                    raise LexicalIndexError("A proven physical fence cannot change its recorded proof")
                return _projectFence(intent, issuer)
            intent.state = "proven"
            intent.proofSha256 = proofHash
            intent.provenAt = now
            db.flush()
            return _projectFence(intent, issuer)

    def assertCurrent(self, *, invocationToken: str, hostIdentity: str, operationId: str,
                      expectedSourceHash: str, expectedEpoch: int) -> dict[str, object]:
        _checkIdentity(invocationToken, hostIdentity, operationId)
        _checkSource(expectedSourceHash)
        with self._lockControl() as (db, control):
            _checkEpoch(control, expectedEpoch)
            issuer = _checkOwner(db.scalar(select(LexicalOperatorIssuer).where(
                LexicalOperatorIssuer.controlId == 1).with_for_update()), invocationToken, hostIdentity, operationId)
            if (issuer.state != "active" or issuer.sourceHash != expectedSourceHash
                or issuer.epoch != expectedEpoch or issuer.clusterUuid != control.clusterUuid):
                raise LexicalIndexError("Operator invocation is no longer active at this source/epoch/native identity")
            return _project(issuer)

    def refresh(self, *, invocationToken: str, hostIdentity: str, operationId: str,
                expectedSourceHash: str, expectedPreviousEpoch: int, expectedEpoch: int,
                expectedPreviousClusterUuid: str, expectedClusterUuid: str,
                transitionReceipt: dict[str, object]) -> dict[str, object]:
        _checkIdentity(invocationToken, hostIdentity, operationId)
        _checkSource(expectedSourceHash)
        try:
            proof: _TransitionReceipt = _TransitionReceipt.model_validate(transitionReceipt)
        except ValidationError as error:
            raise LexicalIndexError("Operator refresh requires an actual successful owner-bound transition receipt") from error
        old: _ActiveIssuer = proof.issuer
        if (proof.execute is not True or proof.errors or proof.sourceHash != expectedSourceHash
            or (old.invocation_token, old.host_identity, old.operation_id, old.source_hash, old.epoch, old.cluster_uuid)
            != (invocationToken, hostIdentity, operationId, expectedSourceHash, expectedPreviousEpoch, expectedPreviousClusterUuid)
            or (proof.control.epoch, proof.control.cluster_uuid) != (expectedEpoch, expectedClusterUuid)
            or (proof.verb == "reserve-restore" and proof.status != "reserved")
            or (proof.verb == "verify-restore" and proof.status != "verified")):
            raise LexicalIndexError("Operator refresh receipt does not bind successful old-owner and new-control identities")
        with self._lockControl() as (db, control):
            _checkEpoch(control, expectedEpoch)
            issuer = _checkOwner(db.scalar(select(LexicalOperatorIssuer).where(
                LexicalOperatorIssuer.controlId == 1).with_for_update()), invocationToken, hostIdentity, operationId)
            if (issuer.state != "active" or issuer.sourceHash != expectedSourceHash
                or (issuer.epoch, issuer.clusterUuid) != (expectedPreviousEpoch, expectedPreviousClusterUuid)
                or issuer.logicalClusterId != old.logical_cluster_id
                or issuer.acquiredAt.isoformat() != old.acquired_at_utc
                or control.clusterUuid != expectedClusterUuid
                or (control.backend, control.mode) != (proof.control.backend, proof.control.mode)):
                raise LexicalIndexError("Operator refresh lost its active owner or exact new backend control identity")
            issuer.epoch = expectedEpoch
            issuer.clusterUuid = expectedClusterUuid
            db.flush()
            return _project(issuer)

    def finish(self, *, invocationToken: str, hostIdentity: str, operationId: str,
               isKnownOutcome: bool) -> dict[str, object]:
        _checkIdentity(invocationToken, hostIdentity, operationId)
        if not isinstance(isKnownOutcome, bool):
            raise LexicalIndexError("Operator completion requires an explicit known-outcome decision")
        with self._lockControl() as (db, _control):
            issuer = _checkOwner(db.scalar(select(LexicalOperatorIssuer).where(
                LexicalOperatorIssuer.controlId == 1).with_for_update()), invocationToken, hostIdentity, operationId)
            if issuer.state == "released":
                return _project(issuer)
            if issuer.state == "unknown" and isKnownOutcome:
                raise LexicalIndexError("Unknown physical/native issuer requires fencing before authority can release")
            issuer.state = "released" if isKnownOutcome else "unknown"
            issuer.releasedAt = db.scalars(select(func.clock_timestamp())).one() if isKnownOutcome else None
            db.flush()
            return _project(issuer)
