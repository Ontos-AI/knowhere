"""Protocol tests: coarse profile parse + deterministic shard finalize."""

from __future__ import annotations

import json
import os

import pytest

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
from app.services.document_parser.profiling.domain_taxonomy import (
    DOMAIN_SECTIONS,
    OTHER_CODE,
)


def _menu_id(code: str) -> int:
    return next(
        number
        for number, leaf_code in enumerate(
            (leaf.code for section in DOMAIN_SECTIONS for leaf in section.leaves),
            start=1,
        )
        if leaf_code == code
    )


def test_parse_profile_reads_classification_fields() -> None:
    raw = json.dumps(
        {
            "is_scanned": True,
            "domain_ids": [_menu_id("6612"), _menu_id("29")],
            "category_label": "Feasibility Study Report",
            "routing_category": "generic",
            "language": "zh",
            "header_y": None,
            "footer_y": None,
        }
    )
    profile = _parse_profile(raw)
    assert profile.is_scanned is True
    assert profile.category == ["6612", "29"]
    assert profile.category_label == "Feasibility Study Report"
    assert profile.routing_category == "generic"
    assert profile.language == "zh"


@pytest.mark.parametrize(
    "domain_ids",
    [
        None,
        [],
        "6612",
        [0],
        [10_000],
        ["12"],
        [True],
        [1, 1],
        [_menu_id(OTHER_CODE), 1],
    ],
)
def test_parse_profile_falls_back_to_other_for_invalid_domain_ids(domain_ids) -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "domain_ids": domain_ids,
            "category_label": "Report",
            "routing_category": "generic",
            "language": "en",
        }
    )
    profile = _parse_profile(raw)
    assert profile.category == [OTHER_CODE]
    assert profile.category_label == "Report"


def test_parse_profile_does_not_route_from_category_label() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "domain_ids": [_menu_id(OTHER_CODE)],
            "category_label": "Engineering Atlas",
            "language": "en",
        }
    )
    assert _parse_profile(raw).routing_category == "generic"


def test_parse_profile_rejects_invalid_header_footer_order() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "domain_ids": [_menu_id(OTHER_CODE)],
            "category_label": "Report",
            "routing_category": "generic",
            "language": "en",
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
        category=[OTHER_CODE],
        category_label="Report",
        routing_category="generic",
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


def test_finalize_shard_plan_aborts_when_validation_fails(tmp_path) -> None:
    from app.services.document_agent.manifest import ShardPlan

    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-finalize-abort",
        output_dir=str(tmp_path / "out"),
    )
    (tmp_path / "out").mkdir()
    _seed_pages(coordinator, 4)
    coordinator.blackboard.shard_plan = ShardPlan(
        enabled=True,
        reason="too_large",
        shards=[],
    )
    coordinator.blackboard.validation_report = {
        "valid": False,
        "errors": ["shard_plan has no shards"],
        "warnings": [],
    }

    try:
        coordinator._finalize_shard_plan()
        raise AssertionError("expected shard validation failure to abort")
    except RuntimeError as exc:
        assert "Shard plan validation failed" in str(exc)

    assert coordinator.blackboard.verdict is not None
    assert coordinator.blackboard.verdict.status == "abort"
    assert "fallback" not in (coordinator.blackboard.verdict.rationale or "").lower()


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
