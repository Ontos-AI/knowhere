"""Protocol tests: coarse profile parse + deterministic shard finalize."""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.coarse_profile.classifier import _parse_profile
from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.manifest import (
    DocumentProfile,
    PageFeature,
    PageLabel,
    TocResult,
)


def test_parse_profile_reads_classification_fields() -> None:
    raw = json.dumps(
        {
            "is_scanned": True,
            "category": "Feasibility Study Report",
            "routing_category": "generic",
            "language": "zh",
            "rationale": "scanned PDF not atlas",
            "header_y": None,
            "footer_y": None,
        }
    )
    profile = _parse_profile(raw)
    assert profile.is_scanned is True
    assert profile.category == "Feasibility Study Report"
    assert profile.routing_category == "generic"
    assert profile.language == "zh"


def test_parse_profile_rejects_invalid_header_footer_order() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "category": "Report",
            "routing_category": "generic",
            "language": "en",
            "rationale": "ok",
            "header_y": 0.8,
            "footer_y": 0.2,
        }
    )
    profile = _parse_profile(raw)
    assert profile.header_y is None
    assert profile.footer_y is None


def _seed_pages(coordinator: ProfileCoordinator, page_count: int) -> None:
    blackboard = coordinator.blackboard
    blackboard.page_count = page_count
    blackboard.doc_stats = {"page_count": page_count}
    blackboard.page_features = [
        PageFeature(
            page=page,
            raw_text_length=0,
            text_density=0.0,
            image_coverage=1.0,
            image_count=1,
            table_count=0,
            drawings_count=0,
            orientation="portrait",
            width=612.0,
            height=792.0,
            has_asset=True,
            is_blank_like=True,
        )
        for page in range(1, page_count + 1)
    ]
    blackboard.page_labels = [
        PageLabel(page=page, kind="normal")
        for page in range(1, page_count + 1)
    ]
    blackboard.toc_result = TocResult(method="none", notes="no toc")
    blackboard.document_profile = DocumentProfile(
        is_scanned=True,
        category="Report",
        routing_category="generic",
        rationale="fixture",
    )


def test_finalize_shard_plan_reaches_success(tmp_path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-finalize",
        output_dir=str(tmp_path / "out"),
    )
    (tmp_path / "out").mkdir()
    _seed_pages(coordinator, 4)

    coordinator._finalize_shard_plan()

    assert coordinator.blackboard.verdict is not None
    assert coordinator.blackboard.verdict.status == "success"
    assert coordinator.blackboard.shard_plan is not None
    assert len(coordinator.blackboard.shard_plan.shards) >= 1


def test_finalize_shard_plan_creates_plan_when_missing(tmp_path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-finalize-missing",
        output_dir=str(tmp_path / "out"),
    )
    (tmp_path / "out").mkdir()
    _seed_pages(coordinator, 3)
    assert coordinator.blackboard.shard_plan is None

    coordinator._finalize_shard_plan()

    assert coordinator.blackboard.shard_plan is not None
    assert len(coordinator.blackboard.shard_plan.shards) == 1
    assert coordinator.blackboard.verdict.status == "success"
