"""Offline capacity includes durable unknown reservations across physical hosts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest


def loadAdmission() -> type:
    path: Path = Path(__file__).parents[3] / "packages/shared-python/shared/services/retrieval/lexical/offline_admission.py"
    assert path.is_file(), "Shared offline-target reservation authority is absent"
    specification = importlib.util.spec_from_file_location("offline_admission_policy", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.LexicalOfflineAdmission)


def test_union_counts_unknown_invisible_reservations_once_and_never_exceeds_400() -> None:
    admission: type = loadAdmission()
    native: tuple[str, ...] = tuple("open-" + str(index) for index in range(398))
    result = admission.planCharges(native, ("open-0", "unknown-old-host"), ("new-one",))
    assert result["charged_after"] == 400
    with pytest.raises(ValueError, match="400"):
        admission.planCharges(native, ("unknown-old-host",), ("new-one", "new-two"))


def test_same_operation_different_physical_issuer_cannot_adopt_old_reservation() -> None:
    admission: type = loadAdmission()
    record: dict[str, object] = {"operation_id": "same-operation", "issuer_token": "old-physical-host", "snapshot_uuid": "snapshot-one",
        "inventory_sha256": "a" * 64, "boundary_sha256": "b" * 64, "target_incarnation": "target-one"}
    proof: dict[str, object] = {**record, "issuer_token": "new-physical-host"}
    with pytest.raises(ValueError, match="issuer"):
        admission.assertReservationOwner(record, proof)


def test_catalog_success_cannot_transition_reservation_to_close_without_payload_proof() -> None:
    admission: type = loadAdmission()
    proof: dict[str, object] = {"snapshot_uuid": "snapshot-one", "inventory_sha256": "a" * 64,
        "boundary_sha256": "b" * 64, "indices": [{"original_index_name": "original"}]}
    with pytest.raises(ValueError, match="payload"):
        admission.assertPayloadVerification(proof, {"status": "SUCCESS", "catalog_verified": True})
