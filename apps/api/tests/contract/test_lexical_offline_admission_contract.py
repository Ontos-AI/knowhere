"""Independent verification targets retain durable capacity across issuer loss."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.contract.test_lexical_operator_authority_contract import (
    issuerRuntime as issuerRuntime,
    _claim,
    _getAuthority,
    _HOST,
)

if TYPE_CHECKING:
    from shared.services.retrieval.lexical.offline_admission import (
        LexicalOfflineAdmission,
    )


def _service(factory: sessionmaker[Session]) -> LexicalOfflineAdmission:
    from shared.services.retrieval.lexical.offline_admission import (
        LexicalOfflineAdmission,
    )

    return LexicalOfflineAdmission(sessionFactory=factory)


def _start(factory: sessionmaker[Session]) -> dict[str, object]:
    return _claim(_getAuthority(factory), str(uuid4()))


def _plan(
    *,
    operation: str | None = None,
    original: str = "knowhere-original",
    incarnation: str = "offline-node",
) -> dict[str, object]:
    identity: str = operation or str(uuid4())
    return {
        "version": 1,
        "purpose": "offline-backup-verification",
        "source_cluster_uuid": "cluster-one",
        "target_cluster_uuid": "offline-cluster",
        "target_incarnation": incarnation,
        "snapshot_uuid": "snapshot",
        "inventory_sha256": "a" * 64,
        "boundary_sha256": "b" * 64,
        "operation_id": identity,
        "batch_reservation_id": str(uuid4()),
        "indices": [
            {
                "original_index_name": original,
                "index_name": "verify-" + identity.replace("-", "") + "-" + original,
            }
        ],
    }


def _indices(proof: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], proof["indices"])


def _observed(proof: dict[str, object]) -> dict[str, str]:
    return {str(row["index_name"]): "restored-uuid" for row in _indices(proof)}


def _verified(proof: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "status": "verified",
        "data_verified": True,
        "errors": [],
        **{
            key: proof[key]
            for key in (
                "snapshot_uuid",
                "inventory_sha256",
                "boundary_sha256",
                "target_cluster_uuid",
                "target_incarnation",
                "operation_id",
                "batch_reservation_id",
            )
        },
        "selected_indices": [row["original_index_name"] for row in _indices(proof)],
    }


def test_unknown_old_incarnation_counts_even_when_absent_from_native(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    from shared.models.database.lexicalofflinereservation import (
        LexicalOfflineReservation,
    )

    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    old = service.reserve(
        _plan(original="old-unknown", incarnation="old-incarnation"),
        nativeOpenNames=(),
        issuer=issuer,
    )
    service.retainUnknown(old, issuer=issuer)
    with factory.begin() as db:
        row = db.scalars(select(LexicalOfflineReservation)).one()
        row.updatedAt = datetime(1900, 1, 1, tzinfo=timezone.utc)
    native = tuple("other-open-" + str(index) for index in range(398))
    reserved = service.reserve(_plan(), nativeOpenNames=native, issuer=issuer)
    assert reserved["charged_after"] == 400 and reserved["status"] == "reserved"
    assert reserved["charged_before"] == 399 and reserved["additional_slots"] == 1
    with pytest.raises(ValueError, match="400"):
        service.reserve(
            _plan(original="another"), nativeOpenNames=native, issuer=issuer
        )


def test_unknown_or_missing_clone_never_releases_without_acknowledged_exact_uuid(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    from shared.models.database.lexicalofflinereservation import (
        LexicalOfflineReservation,
    )

    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    reserved = service.reserve(_plan(), nativeOpenNames=(), issuer=issuer)
    unknown = service.retainUnknown(reserved, issuer=issuer)
    assert unknown["status"] == "unknown" and unknown["charged_after"] == 1
    with pytest.raises(ValueError):
        service.release(
            unknown, observedClosed={}, closeAcknowledged=True, issuer=issuer
        )
    with factory() as db:
        assert db.scalars(select(LexicalOfflineReservation)).one().state == "uncertain"
    with pytest.raises(ValueError, match="existing|repeat"):
        service.reserve(
            _plan(operation=str(reserved["operation_id"])),
            nativeOpenNames=(),
            issuer=issuer,
        )


def test_verified_ready_build_requires_new_close_fence_and_ack_before_release(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    from shared.models.database.lexicalofflinereservation import (
        LexicalOfflineReservation,
    )

    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    reserved = service.reserve(
        _plan(),
        nativeOpenNames=("existing-open", ".opendistro_security"),
        issuer=issuer,
    )
    ready = service.recordReady(
        reserved, observedIndexes=_observed(reserved), issuer=issuer
    )
    assert ready["status"] == "ready" and ready["charged_after"] == 2
    with pytest.raises(ValueError, match="payload"):
        service.beginClose(
            ready,
            verificationReceipt={"status": "SUCCESS", "catalog_verified": True},
            issuer=issuer,
        )
    closing = service.beginClose(
        ready, verificationReceipt=_verified(ready), issuer=issuer
    )
    assert (
        closing["status"] == "closing"
        and closing["batch_reservation_id"] != ready["batch_reservation_id"]
    )
    with pytest.raises(ValueError):
        service.beginClose(ready, verificationReceipt=_verified(ready), issuer=issuer)
    unknown = service.release(
        closing,
        observedClosed=_observed(closing),
        closeAcknowledged=False,
        issuer=issuer,
    )
    assert unknown["status"] == "unknown" and unknown["charged_after"] == 2
    closed = service.release(
        closing,
        observedClosed=_observed(closing),
        closeAcknowledged=True,
        issuer=issuer,
    )
    assert closed["status"] == "closed" and closed["charged_after"] == 1
    with factory() as db:
        row = db.scalars(select(LexicalOfflineReservation)).one()
        assert row.state == "closed" and row.indexUuid == "restored-uuid"
    assert service.retainUnknown(closed, issuer=issuer)["status"] == "closed"


@pytest.mark.parametrize(
    "field",
    ["snapshot_uuid", "boundary_sha256", "batch_reservation_id", "target_incarnation"],
)
def test_unrelated_payload_proof_cannot_authorize_close(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], field: str
) -> None:
    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    reserved = service.reserve(_plan(), nativeOpenNames=(), issuer=issuer)
    ready = service.recordReady(
        reserved, observedIndexes=_observed(reserved), issuer=issuer
    )
    verification = _verified(ready)
    verification[field] = "wrong-binding"
    with pytest.raises(ValueError, match="payload"):
        service.beginClose(ready, verificationReceipt=verification, issuer=issuer)


def test_wrong_native_uuid_is_retained_after_verified_close(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    reserved = service.reserve(_plan(), nativeOpenNames=(), issuer=issuer)
    ready = service.recordReady(
        reserved, observedIndexes=_observed(reserved), issuer=issuer
    )
    closing = service.beginClose(
        ready, verificationReceipt=_verified(ready), issuer=issuer
    )
    wrong = {name: "replacement-uuid" for name in _observed(closing)}
    assert (
        service.release(
            closing, observedClosed=wrong, closeAcknowledged=True, issuer=issuer
        )["status"]
        == "unknown"
    )


def test_new_physical_issuer_cannot_adopt_same_operation_unknown_reservation(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    proof = service.reserve(_plan(), nativeOpenNames=(), issuer=issuer)
    service.retainUnknown(proof, issuer=issuer)
    authority = _getAuthority(factory)
    authority.finish(
        invocationToken=str(issuer["invocation_token"]),
        hostIdentity=_HOST,
        operationId="restore-operation",
        isKnownOutcome=True,
    )
    replacement = _claim(authority, str(uuid4()), hostIdentity="b" * 64)
    forged = {**proof, "issuer_token": replacement["invocation_token"]}
    with pytest.raises(ValueError, match="issuer"):
        service.recordReady(
            forged, observedIndexes=_observed(proof), issuer=replacement
        )


@pytest.mark.parametrize(
    "field,value",
    [("source_hash", "0" * 64), ("epoch", 8), ("host_identity", "b" * 64)],
)
def test_reservation_rechecks_current_source_epoch_and_physical_owner(
    issuerRuntime: tuple[Engine, sessionmaker[Session]], field: str, value: object
) -> None:
    _, factory = issuerRuntime
    issuer = {**_start(factory), field: value}
    with pytest.raises(ValueError):
        _service(factory).reserve(_plan(), nativeOpenNames=(), issuer=issuer)


def test_concurrent_batches_cannot_double_spend_last_target_slot(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    operation = str(uuid4())
    plans = [
        _plan(operation=operation, original="first"),
        _plan(operation=operation, original="second"),
    ]
    native = tuple("open-" + str(index) for index in range(399))

    def reserve(plan: dict[str, object]) -> str:
        try:
            return str(
                service.reserve(plan, nativeOpenNames=native, issuer=issuer)["status"]
            )
        except ValueError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, plans)) == ["refused", "reserved"]


def test_changed_ready_uuid_cannot_authorize_a_different_physical_close(
    issuerRuntime: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = issuerRuntime
    issuer = _start(factory)
    service = _service(factory)
    reserved = service.reserve(_plan(), nativeOpenNames=(), issuer=issuer)
    ready = service.recordReady(
        reserved, observedIndexes=_observed(reserved), issuer=issuer
    )
    changed = {
        **ready,
        "indices": [
            {**row, "index_uuid": "another-physical-index"} for row in _indices(ready)
        ],
    }
    with pytest.raises(ValueError, match="identity"):
        service.beginClose(
            changed, verificationReceipt=_verified(changed), issuer=issuer
        )
