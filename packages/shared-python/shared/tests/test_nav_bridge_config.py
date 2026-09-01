"""Unit tests for map-nav config + exit bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List, Tuple

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.nav._compat import Chunk
from shared.services.retrieval.nav.nav_knowhere import SectionRow, UnitRow
from shared.services.retrieval.nav_bridge import build_referenced_chunks
from shared.services.retrieval.nav_config import (
    MAPNAV_MODEL,
    build_nav_config,
    nav_evidence_chars,
)
from shared.services.retrieval.nav_snapshot import build_nav_snapshot


def test_build_nav_config_is_checklist_map_trim_stack() -> None:
    cfg = build_nav_config()
    assert cfg.mode == "checklist"
    assert cfg.is_checklist
    assert cfg.map_mode is True
    assert cfg.policy == "llm"
    assert cfg.subgoal_max_attempts == 2
    assert cfg.max_replans == 1
    assert cfg.max_waves == 0
    assert cfg.max_harvest_depth == 3
    assert not hasattr(cfg, "compose_packing_mode")
    assert cfg.llm_model == MAPNAV_MODEL
    assert cfg.planner_model == MAPNAV_MODEL
    assert cfg.subagent_model == MAPNAV_MODEL
    assert cfg.planner_thinking == "enabled"
    assert cfg.planner_think_max_tokens == 16_384
    assert cfg.token_limit == 100_000
    assert not hasattr(cfg, "llm_model_env")


def test_nav_evidence_chars_is_code_constant() -> None:
    assert nav_evidence_chars() == 12000


def _root_remount_snapshot():
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
        summary="",
        sort_order=1,
    )
    text_unit = UnitRow(
        chunk_id="chk_text",
        section_id="sec_host",
        chunk_type="text",
        content="body",
        sort_order=0,
        metadata={"connect_to": [{"target": "chk_img"}]},
    )
    img_unit = UnitRow(
        chunk_id="chk_img",
        section_id="sec_root",
        chunk_type="image",
        content="a.png",
        sort_order=1,
        file_path="a.png",
    )
    return build_nav_snapshot(
        document_titles={"doc_a": "Doc A"},
        sections_by_doc={"doc_a": [root, host]},
        units_by_doc={"doc_a": [text_unit, img_unit]},
        chunk_ref_index={
            "chk_text": {
                "document_id": "doc_a",
                "section_path": "Chapter 1",
                "chunk_type": "text",
                "file_path": None,
                "job_id": "job_1",
            },
            "chk_img": {
                "document_id": "doc_a",
                "section_path": "Root",
                "chunk_type": "image",
                "file_path": "a.png",
                "job_id": "job_1",
            },
        },
    )


@dataclass
class _Ep:
    kept_chunks: List[Any]
    scored_chunks: List[Tuple[Any, float]] = field(default_factory=list)


def test_bridge_expands_three_node_id_forms_and_keeps_db_section_path() -> None:
    """chunk_id / leaf section_id / {sid}__self all resolve via chunk_ref_index."""
    snap = _root_remount_snapshot()

    by_chunk = build_referenced_chunks(
        _Ep(
            kept_chunks=[
                Chunk(
                    node_id="chk_text",
                    doc_id="doc_a",
                    text="body",
                    line_ids=(0,),
                    section_id="sec_host",
                )
            ],
            scored_chunks=[
                (
                    Chunk(
                        node_id="chk_text",
                        doc_id="doc_a",
                        text="body",
                        line_ids=(0,),
                        section_id="sec_host",
                    ),
                    0.9,
                )
            ],
        ),
        snap,
    )
    assert by_chunk[0][0]["chunk_id"] == "chk_text"
    assert by_chunk[0][0]["section_path"] == "Chapter 1"
    assert by_chunk[0][0]["job_id"] == "job_1"
    assert by_chunk[1].get("chk_text") == 0.9

    by_section = build_referenced_chunks(
        _Ep(
            kept_chunks=[
                Chunk(
                    node_id="sec_host",
                    doc_id="doc_a",
                    text="host",
                    line_ids=(0,),
                    section_id="sec_host",
                )
            ]
        ),
        snap,
    )
    section_ids = {r["chunk_id"] for r in by_section[0]}
    assert "chk_text" in section_ids
    # After Root-remount, image lives under host for nav but ref path stays DB Root.
    assert "chk_img" in section_ids
    img_ref = next(r for r in by_section[0] if r["chunk_id"] == "chk_img")
    assert img_ref["section_path"] == "Root"
    assert img_ref["job_id"] == "job_1"

    by_self = build_referenced_chunks(
        _Ep(
            kept_chunks=[
                Chunk(
                    node_id="sec_host__self",
                    doc_id="doc_a",
                    text="intro",
                    line_ids=(0,),
                    section_id="sec_host",
                )
            ]
        ),
        snap,
    )
    self_ids = {r["chunk_id"] for r in by_self[0]}
    assert self_ids == section_ids
    assert all(r.get("job_id") == "job_1" for r in by_self[0])
