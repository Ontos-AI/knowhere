"""CLI wrapper for document namespace migration."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import sys


def _bootstrap_python_path() -> None:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    api_root = os.path.dirname(current_dir)
    repo_root = os.path.dirname(os.path.dirname(api_root))
    shared_python_path = os.path.join(repo_root, "packages", "shared-python")

    for path in (api_root, shared_python_path):
        if path not in sys.path:
            sys.path.insert(0, path)


_bootstrap_python_path()

def normalize_retrieval_namespace(namespace: str | None) -> str:
    normalized = str(namespace or "").strip()
    return normalized or "default"


def build_sync_database_url(settings) -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")


def create_session_factory(settings):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        build_sync_database_url(settings),
        pool_pre_ping=True,
        connect_args=settings.get_ssl_connect_args(),
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move a user's Knowhere document namespace rows to another namespace.",
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--source-namespace", required=True)
    parser.add_argument("--target-namespace", default="default")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration. Omit for dry-run.",
    )
    return parser.parse_args()


def print_summary(summary) -> None:
    mode = "DRY RUN" if summary.dry_run else "APPLIED"
    print(
        f"{mode}: user={summary.user_id} "
        f"{summary.source_namespace} -> {summary.target_namespace}"
    )
    for table_name, count in sorted(summary.row_counts.items()):
        print(f"{table_name}: {count}")
    print(f"jobs: {summary.job_count}")
    conflict_counts = {
        key: count for key, count in summary.conflict_counts.items() if count > 0
    }
    if conflict_counts:
        print("conflicts:")
        for conflict_key, count in sorted(conflict_counts.items()):
            print(f"  {conflict_key}: {count}")


def main() -> None:
    args = parse_args()
    source_namespace = normalize_retrieval_namespace(args.source_namespace)
    target_namespace = normalize_retrieval_namespace(args.target_namespace)

    from app.services.documents.namespace_migration import (
        NamespaceMigrationConflictError,
        migrate_namespace,
    )
    from shared.core.config import settings

    session_factory = create_session_factory(settings)

    with session_factory() as session:
        try:
            summary = migrate_namespace(
                session,
                user_id=args.user_id,
                source_namespace=source_namespace,
                target_namespace=target_namespace,
                dry_run=not args.apply,
            )
        except NamespaceMigrationConflictError as exc:
            session.rollback()
            print_summary(exc.summary)
            raise SystemExit(2) from exc
        else:
            if args.apply:
                session.commit()
            else:
                session.rollback()
            print_summary(summary)


if __name__ == "__main__":
    main()
