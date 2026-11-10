"""A restarted native node cannot silently release ownership or resume traffic."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from test_lexical_operator_authority_contract import (
    _SOURCE_HASH, _claim, _getAuthority, issuerRuntime as issuerRuntime,
)


@pytest.mark.parametrize("scenario", ["matched", "foreign-cluster", "foreign-incarnation", "unknown-catalog", "serving"])
def test_gated_restart_matches_same_cluster_and_keeps_uncertain_charge(
        issuerRuntime: tuple[Engine, sessionmaker[Session]], scenario: str) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
    from shared.services.retrieval.lexical.restart_identity import LexicalRestartIdentity

    _, factory = issuerRuntime
    with factory.begin() as database:
        control: LexicalBackendControl = cast(LexicalBackendControl, database.get(LexicalBackendControl, 1))
        control.nodeIncarnation = "old-node"
        database.add(LexicalNamespaceIndex(id="one", ownerId="owner-one", namespace="a", corpusKind="private",
            indexName="knowhere-first", indexUuid="index-one", state="closing", operationId="close-in-doubt"))
        database.add(LexicalNamespaceIndex(id="two", ownerId="owner-two", namespace="b", corpusKind="demo",
            indexName="knowhere-second", indexUuid="index-two", state="closed"))
    authority = _getAuthority(factory)
    owner: dict[str, object] = _claim(authority, str(uuid4()))
    if scenario == "serving":
        with factory.begin() as database:
            cast(LexicalBackendControl, database.get(LexicalBackendControl, 1)).mode = "serving"
    inventory: dict[str, object] = {"version": 1, "cluster_uuid": "cluster-one", "old_incarnation": "old-node",
        "new_incarnation": "new-node", "host_recovery_receipt_sha256": "a" * 64}
    host: dict[str, object] = {"mode": "actual", "status": "recovered-host", "route": "healthy-volume",
        "host": {"active_state": "active", "main_pid": 55}, "fence": {"native_issuer_fenced": True}, "issuer": owner}
    calls: list[str] = []

    def observe() -> NativeLifecycleSnapshot:
        calls.append("native-state")
        indices: dict[str, object] = {name: {"state": "close", "settings": {"index": {"uuid": identity}}}
            for name, identity in (("knowhere-first", "index-one"), ("knowhere-second", "index-two"))}
        if scenario == "unknown-catalog":
            indices["knowhere-foreign"] = {"state": "open", "settings": {"index": {"uuid": "unowned"}}}
        return NativeLifecycleSnapshot.model_validate({"cluster_uuid": "foreign" if scenario == "foreign-cluster" else "cluster-one",
            "nodes": {"node": {"ephemeral_id": "foreign-node" if scenario == "foreign-incarnation" else "new-node"}},
            "metadata": {"indices": indices}, "blocks": {"indices": {name: {"4": {"levels": ["read", "write"]}} for name in indices}}})

    service: LexicalRestartIdentity = LexicalRestartIdentity(sessionFactory=factory, indexPrefix="knowhere", readNative=observe)

    def run() -> dict[str, object]:
        return service.reconcile(inventory, expectedEpoch=7, expectedBackend="opensearch", sourceHash=_SOURCE_HASH,
            issuer=owner, hostReceipt=host, hostReceiptSha256="a" * 64, execute=True)

    if scenario == "matched":
        result: dict[str, object] = run()
        assert result["status"] == "reconciled" and result["service_restored"] is False
        assert result["charged_slots"] == 1 and result["uncertain_indices"] == ["knowhere-first"]
    else:
        with pytest.raises(LexicalIndexError):
            run()
    assert calls == ([] if scenario == "serving" else ["native-state"])
    with factory() as database:
        control = cast(LexicalBackendControl, database.get(LexicalBackendControl, 1))
        first: LexicalNamespaceIndex = cast(LexicalNamespaceIndex, database.get(LexicalNamespaceIndex, "one"))
        second: LexicalNamespaceIndex = cast(LexicalNamespaceIndex, database.get(LexicalNamespaceIndex, "two"))
        assert control.epoch == 7 and control.clusterUuid == "cluster-one"
        assert control.nodeIncarnation == ("new-node" if scenario == "matched" else "old-node")
        assert first.state == ("uncertain" if scenario == "matched" else "closing")
        assert first.operationId == "close-in-doubt" and first.indexUuid == "index-one"
        assert second.state == "closed" and second.indexUuid == "index-two"
