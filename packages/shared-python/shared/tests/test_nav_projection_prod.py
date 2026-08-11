"""Knowhere production projection / config authority tests."""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import SectionRow, UnitRow
from shared.services.retrieval.nav.nav_projection import (
    _section_summary_for_map,
    build_map,
)
from shared.services.retrieval.nav.nav_types import NavConfig, map_mode_enabled
from shared.services.retrieval.nav_config import build_nav_config
from shared.services.retrieval.nav_snapshot import build_nav_snapshot


def test_map_mode_enabled_trusts_config_over_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("NAV_MAP_MODE", "0")
    cfg_on = NavConfig(map_mode=True)
    cfg_off = NavConfig(map_mode=False)
    assert map_mode_enabled(cfg_on) is True
    assert map_mode_enabled(cfg_off) is False
    monkeypatch.setenv("NAV_MAP_MODE", "1")
    assert map_mode_enabled(cfg_off) is False
    assert map_mode_enabled(None) is True


def test_build_nav_config_authoritative_for_production() -> None:
    cfg = build_nav_config()
    assert cfg.map_mode is True
    assert cfg.mode == "checklist"
    assert map_mode_enabled(cfg) is True


def test_section_summary_falls_back_to_provider_structure() -> None:
    class _FakeTs:
        def get_structure(self, section_id: str) -> dict[str, Any]:
            assert section_id == "sec_host"
            return {"summary": "provider hosted summary", "preview": "preview"}

    text = _section_summary_for_map(_FakeTs(), "sec_host", doc_id="doc_a")
    assert text == "provider hosted summary"


def test_build_map_inline_summary_uses_provider_when_store_missing() -> None:
    root = SectionRow(
        section_id="sec_root",
        parent_section_id=None,
        section_path="Root",
        section_title="Root",
        section_level=0,
        summary="root summary",
        sort_order=0,
    )
    host = SectionRow(
        section_id="sec_host",
        parent_section_id="sec_root",
        section_path="Chapter 1",
        section_title="Chapter 1",
        section_level=1,
        summary="chapter one summary from db",
        sort_order=1,
    )
    unit = UnitRow(
        chunk_id="chk_text",
        section_id="sec_host",
        chunk_type="text",
        content="body",
        sort_order=0,
    )
    snap = build_nav_snapshot(
        document_titles={"doc_a": "Doc A"},
        sections_by_doc={"doc_a": [root, host]},
        units_by_doc={"doc_a": [unit]},
        chunk_ref_index={
            "chk_text": {
                "document_id": "doc_a",
                "section_path": "Chapter 1",
                "chunk_type": "text",
                "file_path": None,
                "job_id": "job_1",
            }
        },
    )
    ts = ProviderToolSpace(snap.provider)
    cfg = build_nav_config()
    # Force scoped inline-summary path (small actionable map under the limit).
    cfg.scope_inline_summary_char_limit = 50_000
    projection = build_map(
        ts,
        doc_id="doc_a",
        query="chapter",
        scope="sec_host",
        config=cfg,
        map_scores={"sec_host": 1.0},
    )
    assert "summary: chapter one summary from db" in projection.text
    host_views = [v for v in projection.tree_sections if v.section_id == "sec_host"]
    assert host_views
    assert "chapter one summary from db" in host_views[0].summary
