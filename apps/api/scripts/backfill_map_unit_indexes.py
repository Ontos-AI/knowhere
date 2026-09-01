"""Backfill persisted MAP-NAV lexical indexes for existing revisions.

Rebuilds, per active revision: the map-unit index, the revision serving
manifest, that document's subtree in the namespace MAP snapshot, and the
namespace generation. The migrations that create these
derived tables leave them empty intentionally. Run this command after
deployment with ``--apply`` so each revision is rebuilt and committed
independently; without ``--apply`` it is a read-only inventory.

Use ``--check`` after backfill to verify whether query-time snapshot
fallbacks (manifest_merge / table_scan) would still fire, and whether
map-unit indexes are complete for scoring.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


def _resolve_shared_root(api_root: Path) -> Path:
    """Resolve the shared package in source checkouts and runtime images."""
    runtime_shared_root = api_root / "packages" / "shared-python"
    if runtime_shared_root.is_dir():
        return runtime_shared_root

    repository_shared_root = api_root.parents[1] / "packages" / "shared-python"
    if repository_shared_root.is_dir():
        return repository_shared_root

    raise RuntimeError(f"Could not locate shared-python package from {api_root}")


def _bootstrap_python_path() -> None:
    api_root = Path(__file__).resolve().parents[1]
    shared_root = _resolve_shared_root(api_root)
    for path in (api_root, shared_root):
        value = os.fspath(path)
        if value not in sys.path:
            sys.path.insert(0, value)


_bootstrap_python_path()

from sqlalchemy import select

from shared.core.database_sync import get_sync_session_factory
from shared.models.database.document import (
    Document,
    DocumentMapUnitIndex,
    RetrievalNamespaceMapSnapshot,
    RetrievalServingRevisionManifest,
)
from shared.services.retrieval.map_unit_index import replace_document_map_units
from shared.services.retrieval.namespace_map_snapshot import (
    patch_namespace_map_snapshot,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.serving_generation import (
    advance_namespace_generation,
    lock_namespace_generation,
)
from shared.services.retrieval.serving_manifest import (
    decode_namespace_map_snapshot,
    persist_revision_serving_state,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill MAP-NAV indexes for current document revisions."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Build and commit each current revision index.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only: report whether snapshot fallbacks would still fire "
            "and whether map-unit indexes are complete. Exit 1 if not ready."
        ),
    )
    parser.add_argument(
        "--document-id", default="", help="Limit the backfill to one document."
    )
    return parser


def _load_documents(document_id: str) -> list[Document]:
    session_factory = get_sync_session_factory()
    with session_factory() as db:
        statement = (
            select(Document)
            .where(Document.status == "active")
            .where(Document.current_job_result_id.is_not(None))
        )
        normalized_document_id = document_id.strip()
        if normalized_document_id:
            statement = statement.where(Document.document_id == normalized_document_id)
        return list(db.scalars(statement).all())


@dataclass(frozen=True)
class NamespaceFallbackReport:
    user_id: str
    namespace: str
    active_docs: int
    snapshot_status: str
    missing_from_snapshot: int
    missing_map_index: int
    missing_revision_manifest: int
    suspicious_zero_idf: int
    would_hit_snapshot_fallback: bool
    scoring_incomplete: bool

    @property
    def ready(self) -> bool:
        return not self.would_hit_snapshot_fallback and not self.scoring_incomplete


def check_fallback_readiness(*, document_id: str = "") -> list[NamespaceFallbackReport]:
    """Inspect active revisions for snapshot coverage and map-unit indexes."""
    documents = _load_documents(document_id)
    by_scope: dict[tuple[str, str], list[Document]] = defaultdict(list)
    for document in documents:
        by_scope[(document.user_id, document.namespace)].append(document)

    session_factory = get_sync_session_factory()
    reports: list[NamespaceFallbackReport] = []
    with session_factory() as db:
        for (user_id, namespace), scoped_docs in sorted(
            by_scope.items(), key=lambda item: (item[0][0], item[0][1])
        ):
            revisions = [
                (doc.document_id, str(doc.current_job_result_id))
                for doc in scoped_docs
                if doc.current_job_result_id
            ]
            if not revisions:
                continue

            snapshot = db.execute(
                select(RetrievalNamespaceMapSnapshot)
                .where(RetrievalNamespaceMapSnapshot.user_id == user_id)
                .where(RetrievalNamespaceMapSnapshot.namespace == namespace)
            ).scalar_one_or_none()

            snapshot_documents: dict[str, object] | None = None
            snapshot_status = "missing"
            if snapshot is None:
                snapshot_status = "missing"
            else:
                try:
                    payload = decode_namespace_map_snapshot(
                        bytes(snapshot.payload_zlib),
                        checksum=str(snapshot.checksum),
                        format_version=int(snapshot.format_version),
                    )
                    decoded = payload.get("documents")
                    if isinstance(decoded, dict):
                        snapshot_documents = decoded
                        snapshot_status = "ok"
                    else:
                        snapshot_status = "corrupt"
                except (TypeError, ValueError):
                    snapshot_status = "corrupt"

            missing_from_snapshot = 0
            if snapshot_documents is None:
                missing_from_snapshot = len(revisions)
            else:
                for document_id_value, job_result_id in revisions:
                    entry = snapshot_documents.get(document_id_value)
                    if (
                        not isinstance(entry, dict)
                        or str(entry.get("job_result_id") or "") != job_result_id
                    ):
                        missing_from_snapshot += 1
                if missing_from_snapshot and snapshot_status == "ok":
                    snapshot_status = "stale"

            index_rows = list(
                db.execute(
                    select(
                        DocumentMapUnitIndex.document_id,
                        DocumentMapUnitIndex.job_result_id,
                        DocumentMapUnitIndex.unit_count,
                        DocumentMapUnitIndex.average_idf_path,
                        DocumentMapUnitIndex.average_idf_content,
                    ).where(
                        DocumentMapUnitIndex.document_id.in_(
                            [document_id_value for document_id_value, _ in revisions]
                        )
                    )
                ).all()
            )
            index_by_revision = {
                (str(document_id_value), str(job_result_id)): (
                    int(unit_count or 0),
                    float(average_idf_path or 0.0),
                    float(average_idf_content or 0.0),
                )
                for document_id_value, job_result_id, unit_count, average_idf_path, average_idf_content in index_rows
            }
            missing_map_index = 0
            suspicious_zero_idf = 0
            for document_id_value, job_result_id in revisions:
                stats = index_by_revision.get((document_id_value, job_result_id))
                if stats is None:
                    missing_map_index += 1
                    continue
                unit_count, average_idf_path, average_idf_content = stats
                if (
                    unit_count > 0
                    and average_idf_path == 0.0
                    and average_idf_content == 0.0
                ):
                    suspicious_zero_idf += 1

            manifest_rows = list(
                db.execute(
                    select(
                        RetrievalServingRevisionManifest.document_id,
                        RetrievalServingRevisionManifest.job_result_id,
                    ).where(
                        RetrievalServingRevisionManifest.document_id.in_(
                            [document_id_value for document_id_value, _ in revisions]
                        )
                    )
                ).all()
            )
            manifest_keys = {
                (str(document_id_value), str(job_result_id))
                for document_id_value, job_result_id in manifest_rows
            }
            missing_revision_manifest = sum(
                1
                for document_id_value, job_result_id in revisions
                if (document_id_value, job_result_id) not in manifest_keys
            )

            would_hit_snapshot_fallback = (
                snapshot_status != "ok" or missing_from_snapshot > 0
            )
            scoring_incomplete = missing_map_index > 0 or suspicious_zero_idf > 0
            reports.append(
                NamespaceFallbackReport(
                    user_id=user_id,
                    namespace=namespace,
                    active_docs=len(revisions),
                    snapshot_status=snapshot_status,
                    missing_from_snapshot=missing_from_snapshot,
                    missing_map_index=missing_map_index,
                    missing_revision_manifest=missing_revision_manifest,
                    suspicious_zero_idf=suspicious_zero_idf,
                    would_hit_snapshot_fallback=would_hit_snapshot_fallback,
                    scoring_incomplete=scoring_incomplete,
                )
            )
    return reports


def print_fallback_check(reports: list[NamespaceFallbackReport]) -> int:
    """Print readiness report. Returns process exit code (0 ready, 1 not)."""
    if not reports:
        print("check: no active documents found")
        return 0

    failed = 0
    for report in reports:
        status = "READY" if report.ready else "NOT_READY"
        if not report.ready:
            failed += 1
        print(
            f"check status={status} user={report.user_id} namespace={report.namespace} "
            f"active_docs={report.active_docs} snapshot={report.snapshot_status} "
            f"missing_from_snapshot={report.missing_from_snapshot} "
            f"missing_map_index={report.missing_map_index} "
            f"missing_revision_manifest={report.missing_revision_manifest} "
            f"suspicious_zero_idf={report.suspicious_zero_idf} "
            f"would_hit_snapshot_fallback={report.would_hit_snapshot_fallback} "
            f"scoring_incomplete={report.scoring_incomplete}"
        )
    ready_count = len(reports) - failed
    print(f"check namespaces_ready={ready_count}/{len(reports)}")
    return 1 if failed else 0


def backfill_map_unit_indexes(*, apply: bool, document_id: str = "") -> int:
    documents = _load_documents(document_id)
    if not apply:
        for document in documents:
            print(
                f"would backfill document={document.document_id} revision={document.current_job_result_id}"
            )
        return len(documents)

    session_factory = get_sync_session_factory()
    for document in documents:
        job_result_id = document.current_job_result_id
        if not job_result_id:
            continue
        with session_factory() as db:
            lock_namespace_generation(
                db,
                user_id=document.user_id,
                namespace=document.namespace,
            )
            locked_document = db.execute(
                select(Document)
                .where(Document.document_id == document.document_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                locked_document is None
                or locked_document.status != "active"
                or locked_document.current_job_result_id != job_result_id
                or locked_document.user_id != document.user_id
                or locked_document.namespace != document.namespace
            ):
                db.rollback()
                print(
                    f"skipped stale or inactive document={document.document_id} "
                    f"revision={job_result_id}"
                )
                continue
            scope = DocumentPublicationScope(
                user_id=locked_document.user_id,
                namespace=locked_document.namespace,
                document_id=locked_document.document_id,
                job_result_id=job_result_id,
                source_file_name=str(locked_document.source_file_name or ""),
            )
            replace_document_map_units(db, scope=scope)
            manifest_payload = persist_revision_serving_state(db, scope=scope)
            patch_namespace_map_snapshot(
                db, scope=scope, manifest_payload=manifest_payload
            )
            advance_namespace_generation(
                db,
                user_id=scope.user_id,
                namespace=scope.namespace,
            )
            db.commit()
        print(f"backfilled document={document.document_id} revision={job_result_id}")
    return len(documents)


def main() -> None:
    arguments = _build_parser().parse_args()
    if arguments.check:
        if arguments.apply:
            raise SystemExit("use either --check or --apply, not both")
        reports = check_fallback_readiness(document_id=str(arguments.document_id))
        raise SystemExit(print_fallback_check(reports))

    count = backfill_map_unit_indexes(
        apply=bool(arguments.apply), document_id=str(arguments.document_id)
    )
    action = "backfilled" if arguments.apply else "found"
    print(f"{action} revisions={count}")


if __name__ == "__main__":
    main()
