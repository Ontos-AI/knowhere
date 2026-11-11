"""Reviewed operator entrypoint; dry-run by default and immutable JSON receipts."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import TextIO, cast

_ROOT: Path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "packages" / "shared-python"))


def _readObject(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}")
    return cast(dict[str, object], value)


def _getHostIdentity() -> str:
    # The physical Linux issuer must prove its own identity, rather than trust
    # the caller's claimed host. Local component tests inject this boundary.
    machineIdentity: bytes = Path("/etc/machine-id").read_bytes().strip()
    if not machineIdentity:
        raise ValueError("Physical issuer machine identity is unavailable")
    return sha256(machineIdentity).hexdigest()


def _buildParser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "verb",
        choices=(
            "inspect",
            "gate",
            "settle",
            "backfill",
            "seed-manifests",
            "verify",
            "resume",
            "abort",
            "reserve-restore",
            "verify-restore",
            "release-restore",
            "reconcile-restart",
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the explicitly requested operation; default is dry-run",
    )
    parser.add_argument(
        "--target-backend",
        choices=("postgresql", "opensearch"),
        help="Explicit backend required when executing a transition",
    )
    parser.add_argument(
        "--epoch",
        type=int,
        help="Exact expected current control epoch; required for execution",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum revisions repaired in one resumable backfill invocation",
    )
    parser.add_argument(
        "--documents-per-second",
        type=float,
        default=1.0,
        help="Separate maximum backfill write rate",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="Prior immutable receipt; source and target must still match",
    )
    parser.add_argument(
        "--expected-source-hash",
        help="Refuse dispatch unless the current aggregate source matches this reviewed hash",
    )
    parser.add_argument(
        "--issuer-token", help="Current physical invocation UUID; required for mutation"
    )
    parser.add_argument(
        "--issuer-host-sha256", help="SHA256 of this physical host machine-id"
    )
    parser.add_argument(
        "--issuer-operation-id", help="Operation identity bound to the active issuer"
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        help="Reviewed restore plan JSON, with post-restore attestation when applicable",
    )
    parser.add_argument("--operation-id", help="Explicit restore operation UUID")
    parser.add_argument("--host-recovery-receipt", type=Path,
        help="Actual process or healthy-volume recovery receipt bound by the restart inventory")
    parser.add_argument(
        "--reservation",
        type=Path,
        help="Original reserve-restore receipt for verification/release",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="Create a new immutable JSON receipt; never overwrite an existing receipt",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments: argparse.Namespace = _buildParser().parse_args(argv)
    receipt: dict[str, object]
    receiptStream: TextIO | None = None
    sourceHash: str | None = None
    issuer: dict[str, object] | None = None
    observe: Callable[[], dict[str, object]] | None = None
    targetBackend: str = arguments.target_backend or "opensearch"
    try:
        if (
            arguments.execute
            and arguments.verb != "inspect"
            and arguments.epoch is None
        ):
            raise ValueError("Execution requires the exact --epoch")
        if (
            arguments.execute
            and not arguments.verb.endswith("-restore")
            and arguments.verb != "inspect"
            and arguments.target_backend is None
        ):
            raise ValueError(
                "Transition execution requires an explicit --target-backend"
            )
        if arguments.receipt and arguments.receipt.exists():
            raise ValueError(
                "Receipt already exists; choose a new immutable receipt path"
            )
        if arguments.receipt:
            arguments.receipt.parent.mkdir(parents=True, exist_ok=True)
            receiptStream = arguments.receipt.open("x")
        from shared.core.database_sync import get_sync_session_factory
        from shared.services.retrieval.lexical.transition import LexicalTransition

        sourceHash = LexicalTransition.getSourceHash()
        if (
            arguments.expected_source_hash is not None
            and arguments.expected_source_hash != sourceHash
        ):
            raise ValueError(
                "The reviewed source hash differs from the current source; execution was refused"
            )
        if arguments.checkpoint:
            previous: dict[str, object] = _readObject(arguments.checkpoint)
            if previous.get("sourceHash") != sourceHash or previous.get(
                "targetBackend"
            ) not in (None, targetBackend):
                raise ValueError(
                    "Checkpoint source or target differs from this reviewed invocation"
                )
        if arguments.execute and arguments.verb != "inspect":
            if (
                not arguments.issuer_token
                or not arguments.issuer_host_sha256
                or not arguments.issuer_operation_id
            ):
                raise ValueError(
                    "Mutation requires --issuer-token, --issuer-host-sha256 and --issuer-operation-id"
                )
            if arguments.issuer_host_sha256 != _getHostIdentity():
                raise ValueError(
                    "The claimed issuer differs from this physical host identity"
                )
            from shared.services.retrieval.lexical.operator_issuer import (
                LexicalOperatorAuthority,
            )

            issuer = LexicalOperatorAuthority(
                sessionFactory=get_sync_session_factory()
            ).assertCurrent(
                invocationToken=arguments.issuer_token,
                hostIdentity=arguments.issuer_host_sha256,
                operationId=arguments.issuer_operation_id,
                expectedSourceHash=sourceHash,
                expectedEpoch=arguments.epoch,
            )
        if arguments.verb == "reconcile-restart":
            if arguments.inventory is None or arguments.host_recovery_receipt is None or arguments.epoch is None:
                raise ValueError("Restart reconciliation requires --inventory, --host-recovery-receipt and --epoch")
            if arguments.execute and arguments.expected_source_hash is None:
                raise ValueError("Restart reconciliation requires the reviewed --expected-source-hash")
            from shared.core.config import settings
            from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
            from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
            from shared.services.retrieval.lexical.restart_identity import LexicalRestartIdentity
            from shared.services.retrieval.lexical.scope import LexicalScope

            def readRestartNative() -> NativeLifecycleSnapshot:
                native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                    scope=LexicalScope(ownerId="operator-read-only", namespace="restart-reconciliation"),
                    indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
                return native.getLifecycleSnapshot(isExact=False, timeoutSeconds=settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS)

            raw: bytes = arguments.host_recovery_receipt.read_bytes()
            decoded: object = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("Expected a real host recovery receipt")
            receipt = {"version": 1, "sourceHash": sourceHash, "execute": arguments.execute,
                **LexicalRestartIdentity(sessionFactory=get_sync_session_factory(),
                    indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX, readNative=readRestartNative).reconcile(
                    _readObject(arguments.inventory), expectedEpoch=arguments.epoch, expectedBackend=targetBackend,
                    sourceHash=sourceHash, issuer=issuer, hostReceipt=cast(dict[str, object], decoded),
                    hostReceiptSha256=sha256(raw).hexdigest(), execute=arguments.execute)}
        elif arguments.verb.endswith("-restore"):
            if (
                not arguments.inventory
                or not arguments.operation_id
                or arguments.epoch is None
            ):
                raise ValueError(
                    "Restore operations require --inventory, --operation-id and --epoch"
                )
            from shared.services.retrieval.lexical.restore_admission import (
                LexicalRestoreAdmission,
            )

            receipt = LexicalRestoreAdmission(
                sessionFactory=get_sync_session_factory()
            ).run(
                arguments.verb,
                inventory=_readObject(arguments.inventory),
                operationId=arguments.operation_id,
                expectedEpoch=arguments.epoch,
                execute=arguments.execute,
                reservation=_readObject(arguments.reservation)
                if arguments.reservation
                else None,
            )
            receipt = {
                "version": 1,
                "sourceHash": sourceHash,
                "execute": arguments.execute,
                **receipt,
            }
        else:
            transitionService: LexicalTransition = LexicalTransition(
                sessionFactory=get_sync_session_factory()
            )

            def observeState() -> dict[str, object]:
                return transitionService.run("inspect", targetBackend=targetBackend)

            observe = observeState
            receipt = transitionService.run(
                arguments.verb,
                targetBackend=targetBackend,
                execute=arguments.execute,
                expectedEpoch=arguments.epoch,
                limit=arguments.limit,
                documentsPerSecond=arguments.documents_per_second,
            )
    except Exception as error:
        message: str = (
            str(error)
            if isinstance(error, (ValueError, FileNotFoundError))
            or type(error).__name__ == "LexicalIndexError"
            else type(error).__name__
        )
        observed: dict[str, object] = {}
        if observe is not None:
            try:
                observed = observe()
            except Exception:
                pass  # An unavailable/unauthorized observation cannot authorize resumption.
        receipt = {
            **observed,
            "version": 1,
            "verb": arguments.verb,
            "execute": arguments.execute,
            "sourceHash": sourceHash,
            "control": observed.get("control"),
            "currentPublished": observed.get("currentPublished"),
            "errors": [message],
        }
    receipt["expectedSourceHash"] = arguments.expected_source_hash
    receipt["issuer"] = issuer
    receipt["verb"] = arguments.verb
    serialized: str = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if receiptStream is not None:
        try:
            receiptStream.write(serialized)
            receiptStream.flush()
        finally:
            receiptStream.close()
    print(serialized, end="")
    return 1 if receipt.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
