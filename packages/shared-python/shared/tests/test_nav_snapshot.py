"""Unit tests for map-nav snapshot assembly (no DB)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.nav.nav_knowhere import SectionRow, UnitRow
from shared.services.retrieval.nav_snapshot import build_nav_snapshot


def test_build_nav_snapshot_keeps_original_section_path_after_root_remount() -> None:
    """chunk_ref_index must keep DB Root path even if provider remounts assets."""
    root = SectionRow(
        section_id="sec_root",
        parent_section_id=None,
        section_path="Root",
        section_title="Root",
        section_level=0,
        summary="",
        sort_order=0,
    )
    host = SectionRow(
        section_id="sec_host",
        parent_section_id="sec_root",
        section_path="Chapter 1",
        section_title="Chapter 1",
        section_level=1,
        summary="host",
        sort_order=1,
    )
    text = UnitRow(
        chunk_id="chk_text",
        section_id="sec_host",
        chunk_type="text",
        content="body",
        sort_order=0,
        metadata={"connect_to": [{"target": "chk_img"}]},
    )
    image = UnitRow(
        chunk_id="chk_img",
        section_id="sec_root",
        chunk_type="image",
        content="img/a.png",
        sort_order=1,
        file_path="img/a.png",
    )
    ref_index = {
        "chk_text": {
            "document_id": "doc_a",
            "section_path": "Chapter 1",
            "chunk_type": "text",
            "file_path": None,
            "job_id": "job_1",
        },
        "chk_img": {
            "document_id": "doc_a",
            "section_path": "Root",  # DB original
            "chunk_type": "image",
            "file_path": "img/a.png",
            "job_id": "job_1",
        },
    }
    snap = build_nav_snapshot(
        document_titles={"doc_a": "Doc A"},
        sections_by_doc={"doc_a": [root, host]},
        units_by_doc={"doc_a": [text, image]},
        chunk_ref_index=ref_index,
    )

    assert snap.document_ids == ["doc_a"]
    assert snap.chunk_ref_index["chk_img"]["section_path"] == "Root"
    # Provider remounted the image onto the host section for navigation.
    host_units = list(snap.provider.self_units("sec_host"))
    assert any(u.chunk_id == "chk_img" for u in host_units)
    root_units = list(snap.provider.self_units("sec_root"))
    assert not any(u.chunk_id == "chk_img" for u in root_units)


def test_build_nav_snapshot_rejects_empty_corpus() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_nav_snapshot(
            document_titles={},
            sections_by_doc={},
            units_by_doc={},
            chunk_ref_index={},
        )


def test_load_nav_snapshot_filters_current_revision_only() -> None:
    """Snapshot loading must select and query only each current revision."""
    import inspect

    from shared.services.retrieval import nav_snapshot as nav_snapshot_mod

    loader_src = "".join(inspect.getsource(nav_snapshot_mod.load_nav_snapshot).split())
    sections_src = "".join(inspect.getsource(nav_snapshot_mod._load_sections).split())
    chunks_src = "".join(inspect.getsource(nav_snapshot_mod._load_chunks).split())
    assert "Document.current_job_result_id" in loader_src
    assert "DocumentSection.job_result_id==Document.current_job_result_id" in sections_src
    assert "DocumentChunk.document_id==document_id" in chunks_src
    assert "DocumentChunk.job_result_id==job_result_id" in chunks_src
