"""Backfill persisted MAP-NAV lexical indexes for existing revisions.

The migration creates empty derived tables intentionally. Run this command
after deployment with ``--apply`` so each revision is rebuilt and committed
independently; without ``--apply`` it is a read-only inventory.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _bootstrap_python_path() -> None:
    api_root = Path(__file__).resolve().parents[1]
    repo_root = api_root.parents[1]
    shared_root = repo_root / "packages" / "shared-python"
    for path in (api_root, shared_root):
        value = os.fspath(path)
        if value not in sys.path:
            sys.path.insert(0, value)


_bootstrap_python_path()

from sqlalchemy import select

from shared.core.database_sync import get_sync_session_factory
from shared.models.database.document import Document
from shared.services.retrieval.map_unit_index import replace_document_map_units
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.serving_generation import (
    advance_namespace_generation,
    lock_namespace_generation,
)
from shared.services.retrieval.serving_manifest import (
    persist_revision_serving_state,
    rebuild_namespace_serving_statistics,
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
            persist_revision_serving_state(db, scope=scope)
            rebuild_namespace_serving_statistics(
                db,
                user_id=scope.user_id,
                namespace=scope.namespace,
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
    count = backfill_map_unit_indexes(
        apply=bool(arguments.apply), document_id=str(arguments.document_id)
    )
    action = "backfilled" if arguments.apply else "found"
    print(f"{action} revisions={count}")


if __name__ == "__main__":
    main()
