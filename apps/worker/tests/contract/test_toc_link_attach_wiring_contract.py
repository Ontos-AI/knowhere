"""PROFILE attaches TOC-page links before calibration."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.manifest import ToolResult


def _hierarchy() -> list[dict[str, object]]:
    return [
        {
            "toc_range": [2, 5],
            "toc_with_level": [
                {"heading": "Ch1", "level": 1, "page_number": 2},
            ],
        }
    ]


def _coordinator() -> ProfileCoordinator:
    coordinator = ProfileCoordinator(pdf_path="/tmp/doc.pdf", job_id="job-link-order")
    coordinator.blackboard.toc_hierarchies = _hierarchy()
    return coordinator


def test_profile_attaches_toc_links_before_anchoring() -> None:
    coordinator = _coordinator()
    seen: dict[str, object] = {}

    def fake_enrich(*, pdf_path: str, toc_hierarchies: list[dict[str, object]]):
        assert pdf_path == "/tmp/doc.pdf"
        attached = [
            {
                **toc_hierarchies[0],
                "toc_with_level": [
                    {
                        **toc_hierarchies[0]["toc_with_level"][0],  # type: ignore[index]
                        "link": {"physical_page": 8},
                    }
                ],
            }
        ]
        return attached, SimpleNamespace(
            entries_matched=1,
            entries_total=1,
            skipped_no_links=False,
        )

    def fake_anchor(ctx) -> None:
        entry = ctx.blackboard.toc_hierarchies[0]["toc_with_level"][0]
        seen["physical_page"] = (entry.get("link") or {}).get("physical_page")

    with (
        patch.object(
            ProfileCoordinator,
            "_dispatch_profile_tool",
            return_value=ToolResult(status="ok"),
        ),
        patch(
            "app.services.document_agent.coordinator.enrich_toc_hierarchies_with_links",
            side_effect=fake_enrich,
        ),
        patch(
            "app.services.document_agent.structure.toc_link_enrichment.enrich_toc_hierarchies_with_links",
            side_effect=fake_enrich,
        ),
        patch(
            "app.services.document_agent.coordinator.run_toc_anchoring",
            side_effect=fake_anchor,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.run_toc_anchoring",
            side_effect=fake_anchor,
        ),
    ):
        coordinator._run_toc_extraction_pipeline()

    assert seen["physical_page"] == 8
    assert (
        coordinator.blackboard.toc_hierarchies[0]["toc_with_level"][0]["link"][
            "physical_page"
        ]
        == 8
    )


def test_link_attach_failure_keeps_hierarchies_and_still_anchors() -> None:
    coordinator = _coordinator()
    seen = {"anchored": False}

    def fake_anchor(ctx) -> None:
        seen["anchored"] = True
        entry = ctx.blackboard.toc_hierarchies[0]["toc_with_level"][0]
        assert entry.get("link") is None

    with (
        patch.object(
            ProfileCoordinator,
            "_dispatch_profile_tool",
            return_value=ToolResult(status="ok"),
        ),
        patch(
            "app.services.document_agent.coordinator.enrich_toc_hierarchies_with_links",
            side_effect=RuntimeError("pymupdf failed"),
        ),
        patch(
            "app.services.document_agent.structure.toc_link_enrichment.enrich_toc_hierarchies_with_links",
            side_effect=RuntimeError("pymupdf failed"),
        ),
        patch(
            "app.services.document_agent.coordinator.run_toc_anchoring",
            side_effect=fake_anchor,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.run_toc_anchoring",
            side_effect=fake_anchor,
        ),
    ):
        coordinator._run_toc_extraction_pipeline()

    assert seen["anchored"] is True
    assert coordinator.blackboard.toc_hierarchies == _hierarchy()


def test_skip_toc_anchoring_stops_after_link_attach() -> None:
    coordinator = _coordinator()
    coordinator.ctx.settings["skip_toc_anchoring"] = True
    seen = {"anchored": False}

    def fake_enrich(*, pdf_path: str, toc_hierarchies: list[dict[str, object]]):
        return toc_hierarchies, SimpleNamespace(
            entries_matched=0,
            entries_total=1,
            skipped_no_links=True,
        )

    def fake_anchor(_ctx) -> None:
        seen["anchored"] = True

    with (
        patch.object(
            ProfileCoordinator,
            "_dispatch_profile_tool",
            return_value=ToolResult(status="ok"),
        ),
        patch(
            "app.services.document_agent.coordinator.enrich_toc_hierarchies_with_links",
            side_effect=fake_enrich,
        ),
        patch(
            "app.services.document_agent.structure.toc_link_enrichment.enrich_toc_hierarchies_with_links",
            side_effect=fake_enrich,
        ),
        patch(
            "app.services.document_agent.coordinator.run_toc_anchoring",
            side_effect=fake_anchor,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.run_toc_anchoring",
            side_effect=fake_anchor,
        ),
    ):
        coordinator._run_toc_extraction_pipeline()

    assert seen["anchored"] is False
    assert coordinator.blackboard.skeleton_anchor is None
    assert coordinator.blackboard.skeleton_nodes is None
    assert coordinator.blackboard.toc_page_offset is None


def test_stop_after_asset_probe_skips_toc() -> None:
    coordinator = ProfileCoordinator(pdf_path="/tmp/doc.pdf", job_id="job-stage0")
    coordinator.ctx.settings["stop_after_asset_probe"] = True
    seen = {"toc": False, "assets": False}

    profile = SimpleNamespace(
        category="spec",
        routing_category="generic",
        is_scanned=False,
    )

    def fake_toc(self, *, strict: bool) -> None:
        seen["toc"] = True

    def fake_assets(self) -> None:
        seen["assets"] = True

    with (
        patch.object(ProfileCoordinator, "_run_bootstrap", return_value=None),
        patch.object(
            ProfileCoordinator,
            "_propose_profile",
            return_value=(profile, None, ToolResult(status="ok")),
        ),
        patch.object(ProfileCoordinator, "_run_text_scan", return_value=None),
        patch.object(ProfileCoordinator, "_ensure_toc_profile", fake_toc),
        patch.object(ProfileCoordinator, "_ensure_asset_probe", fake_assets),
    ):
        out = coordinator._run_coarse()

    assert out is profile
    assert seen["assets"] is True
    assert seen["toc"] is False
