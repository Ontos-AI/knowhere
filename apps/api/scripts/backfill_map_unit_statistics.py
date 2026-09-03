# ruff: noqa: E402

"""Backfill persisted per-channel BM25 statistics without rebuilding tokens.

This maintenance command aggregates existing ``document_map_units`` rows and
updates the four nullable statistics columns on the current active revision.
It never rewrites map-unit tokens, serving manifests, or namespace snapshots.
Each revision is committed independently so interruption is safe.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _bootstrap_python_path() -> None:
    api_root = Path(__file__).resolve().parents[1]
    candidate_roots = (
        api_root / "packages" / "shared-python",
        api_root.parents[1] / "packages" / "shared-python",
    )
    shared_root = next(
        (path for path in candidate_roots if path.is_dir()), None
    )
    if shared_root is None:
        raise RuntimeError("Could not locate shared-python package")
    for path in (api_root, shared_root):
        value = os.fspath(path)
        if value not in sys.path:
            sys.path.insert(0, value)


_bootstrap_python_path()

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from shared.core.database_sync import get_sync_session_factory
from shared.models.database.document import (
    Document,
    DocumentMapUnit,
    DocumentMapUnitIndex,
)
from shared.services.retrieval.nav.knowhere_hybrid import MAP_UNIT_INDEX_FORMAT_VERSION


@dataclass(frozen=True)
class RevisionStatistics:
    path_document_count: int
    path_total_length: int
    content_document_count: int
    content_total_length: int


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write statistics.")
    parser.add_argument(
        "--check", action="store_true", help="Report missing or mismatched statistics."
    )
    parser.add_argument("--document-id", default="")
    parser.add_argument("--user-id", default="")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--batch-size", type=int, default=100)
    return parser


def _load_documents(
    *, document_id: str, user_id: str, namespace: str
) -> list[Document]:
    session_factory = get_sync_session_factory()
    with session_factory() as db:
        statement = (
            select(Document)
            .where(Document.status == "active")
            .where(Document.current_job_result_id.is_not(None))
            .order_by(Document.document_id)
        )
        if document_id:
            statement = statement.where(Document.document_id == document_id)
        if user_id:
            statement = statement.where(Document.user_id == user_id)
        if namespace:
            statement = statement.where(Document.namespace == namespace)
        return list(db.scalars(statement).all())


def _aggregate_statistics(
    db: Session, *, document_id: str, job_result_id: str
) -> RevisionStatistics:
    statement = (
        select(
            func.count()
            .filter(DocumentMapUnit.path_token_count > 0)
            .label("path_document_count"),
            func.coalesce(func.sum(DocumentMapUnit.path_token_count), 0).label(
                "path_total_length"
            ),
            func.count()
            .filter(DocumentMapUnit.content_token_count > 0)
            .label("content_document_count"),
            func.coalesce(func.sum(DocumentMapUnit.content_token_count), 0).label(
                "content_total_length"
            ),
        )
        .where(DocumentMapUnit.document_id == document_id)
        .where(DocumentMapUnit.job_result_id == job_result_id)
    )
    row = db.execute(statement).one()
    return RevisionStatistics(
        path_document_count=int(row.path_document_count or 0),
        path_total_length=int(row.path_total_length or 0),
        content_document_count=int(row.content_document_count or 0),
        content_total_length=int(row.content_total_length or 0),
    )


def _is_complete(index: DocumentMapUnitIndex | None, stats: RevisionStatistics) -> bool:
    return bool(
        index
        and index.format_version == MAP_UNIT_INDEX_FORMAT_VERSION
        and index.path_document_count == stats.path_document_count
        and index.path_total_length == stats.path_total_length
        and index.content_document_count == stats.content_document_count
        and index.content_total_length == stats.content_total_length
    )


def _is_check_ready(
    *, would_update: int, complete: int, skipped: int, documents: int
) -> bool:
    """Return whether a read-only inventory proves every document is ready."""
    return would_update == 0 and skipped == 0 and complete == documents


def _process_batch(
    documents: list[Document], *, apply_changes: bool
) -> tuple[int, int, int]:
    session_factory = get_sync_session_factory()
    updated = 0
    complete = 0
    skipped = 0
    with session_factory() as db:
        for document in documents:
            job_result_id = str(document.current_job_result_id or "")
            stats = _aggregate_statistics(
                db, document_id=document.document_id, job_result_id=job_result_id
            )
            index_statement = (
                select(DocumentMapUnitIndex)
                .where(DocumentMapUnitIndex.document_id == document.document_id)
                .where(DocumentMapUnitIndex.job_result_id == job_result_id)
            )
            if apply_changes:
                index_statement = index_statement.with_for_update()
            index = db.scalar(index_statement)
            if index is None or index.format_version != MAP_UNIT_INDEX_FORMAT_VERSION:
                skipped += 1
                print(f"skip document={document.document_id} reason=missing_or_legacy_index")
                db.rollback()
                continue
            if _is_complete(index, stats):
                complete += 1
                db.rollback()
                continue
            if not apply_changes:
                updated += 1
                db.rollback()
                continue
            db.execute(
                update(DocumentMapUnitIndex)
                .where(DocumentMapUnitIndex.id == index.id)
                .values(
                    path_document_count=stats.path_document_count,
                    path_total_length=stats.path_total_length,
                    content_document_count=stats.content_document_count,
                    content_total_length=stats.content_total_length,
                )
            )
            db.commit()
            updated += 1
    return updated, complete, skipped


def main() -> None:
    args = _build_parser().parse_args()
    if args.apply == args.check:
        raise SystemExit("choose exactly one of --apply or --check")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    documents = _load_documents(
        document_id=args.document_id.strip(),
        user_id=args.user_id.strip(),
        namespace=args.namespace.strip(),
    )
    totals = [0, 0, 0]
    for offset in range(0, len(documents), args.batch_size):
        batch_totals = _process_batch(
            documents[offset : offset + args.batch_size], apply_changes=args.apply
        )
        totals = [left + right for left, right in zip(totals, batch_totals)]
    action = "applied" if args.apply else "would_update"
    print(
        f"{action}={totals[0]} complete={totals[1]} skipped={totals[2]} "
        f"documents={len(documents)}"
    )
    if args.check and not _is_check_ready(
        would_update=totals[0],
        complete=totals[1],
        skipped=totals[2],
        documents=len(documents),
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
