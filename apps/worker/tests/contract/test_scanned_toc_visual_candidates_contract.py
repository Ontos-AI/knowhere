"""Scanned Coarse TOC: PyMuPDF bands + visual keyword pages injected before find."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.tools.find_toc_anchor_pages import (  # noqa: E402
    TOC_KEYWORDS,
    _all_scan_keywords,
    _prefix_toc_keywords_for_categories,
)
from app.services.page_memory.phase_a import (  # noqa: E402
    PHASE_B_BLACKBOARD_REL,
    PHASE_B_SOURCE_PDF_REL,
)
from app.services.page_memory.scope_enrichment_service import (  # noqa: E402
    _ClaimedScopeTarget,
    _resume_profile_from_m0_workspace,
)
from shared.models.schemas.page_memory_config import PageMemoryConfig  # noqa: E402


def _live_page_embedding_index():
    import shared.services.retrieval.page_embedding_index as page_embedding_index_mod

    return page_embedding_index_mod


def _target(*, document_id: str = "doc_scan", namespace: str = "default") -> _ClaimedScopeTarget:
    return _ClaimedScopeTarget(
        document_id=document_id,
        job_id="job-scan",
        user_id="user-scan",
        namespace=namespace,
        source_file_name="scan.pdf",
        raw_prefix=None,
        job_metadata={},
        page_memory_config=PageMemoryConfig.default(),
    )


def _write_m0_workspace(
    tmp_path: Path,
    *,
    is_scanned: bool,
    routing_category: str | None = None,
) -> str:
    add_dir = tmp_path / "scan"
    add_dir.mkdir(exist_ok=True)
    (add_dir / PHASE_B_SOURCE_PDF_REL).write_bytes(b"%PDF-1.4")
    blackboard_path = add_dir / PHASE_B_BLACKBOARD_REL
    blackboard_path.parent.mkdir(parents=True, exist_ok=True)
    blackboard_path.write_text(
        json.dumps(
            {
                "page_count": 20,
                "document_profile": {
                    "is_scanned": is_scanned,
                    "category": ["OTHER"],
                    "category_label": "Scanned Book",
                    "routing_category": (
                        routing_category
                        if routing_category is not None
                        else ("scanned" if is_scanned else "generic")
                    ),
                    "language": "en",
                    "header_y": 0.06,
                    "footer_y": 0.95,
                },
                "page_features": [],
                "page_labels": [{"page": 1, "kind": "normal", "evidence": {}}],
                "doc_stats": {},
                "global_signals": {},
                "page_full_text_bands": {
                    "14": {
                        "lines": [
                            {
                                "text": "CONTENTS",
                                "region": "header",
                                "line_index": 0,
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return str(add_dir)


def test_resume_scanned_injects_visual_candidate_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_query(db, *, namespace, document_id, job_result_id, queries, per_keyword_top_k=3):
        captured["namespace"] = namespace
        captured["document_id"] = document_id
        captured["job_result_id"] = job_result_id
        captured["queries"] = list(queries)
        captured["per_keyword_top_k"] = per_keyword_top_k
        captured["db"] = db
        return {14, 15}

    def fake_toc(self, *, strict: bool) -> None:
        captured["toc_strict"] = strict
        captured["injected"] = list(
            self.ctx.settings.get("toc_visual_candidate_pages") or []
        )

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace()

    page_embedding_index_mod = _live_page_embedding_index()
    import app.services.page_memory.scope_enrichment_service as scope_enrichment
    import app.services.document_agent.coordinator as coordinator_mod
    import app.services.document_parser.profiling.doc_profiler as doc_profiler

    monkeypatch.setattr(
        page_embedding_index_mod, "query_visual_candidate_pages", fake_query
    )
    monkeypatch.setattr(scope_enrichment, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(
        coordinator_mod.ProfileCoordinator, "_ensure_toc_profile", fake_toc
    )
    monkeypatch.setattr(
        coordinator_mod.ProfileCoordinator,
        "run_lightweight_anatomy",
        lambda self: SimpleNamespace(),
    )
    monkeypatch.setattr(
        doc_profiler, "_map_toc_profile", lambda _coordinator: SimpleNamespace()
    )

    add_dir = _write_m0_workspace(tmp_path, is_scanned=True)
    _resume_profile_from_m0_workspace(
        add_dir=add_dir,
        target=_target(),
        job_result_id="jr_scan",
    )
    expected_queries = sorted(
        _all_scan_keywords(
            prefix_keywords=_prefix_toc_keywords_for_categories(["OTHER"])
        )
    )
    assert captured["document_id"] == "doc_scan"
    assert captured["job_result_id"] == "jr_scan"
    assert captured["namespace"] == "default"
    assert captured["per_keyword_top_k"] == 3
    assert captured["queries"] == expected_queries
    assert set(expected_queries) >= set(TOC_KEYWORDS)
    assert captured["injected"] == [14, 15]
    assert captured["toc_strict"] is False


def test_resume_born_digital_skips_visual_candidate_query_until_after_coarse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    injected: list[int] = []

    def fail_if_queried(*_args: object, **_kwargs: object) -> set[int]:
        raise AssertionError(
            "Electronic PDF has no M0 page embeddings; must not query before coarse publish"
        )

    def fake_toc(self, *, strict: bool) -> None:
        del strict
        called.append("toc")
        injected.extend(self.ctx.settings.get("toc_visual_candidate_pages") or [])

    page_embedding_index_mod = _live_page_embedding_index()
    import app.services.document_agent.coordinator as coordinator_mod
    import app.services.document_parser.profiling.doc_profiler as doc_profiler

    monkeypatch.setattr(
        page_embedding_index_mod, "query_visual_candidate_pages", fail_if_queried
    )
    monkeypatch.setattr(
        coordinator_mod.ProfileCoordinator, "_ensure_toc_profile", fake_toc
    )
    monkeypatch.setattr(
        coordinator_mod.ProfileCoordinator,
        "run_lightweight_anatomy",
        lambda self: SimpleNamespace(),
    )
    monkeypatch.setattr(
        doc_profiler, "_map_toc_profile", lambda _coordinator: SimpleNamespace()
    )

    add_dir = _write_m0_workspace(tmp_path, is_scanned=False)
    _resume_profile_from_m0_workspace(
        add_dir=add_dir,
        target=_target(),
        job_result_id="jr_native",
    )
    assert called == ["toc"]
    assert injected == []


def test_resume_routing_scanned_injects_visual_candidate_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    injected: list[int] = []

    def fake_query(*_args: object, **_kwargs: object) -> set[int]:
        called.append("query")
        return {4, 5}

    def fake_toc(self, *, strict: bool) -> None:
        del strict
        called.append("toc")
        injected.extend(self.ctx.settings.get("toc_visual_candidate_pages") or [])

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace()

    page_embedding_index_mod = _live_page_embedding_index()
    import app.services.page_memory.scope_enrichment_service as scope_enrichment
    import app.services.document_agent.coordinator as coordinator_mod
    import app.services.document_parser.profiling.doc_profiler as doc_profiler

    monkeypatch.setattr(
        page_embedding_index_mod, "query_visual_candidate_pages", fake_query
    )
    monkeypatch.setattr(scope_enrichment, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(
        coordinator_mod.ProfileCoordinator, "_ensure_toc_profile", fake_toc
    )
    monkeypatch.setattr(
        coordinator_mod.ProfileCoordinator,
        "run_lightweight_anatomy",
        lambda self: SimpleNamespace(),
    )
    monkeypatch.setattr(
        doc_profiler, "_map_toc_profile", lambda _coordinator: SimpleNamespace()
    )

    add_dir = _write_m0_workspace(
        tmp_path,
        is_scanned=False,
        routing_category="scanned",
    )
    _resume_profile_from_m0_workspace(
        add_dir=add_dir,
        target=_target(),
        job_result_id="jr_routing_scan",
    )
    assert called == ["query", "toc"]
    assert injected == [4, 5]


def test_query_visual_candidate_pages_unions_per_keyword_top3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings

    page_embedding_index_mod = _live_page_embedding_index()
    monkeypatch.setattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(
        page_embedding_index_mod,
        "embed_query",
        lambda text, **_kw: [float(len(text)), 0.0],
    )

    pages_by_call = [[14, 15, 16], [15, 17, 18]]

    class _Rows:
        def __init__(self, pages: list[int]) -> None:
            self._pages = pages

        def all(self) -> list[int]:
            return list(self._pages)

    class _Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        def scalars(self, statement: object) -> _Rows:
            self.statements.append(statement)
            return _Rows(pages_by_call[len(self.statements) - 1])

    db = _Session()
    pages = page_embedding_index_mod.query_visual_candidate_pages(
        db,  # type: ignore[arg-type]
        namespace="default",
        document_id="doc_scan",
        job_result_id="jr_scan",
        queries=["contents", "目录"],
    )
    compiled = str(db.statements[0])
    assert pages == {14, 15, 16, 17, 18}
    assert len(db.statements) == 2
    assert "cosine_distance" in compiled or "<=>" in compiled
    assert "document_page_embeddings" in compiled


def test_query_visual_candidate_pages_disabled_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings

    page_embedding_index_mod = _live_page_embedding_index()
    monkeypatch.setattr(settings, "VISUAL_RETRIEVAL_ENABLED", False)
    pages = page_embedding_index_mod.query_visual_candidate_pages(
        SimpleNamespace(),  # type: ignore[arg-type]
        namespace="default",
        document_id="doc_scan",
        job_result_id="jr_scan",
        queries=["contents"],
    )
    assert pages == set()
