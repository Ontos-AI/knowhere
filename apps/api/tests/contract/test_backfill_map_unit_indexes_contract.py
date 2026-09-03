from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy.dialects import postgresql


def test_backfill_script_resolves_shared_package_from_runtime_image_layout(
    tmp_path: Path,
) -> None:
    from scripts.backfill_map_unit_indexes import _resolve_shared_root

    api_root = tmp_path / "app"
    shared_root = api_root / "packages" / "shared-python"
    shared_root.mkdir(parents=True)

    assert _resolve_shared_root(api_root) == shared_root


def test_backfill_script_resolves_shared_package_from_source_checkout_layout(
    tmp_path: Path,
) -> None:
    from scripts.backfill_map_unit_indexes import _resolve_shared_root

    repository_root = tmp_path / "repository"
    api_root = repository_root / "apps" / "api"
    shared_root = repository_root / "packages" / "shared-python"
    shared_root.mkdir(parents=True)

    assert _resolve_shared_root(api_root) == shared_root


def test_statistics_backfill_resolves_shared_package_from_runtime_image_layout(
    tmp_path: Path,
) -> None:
    from scripts.backfill_map_unit_statistics import _resolve_shared_root

    api_root = tmp_path / "app"
    shared_root = api_root / "packages" / "shared-python"
    shared_root.mkdir(parents=True)

    assert _resolve_shared_root(api_root) == shared_root


def test_statistics_backfill_resolves_shared_package_from_source_checkout_layout(
    tmp_path: Path,
) -> None:
    from scripts.backfill_map_unit_statistics import _resolve_shared_root

    repository_root = tmp_path / "repository"
    api_root = repository_root / "apps" / "api"
    shared_root = repository_root / "packages" / "shared-python"
    shared_root.mkdir(parents=True)

    assert _resolve_shared_root(api_root) == shared_root


def test_statistics_backfill_aggregates_positive_lengths_per_channel() -> None:
    from scripts.backfill_map_unit_statistics import (
        RevisionStatistics,
        _aggregate_statistics,
    )

    class AggregateResult:
        def one(self) -> SimpleNamespace:
            return SimpleNamespace(
                path_document_count=2,
                path_total_length=9,
                content_document_count=1,
                content_total_length=7,
            )

    class AggregateSession:
        statement_sql: str = ""

        def execute(self, statement: Any) -> AggregateResult:
            self.statement_sql = str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            return AggregateResult()

    session = AggregateSession()
    statistics = _aggregate_statistics(
        cast(Any, session), document_id="doc_1", job_result_id="result_1"
    )

    assert statistics == RevisionStatistics(
        path_document_count=2,
        path_total_length=9,
        content_document_count=1,
        content_total_length=7,
    )
    assert "path_token_count > 0" in session.statement_sql
    assert "content_token_count > 0" in session.statement_sql


def test_statistics_backfill_readiness_requires_every_document_complete() -> None:
    from scripts.backfill_map_unit_statistics import _is_check_ready

    assert _is_check_ready(
        would_update=0, complete=4, skipped=0, documents=4
    )
    assert not _is_check_ready(
        would_update=1, complete=3, skipped=0, documents=4
    )
    assert not _is_check_ready(
        would_update=0, complete=3, skipped=1, documents=4
    )


def test_statistics_backfill_completion_rejects_missing_or_legacy_indexes() -> None:
    from scripts.backfill_map_unit_statistics import RevisionStatistics, _is_complete

    statistics = RevisionStatistics(
        path_document_count=1,
        path_total_length=2,
        content_document_count=1,
        content_total_length=3,
    )
    legacy_index = SimpleNamespace(
        format_version=1,
        path_document_count=1,
        path_total_length=2,
        content_document_count=1,
        content_total_length=3,
    )
    complete_index = SimpleNamespace(
        format_version=2,
        path_document_count=1,
        path_total_length=2,
        content_document_count=1,
        content_total_length=3,
    )

    assert not _is_complete(None, statistics)
    assert not _is_complete(cast(Any, legacy_index), statistics)
    assert _is_complete(cast(Any, complete_index), statistics)


def test_full_backfill_readiness_requires_revision_manifests() -> None:
    from scripts.backfill_map_unit_indexes import NamespaceFallbackReport

    report = NamespaceFallbackReport(
        user_id="user_1",
        namespace="default",
        active_docs=1,
        snapshot_status="ok",
        missing_from_snapshot=0,
        missing_map_index=0,
        missing_revision_manifest=1,
        suspicious_zero_idf=0,
        would_hit_snapshot_fallback=False,
        scoring_incomplete=False,
    )

    assert not report.ready


def test_full_backfill_readiness_allows_mathematically_valid_zero_idf() -> None:
    from scripts.backfill_map_unit_indexes import NamespaceFallbackReport

    report = NamespaceFallbackReport(
        user_id="user_1",
        namespace="default",
        active_docs=1,
        snapshot_status="ok",
        missing_from_snapshot=0,
        missing_map_index=0,
        missing_revision_manifest=0,
        suspicious_zero_idf=1,
        would_hit_snapshot_fallback=False,
        scoring_incomplete=False,
    )

    assert report.ready
