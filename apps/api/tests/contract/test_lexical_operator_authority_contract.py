"""Actual SQL ownership excludes concurrent physical issuers without a TTL."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import (
    PostgreSQLProcess, clear_application_modules, configure_contract_environment,
    get_contract_database_url, prepare_contract_storage,
)

if TYPE_CHECKING:
    from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority

_SOURCE_HASH: str = "f" * 64
_HOST: str = "a" * 64
_OTHER_HOST: str = "b" * 64
_LOGICAL_CLUSTER: str = "us-east-1:reviewed-logical-cluster"


@pytest.fixture
def issuerRuntime(monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.transition import LexicalTransition
    monkeypatch.setattr(LexicalTransition, "getSourceHash", staticmethod(lambda: _SOURCE_HASH))
    engine: Engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    factory: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        control = db.get(LexicalBackendControl, 1)
        if control is None:
            control = LexicalBackendControl(id=1, backend="opensearch", mode="gated", epoch=7, clusterUuid="cluster-one")
            db.add(control)
        else:
            control.backend = "opensearch"
            control.mode = "gated"
            control.epoch = 7
            control.clusterUuid = "cluster-one"
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _getAuthority(factory: sessionmaker[Session]) -> LexicalOperatorAuthority:
    path: Path = Path(__file__).resolve().parents[4] / "packages/shared-python/shared/services/retrieval/lexical/operator_issuer.py"
    assert path.is_file(), "Physical operator authority must be implemented before executable driver mutation"
    from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority
    return LexicalOperatorAuthority(sessionFactory=factory)


def _claim(authority: LexicalOperatorAuthority, token: str, *, logicalClusterId: str = _LOGICAL_CLUSTER,
           hostIdentity: str = _HOST, expectedSourceHash: str = _SOURCE_HASH, expectedEpoch: int = 7,
           expectedClusterUuid: str = "cluster-one", expectedOwnerToken: str | None = None,
           fenceReceipt: dict[str, object] | None = None) -> dict[str, object]:
    return authority.claim(logicalClusterId=logicalClusterId, invocationToken=token,
        hostIdentity=hostIdentity, operationId="restore-operation", expectedSourceHash=expectedSourceHash,
        expectedEpoch=expectedEpoch, expectedClusterUuid=expectedClusterUuid,
        expectedOwnerToken=expectedOwnerToken, fenceReceipt=fenceReceipt)


def _fence(receipt: dict[str, object]) -> dict[str, object]:
    token: str = str(receipt["invocation_token"])
    return {"version": 1, "mode": "actual", "status": "fenced", "driver": "aws-ssm-systemd-v1",
        "logical_cluster_id": _LOGICAL_CLUSTER, "old_issuer": {
            key: receipt[key] for key in ("invocation_token", "host_identity", "operation_id", "source_hash", "epoch", "cluster_uuid")},
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "external": {"account_id": "123456789012", "region": "us-east-1", "instance_id": "i-0123456789abcdef0",
            "command_id": str(uuid4()), "document_name": "AWS-RunShellScript", "status": "Success", "response_code": 0,
            "unit_name": "knowhere-native-issuer-" + token.replace("-", "") + ".service",
            "observed_host_sha256": receipt["host_identity"], "active_state": "inactive", "main_pid": 0, "cgroup_empty": True},
        "native_issuer_fenced": True, "native_fence": {"instance_id": "i-1123456789abcdef0", "command_id": str(uuid4()),
            "status": "Success", "response_code": 0, "unit": "knowhere-opensearch.service", "active_state": "inactive", "main_pid": 0}}


def _reserveFence(authority: LexicalOperatorAuthority, owner: dict[str, object], token: str) -> dict[str, object]:
    return authority.reserveFence(expectedOwnerReceipt=owner, fenceToken=token, expectedSourceHash=_SOURCE_HASH,
        expectedEpoch=7, expectedClusterUuid="cluster-one")


def _proveFence(authority: LexicalOperatorAuthority, token: str, proof: dict[str, object]) -> dict[str, object]:
    return authority.proveFence(fenceToken=token, expectedSourceHash=_SOURCE_HASH,
        expectedEpoch=7, expectedClusterUuid="cluster-one", fenceReceipt=proof)


def test_authority_excludes_other_host_even_with_same_logical_operation(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    receipt = _claim(authority, token)
    assert receipt["status"] == "active" and receipt["invocation_token"] == token
    with pytest.raises(LexicalIndexError):
        _claim(authority, str(uuid4()), hostIdentity=_OTHER_HOST)
    with pytest.raises(LexicalIndexError):
        _claim(authority, str(uuid4()), logicalClusterId="us-east-1:alternate-key")
    assert authority.assertCurrent(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
        expectedSourceHash=_SOURCE_HASH, expectedEpoch=7)["status"] == "active"


def test_authority_claim_is_single_use_and_releases_only_known_outcome(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    _claim(authority, token)
    with pytest.raises(LexicalIndexError):
        _claim(authority, token)
    with pytest.raises(LexicalIndexError):
        _claim(authority, token, hostIdentity=_OTHER_HOST)
    assert authority.finish(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation", isKnownOutcome=True)["status"] == "released"
    with pytest.raises(LexicalIndexError):
        authority.assertCurrent(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
            expectedSourceHash=_SOURCE_HASH, expectedEpoch=7)
    assert _claim(authority, str(uuid4()), hostIdentity=_OTHER_HOST)["status"] == "active"


@pytest.mark.parametrize("spelling", ["canonical", "uppercase", "compact"])
def test_historical_invocation_token_cannot_reclaim_after_another_owner_released(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], spelling: str,
) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    first: str = str(uuid4())
    second: str = str(uuid4())
    for token in (first, second):
        _claim(authority, token)
        authority.finish(invocationToken=token, hostIdentity=_HOST,
            operationId="restore-operation", isKnownOutcome=True)
    replay: str = first.upper() if spelling == "uppercase" else first.replace("-", "") if spelling == "compact" else first
    with pytest.raises(LexicalIndexError):
        _claim(authority, replay)


@pytest.mark.parametrize("doesFailReset", [False, True])
def test_isolated_contract_reset_restores_ledger_guards_even_after_failure(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], doesFailReset: bool,
) -> None:
    from shared.testing.contract_runtime import reset_contract_database
    engine, factory = issuerRuntime
    token: str = str(uuid4())
    _claim(_getAuthority(factory), token)

    def failTruncate(_connection: object, _cursor: object, statement: str,
                     _parameters: object, _context: object, _doesExecuteMany: bool) -> None:
        if statement.startswith("TRUNCATE TABLE"):
            raise RuntimeError("Injected isolated reset failure")

    if doesFailReset:
        event.listen(Engine, "before_cursor_execute", failTruncate)
    try:
        if doesFailReset:
            with pytest.raises(RuntimeError, match="Injected"):
                asyncio.run(reset_contract_database())
        else:
            asyncio.run(reset_contract_database())
    finally:
        if doesFailReset:
            event.remove(Engine, "before_cursor_execute", failTruncate)
    with engine.connect() as connection:
        guards = connection.execute(text("SELECT tgenabled FROM pg_trigger WHERE tgrelid='lexical_operator_invocations'::regclass AND NOT tgisinternal")).scalars().all()
        assert guards == ["O", "O"]
        assert connection.scalar(text("SELECT count(*) FROM lexical_operator_invocations")) == int(doesFailReset)
    with engine.begin() as connection, pytest.raises(DBAPIError, match="append-only"):
        connection.execute(text("TRUNCATE lexical_operator_invocations CASCADE"))


def test_unknown_authority_has_no_age_based_takeover_or_known_finish_escape(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    _claim(authority, token)
    assert authority.finish(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation", isKnownOutcome=False)["status"] == "unknown"
    with factory.begin() as db:
        db.scalars(select(LexicalOperatorIssuer)).one().acquiredAt = datetime.now(timezone.utc) - timedelta(days=365)
    with pytest.raises(LexicalIndexError):
        _claim(authority, str(uuid4()))
    with pytest.raises(LexicalIndexError):
        authority.finish(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation", isKnownOutcome=True)


def test_actual_fence_takeover_requires_exact_old_owner_and_records_proof_hash(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    receipt = _claim(authority, token)
    authority.finish(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation", isKnownOutcome=False)
    fenceToken: str = str(uuid4())
    assert _reserveFence(authority, receipt, fenceToken)["status"] == "reserved"
    proof = _fence(receipt)
    assert _proveFence(authority, fenceToken, proof)["status"] == "proven"
    with pytest.raises(LexicalIndexError):
        _claim(authority, str(uuid4()), expectedOwnerToken=str(uuid4()), fenceReceipt=proof)
    current = _claim(authority, str(uuid4()), hostIdentity=_OTHER_HOST, expectedOwnerToken=token, fenceReceipt=proof)
    assert current["status"] == "active" and current["host_identity"] == _OTHER_HOST
    assert current["fence_receipt_sha256"] == sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(LexicalIndexError):
        authority.finish(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation", isKnownOutcome=True)


@pytest.mark.parametrize("damage", ["dry-run", "wrong-host", "live-pid", "nonempty-cgroup", "wrong-unit", "missing-native", "future", "before-claim", "boolean-version"])
def test_takeover_rejects_incomplete_or_unbound_physical_fence(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], damage: str,
) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    owner = _claim(authority, token)
    fenceToken: str = str(uuid4())
    _reserveFence(authority, owner, fenceToken)
    proof = _fence(owner)
    if damage == "dry-run":
        proof["mode"] = "dry-run"
    elif damage == "wrong-host":
        cast(dict[str, object], proof["old_issuer"])["host_identity"] = _OTHER_HOST
    elif damage == "live-pid":
        cast(dict[str, object], proof["external"])["main_pid"] = 1234
    elif damage == "nonempty-cgroup":
        cast(dict[str, object], proof["external"])["cgroup_empty"] = False
    elif damage == "wrong-unit":
        cast(dict[str, object], proof["external"])["unit_name"] = "unrelated.service"
    elif damage == "missing-native":
        proof["native_fence"] = None
    elif damage == "future":
        proof["verified_at_utc"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    elif damage == "boolean-version":
        proof["version"] = True
    else:
        proof["verified_at_utc"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(LexicalIndexError):
        _proveFence(authority, fenceToken, proof)


def test_unpersisted_physical_proof_cannot_take_over_even_when_structurally_valid(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    proof = _fence(_claim(authority, token))
    with pytest.raises(LexicalIndexError, match="proven durable fence"):
        _claim(authority, str(uuid4()), expectedOwnerToken=token, fenceReceipt=proof)


def test_concurrent_physical_fences_admit_exactly_one_intent_and_stop_old_issuer(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    from threading import Barrier
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    old = _claim(authority, token)
    barrier: Barrier = Barrier(2)

    def reserve(fenceToken: str) -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            return str(_reserveFence(authority, old, fenceToken)["status"]), fenceToken
        except LexicalIndexError:
            return "refused", fenceToken

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [str(uuid4()), str(uuid4())]))
    assert sorted(status for status, _token in results) == ["refused", "reserved"]
    accepted: str = next(fence for status, fence in results if status == "reserved")
    assert authority.assertFenceCurrent(fenceToken=accepted, expectedSourceHash=_SOURCE_HASH,
        expectedEpoch=7, expectedClusterUuid="cluster-one")["status"] == "reserved"
    with pytest.raises(LexicalIndexError):
        authority.assertCurrent(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
            expectedSourceHash=_SOURCE_HASH, expectedEpoch=7)


def test_unknown_fence_survives_disconnect_and_only_exact_proof_can_settle_and_consume(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.error import LexicalIndexError
    engine, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    owner = _claim(authority, token)
    stale = _fence(owner)
    fenceToken: str = str(uuid4())
    _reserveFence(authority, owner, fenceToken)
    # No transaction/connection remains held over the physical operation.
    with factory.begin() as db:
        assert db.scalar(select(LexicalBackendControl.id).with_for_update(nowait=True)) == 1
    engine.dispose()
    authority = _getAuthority(factory)
    with pytest.raises(LexicalIndexError):
        _reserveFence(authority, owner, str(uuid4()))
    proof = _fence(owner)
    with pytest.raises(LexicalIndexError, match="proven durable fence"):
        _claim(authority, str(uuid4()), expectedOwnerToken=token, fenceReceipt=proof)
    with pytest.raises(LexicalIndexError):
        _proveFence(authority, fenceToken, stale)
    with pytest.raises(LexicalIndexError):
        _proveFence(authority, str(uuid4()), proof)
    proven = _proveFence(authority, fenceToken, proof)
    assert _proveFence(authority, fenceToken, proof) == proven
    with pytest.raises(LexicalIndexError):
        _claim(authority, str(uuid4()), expectedOwnerToken=token, fenceReceipt=_fence(owner))
    replacement: str = str(uuid4())
    assert _claim(authority, replacement, expectedOwnerToken=token, fenceReceipt=proof)["status"] == "active"
    with pytest.raises(LexicalIndexError):
        _proveFence(authority, fenceToken, proof)
    authority.finish(invocationToken=replacement, hostIdentity=_HOST,
        operationId="restore-operation", isKnownOutcome=True)
    with pytest.raises(LexicalIndexError, match="already used"):
        _claim(authority, fenceToken)


@pytest.mark.parametrize("drift", ["mode", "incarnation"])
def test_durable_fence_refuses_commands_and_proof_after_control_drift(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], drift: str,
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    owner = _claim(authority, str(uuid4()))
    fenceToken: str = str(uuid4())
    _reserveFence(authority, owner, fenceToken)
    with factory.begin() as db:
        control = db.scalars(select(LexicalBackendControl)).one()
        if drift == "mode":
            control.mode = "serving"
        else:
            control.nodeIncarnation = "changed-node"
    with pytest.raises(LexicalIndexError):
        authority.assertFenceCurrent(fenceToken=fenceToken, expectedSourceHash=_SOURCE_HASH,
            expectedEpoch=7, expectedClusterUuid="cluster-one")
    with pytest.raises(LexicalIndexError):
        _proveFence(authority, fenceToken, _fence(owner))


def test_concurrent_claims_allow_exactly_one_physical_issuer(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from threading import Barrier
    from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    barrier: Barrier = Barrier(2)

    def claimPhysicalIssuer(token: str) -> str:
        barrier.wait(timeout=5)
        try:
            return str(_claim(authority, token)["status"])
        except LexicalIndexError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results: list[str] = list(pool.map(claimPhysicalIssuer, [str(uuid4()), str(uuid4())]))
    assert sorted(results) == ["active", "refused"]
    with factory() as db:
        assert len(db.scalars(select(LexicalOperatorIssuer)).all()) == 1


def test_issuer_requires_actual_maintenance_visibility(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    engine, _factory = issuerRuntime
    role: str = "issuer_contract_" + uuid4().hex
    with engine.connect() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        connection.commit()
        try:
            connection.execute(text(f'SET ROLE "{role}"'))
            connection.commit()
            authority = _getAuthority(sessionmaker(connection, expire_on_commit=False))
            with pytest.raises(LexicalIndexError, match="maintenance visibility"):
                _claim(authority, str(uuid4()))
        finally:
            connection.rollback()
            connection.execute(text("RESET ROLE"))
            connection.execute(text(f'DROP ROLE "{role}"'))
            connection.commit()


@pytest.mark.parametrize("drift", ["source", "epoch", "cluster"])
def test_current_authority_rejects_source_epoch_and_native_identity_drift(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch, drift: str,
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.transition import LexicalTransition
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    _claim(authority, token)
    if drift == "source":
        monkeypatch.setattr(LexicalTransition, "getSourceHash", staticmethod(lambda: "e" * 64))
    else:
        with factory.begin() as db:
            control = db.scalars(select(LexicalBackendControl)).one()
            if drift == "epoch":
                control.epoch = 8
            else:
                control.clusterUuid = "new-native-cluster"
    with pytest.raises(LexicalIndexError):
        authority.assertCurrent(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
            expectedSourceHash=_SOURCE_HASH, expectedEpoch=7)
    # Releasing one's own completed work remains possible after a control transition.
    assert authority.finish(invocationToken=token, hostIdentity=_HOST,
        operationId="restore-operation", isKnownOutcome=True)["status"] == "released"


def _advanceControl(factory: sessionmaker[Session], previous: dict[str, object]) -> dict[str, object]:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    with factory.begin() as db:
        control = db.scalars(select(LexicalBackendControl)).one()
        control.epoch = 8
        control.clusterUuid = "restored-native-cluster"
    return {"version": 1, "verb": "verify-restore", "sourceHash": _SOURCE_HASH,
        "execute": True, "errors": [], "issuer": previous, "status": "verified",
        "control": {"backend": "opensearch", "mode": "gated", "epoch": 8, "cluster_uuid": "restored-native-cluster"}}


def _refresh(authority: LexicalOperatorAuthority, token: str, transition: dict[str, object]) -> dict[str, object]:
    return authority.refresh(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
        expectedSourceHash=_SOURCE_HASH, expectedPreviousEpoch=7, expectedEpoch=8,
        expectedPreviousClusterUuid="cluster-one", expectedClusterUuid="restored-native-cluster",
        transitionReceipt=transition)


def test_owner_can_refresh_only_after_receipted_control_transition(issuerRuntime: tuple[Engine, sessionmaker[Session]]) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    previous = _claim(authority, token)
    transition = _advanceControl(factory, previous)
    current = _refresh(authority, token, transition)
    assert current["epoch"] == 8 and current["cluster_uuid"] == "restored-native-cluster"
    assert current["acquired_at_utc"] == previous["acquired_at_utc"]
    assert authority.assertCurrent(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
        expectedSourceHash=_SOURCE_HASH, expectedEpoch=8)["status"] == "active"
    with pytest.raises(LexicalIndexError):
        _refresh(authority, token, transition)


@pytest.mark.parametrize("verb", ["gate", "resume", "abort", "reserve-restore"])
def test_owner_refresh_supports_transition_epochs_and_same_epoch_restore_adoption(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], verb: str,
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    previous = _claim(authority, token)
    isRestore: bool = verb == "reserve-restore"
    epoch: int = 7 if isRestore else 8
    cluster: str = "restored-native-cluster" if isRestore else "cluster-one"
    mode: str = "serving" if verb in ("resume", "abort") else "gated"
    with factory.begin() as db:
        control = db.scalars(select(LexicalBackendControl)).one()
        control.epoch, control.clusterUuid, control.mode = epoch, cluster, mode
    transition: dict[str, object] = {"version": 1, "verb": verb, "sourceHash": _SOURCE_HASH,
        "execute": True, "errors": [], "issuer": previous,
        "control": {"backend": "opensearch", "mode": mode, "epoch": epoch, "cluster_uuid": cluster}}
    if isRestore:
        transition["status"] = "reserved"
    current = authority.refresh(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
        expectedSourceHash=_SOURCE_HASH, expectedPreviousEpoch=7, expectedEpoch=epoch,
        expectedPreviousClusterUuid="cluster-one", expectedClusterUuid=cluster, transitionReceipt=transition)
    assert current["epoch"] == epoch and current["cluster_uuid"] == cluster
    assert current["acquired_at_utc"] == previous["acquired_at_utc"]


@pytest.mark.parametrize("damage", ["dry-run", "errors", "source", "owner", "old-epoch", "new-epoch", "new-cluster", "verb", "unknown", "released", "unknown-native", "retained-reservation"])
def test_refresh_cannot_authorize_unreceipted_or_foreign_transition(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], damage: str,
) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    _, factory = issuerRuntime
    authority = _getAuthority(factory)
    token: str = str(uuid4())
    previous = _claim(authority, token)
    transition = _advanceControl(factory, previous)
    if damage == "dry-run":
        transition["execute"] = False
    elif damage == "errors":
        transition["errors"] = ["incomplete"]
    elif damage == "source":
        transition["sourceHash"] = "e" * 64
    elif damage == "owner":
        previous["invocation_token"] = str(uuid4())
    elif damage == "old-epoch":
        previous["epoch"] = 6
    elif damage == "new-epoch":
        cast(dict[str, object], transition["control"])["epoch"] = 9
    elif damage == "new-cluster":
        cast(dict[str, object], transition["control"])["cluster_uuid"] = "unreviewed-cluster"
    elif damage == "verb":
        transition["verb"] = "inspect"
    elif damage == "unknown-native":
        transition["status"] = "unknown"
    elif damage == "retained-reservation":
        transition["verb"] = "reserve-restore"
        transition["status"] = "retained"
    else:
        authority.finish(invocationToken=token, hostIdentity=_HOST, operationId="restore-operation",
            isKnownOutcome=damage == "released")
    with pytest.raises(LexicalIndexError):
        _refresh(authority, token, transition)
