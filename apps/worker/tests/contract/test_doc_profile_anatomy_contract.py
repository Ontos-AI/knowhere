from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent import coordinator as coordinator_module
from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.trace import ParseRunRecorder
from app.services.document_agent.manifest import (
    DocumentProfile,
    H1BoundaryResult,
    PageAnatomyMap,
    PageFeature,
    PageLabel,
    Shard,
    ShardPlan,
    TocAnchorPage,
    TocEvidence as AgentTocEvidence,
    TocResult,
    ToolResult,
)
from app.services.document_agent.tools import find_toc_anchor_pages as toc_anchor_tool
from app.services.document_agent.validators import validate_shard_plan
from app.services.document_parser.formats.atlas import parser as atlas_parser
from app.services.document_parser.formats.pdf import parser as pdf_parser
from app.services.document_parser.formats.pdf import shard_splitter
from app.services.document_parser.profiling import doc_profiler
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.profiling.profile_model import (
    ParserDocumentProfile,
    ParserTocProfile,
)
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
from app.services.document_parser.structure.layout_parser import pred_titles


def _page_feature(page: int = 1) -> PageFeature:
    return PageFeature(
        page=page,
        raw_text_length=20,
        text_density=0.1,
        image_coverage=0.0,
        image_count=0,
        table_count=0,
        drawings_count=0,
        orientation="portrait",
        width=72.0,
        height=72.0,
        has_asset=False,
        is_blank_like=False,
    )


def _seed_preprobed_pages(
    coordinator: ProfileCoordinator,
    *,
    page_count: int,
    pages: list[int] | None = None,
) -> None:
    """Seed text-bootstrap state for coordinator tests without a real PDF.

    Marks assets as already probed so `_ensure_asset_probe` does not spawn a
    PyMuPDF child against a missing fixture path.
    """
    probed_pages = pages or list(range(1, page_count + 1))
    coordinator.blackboard.page_count = page_count
    coordinator.blackboard.page_features = [_page_feature(page) for page in probed_pages]
    coordinator.blackboard.page_labels = [
        PageLabel(page=page, kind="normal", confidence=1.0) for page in probed_pages
    ]
    coordinator.blackboard.doc_stats = {"page_count": page_count}
    coordinator.blackboard.global_signals["page_kind_counts"] = {
        "normal": page_count
    }
    coordinator.blackboard.global_signals["assets_probed"] = True


def test_toc_anchor_text_scan_matches_full_page_and_cross_line_keywords() -> None:
    late_lines = [f"body line {idx}" for idx in range(60)] + ["目录"]
    split_lines = ["Table of", "Con", "tents"]

    late_matches = toc_anchor_tool._find_toc_text_matches(  # noqa: SLF001
        late_lines,
        cross_line_window=6,
    )
    split_matches = toc_anchor_tool._find_toc_text_matches(  # noqa: SLF001
        split_lines,
        cross_line_window=6,
    )

    assert late_matches[0]["line_index"] == 60
    assert late_matches[0]["match_kind"] == "keyword:目录"
    assert split_matches[0]["match_kind"] == "cross_line:tableofcontents"


def test_toc_extraction_degrades_to_empty_result_on_failure(tmp_path: Path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "standard.pdf"),
        job_id="job-toc-fail-soft",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.page_count = 1
    coordinator.blackboard.page_features = [_page_feature()]

    def _fail_toc_extraction() -> None:
        raise RuntimeError("VLM JSON parse failed")

    coordinator._run_toc_extraction_pipeline = _fail_toc_extraction  # type: ignore[method-assign]

    coordinator._ensure_toc_profile(strict=False)
    toc_result = coordinator.blackboard.toc_result

    assert toc_result is not None
    assert toc_result.method == "none"
    assert toc_result.toc_pages == []
    assert toc_result.failure_kind == "degraded"
    assert "degraded" in toc_result.notes
    assert coordinator.blackboard.toc_hierarchies is None


def test_run_lightweight_anatomy_builds_single_shard_without_planner_llm(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "profile"
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "standard.pdf"),
        job_id="job-lightweight",
        output_dir=str(output_dir),
        settings={"shard_threshold": 200},
    )
    _seed_preprobed_pages(coordinator, page_count=2)
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category="Research Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )
    coordinator.blackboard.toc_result = TocResult(method="none")

    anatomy = coordinator.run_lightweight_anatomy()

    assert anatomy.shard_plan.enabled is False
    assert len(anatomy.shard_plan.shards) == 1
    assert anatomy.shard_plan.shards[0].page_start == 1
    assert anatomy.shard_plan.shards[0].page_end == 2
    assert anatomy.toc_result.method == "none"
    assert (output_dir / "anatomy_map.json").exists()
    anatomy_data = json.loads(
        (output_dir / "anatomy_map.json").read_text(encoding="utf-8")
    )
    assert list(anatomy_data)[:2] == ["version", "toc_hierarchies"]
    assert "page_full_text_cache" not in anatomy_data
    assert "text_lines_preview" not in anatomy_data["page_features"][0]
    assert "asset_bboxes" not in anatomy_data["page_features"][0]
    trace_data = json.loads((output_dir / "trace.json").read_text(encoding="utf-8"))
    assert "visual_stages" in trace_data["summary"]["budget"]


def test_run_coarse_runs_asset_probe_after_planner(monkeypatch, tmp_path: Path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-asset-probe-after-coarse",
        output_dir=str(tmp_path / "profile"),
    )
    (tmp_path / "profile").mkdir()
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.page_features = [_page_feature(1), _page_feature(2)]
    coordinator.blackboard.page_labels = [
        PageLabel(page=1, kind="normal", confidence=1.0),
        PageLabel(page=2, kind="normal", confidence=1.0),
    ]
    coordinator.blackboard.doc_stats = {"page_count": 2}
    coordinator.blackboard.global_signals["page_kind_counts"] = {"normal": 2}

    calls: list[str] = []

    def fake_propose(_self):
        calls.append("planner")
        return (
            DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            ),
            None,
            ToolResult(status="ok", payload={}),
        )

    def fake_probe_page_assets(ctx, _args):
        calls.append("probe.page_assets")
        ctx.blackboard.global_signals["assets_probed"] = True
        return ToolResult(status="ok", payload={"page_count": 2})

    def fake_aggregate(ctx, _args):
        calls.append("aggregate.doc_stats")
        return ToolResult(status="ok", payload={})

    def fake_text_scan() -> None:
        calls.append("text_scan")
        coordinator.blackboard.page_full_text_cache = {1: "a", 2: "b"}

    def fake_toc() -> None:
        calls.append("toc")
        coordinator.blackboard.toc_result = TocResult(method="none")

    monkeypatch.setattr(coordinator_module.ProfilePlanner, "propose", fake_propose)
    monkeypatch.setattr(coordinator_module, "probe_page_assets", fake_probe_page_assets)
    monkeypatch.setattr(coordinator_module, "aggregate_doc_stats", fake_aggregate)
    monkeypatch.setattr(coordinator, "_run_text_scan", fake_text_scan)
    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc)

    profile = coordinator.run_coarse()

    assert profile.category == "Research Report"
    assert calls == ["planner", "text_scan", "toc", "probe.page_assets", "aggregate.doc_stats"]
    assert coordinator.blackboard.global_signals["assets_probed"] is True


def test_parse_run_recorder_doc_profile_uses_final_anatomy_toc() -> None:
    recorder = ParseRunRecorder(job_id="job-doc-profile")
    recorder.set_doc_profile(
        ParserDocumentProfile(
            file_type="pdf",
            category="Financial Prospectus",
            routing_category=PdfRoutingCategory.GENERIC,
            page_count=0,
        )
    )
    recorder._anatomy = PageAnatomyMap(  # noqa: SLF001
        job_id="job-doc-profile",
        file_path="/tmp/doc.pdf",
        page_count=2,
        page_features=[_page_feature(1), _page_feature(2)],
        page_labels=[
            PageLabel(page=1, kind="normal", confidence=1.0),
            PageLabel(page=2, kind="normal", confidence=1.0),
        ],
        toc_result=TocResult(toc_pages=[2], method="vlm_batch", notes="ok"),
        h1_result=H1BoundaryResult(method="toc_grep"),
        shard_plan=ShardPlan(enabled=False, reason="not_needed"),
        toc_hierarchies=[{"toc_range": [2, 2], "toc_tree": {}}],
        global_signals={"toc_profile_attempted": True},
    )

    doc_profile = recorder._doc_profile_for_plan()  # noqa: SLF001

    assert doc_profile is not None
    assert doc_profile["category"] == "Financial Prospectus"
    assert doc_profile["routing_category"] == PdfRoutingCategory.GENERIC.value
    assert doc_profile["page_count"] == 2
    assert doc_profile["toc"]["method"] == "vlm_batch"
    assert doc_profile["toc"]["toc_pages"] == [2]


def test_parse_run_recorder_profile_only_row_is_updated_by_anatomy_flush() -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.rows = []
            self.flushes = 0

        def add(self, row) -> None:
            self.rows.append(row)

        def flush(self) -> None:
            self.flushes += 1

        def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

    db = FakeDb()
    recorder = ParseRunRecorder(job_id="job-profile-only", db=db)
    recorder.persist_doc_profile(
        ParserDocumentProfile(
            file_type="pdf",
            category="Atlas",
            routing_category=PdfRoutingCategory.ATLAS,
            page_count=12,
        )
    )

    assert len(db.rows) == 1
    assert db.rows[0].doc_profile["category"] == "Atlas"
    assert db.rows[0].shard_plan is None

    recorder._anatomy = PageAnatomyMap(  # noqa: SLF001
        job_id="job-profile-only",
        file_path="/tmp/doc.pdf",
        page_count=12,
        page_features=[_page_feature(1)],
        page_labels=[PageLabel(page=1, kind="normal", confidence=1.0)],
        toc_result=TocResult(toc_pages=[2], method="vlm_batch", notes="ok"),
        h1_result=H1BoundaryResult(method="toc_grep"),
        shard_plan=ShardPlan(enabled=False, reason="not_needed"),
        toc_hierarchies=[{"toc_range": [2, 2], "toc_tree": {}}],
        global_signals={"toc_profile_attempted": True},
    )
    recorder.flush(final_status="ready")

    assert len(db.rows) == 2
    assert db.rows[0].shard_plan is not None
    assert db.rows[0].doc_profile["toc"]["method"] == "vlm_batch"


def test_run_structural_retries_transient_confirm_failed_toc_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-suspect-empty-toc",
        output_dir=str(tmp_path / "profile"),
    )
    (tmp_path / "profile").mkdir()
    _seed_preprobed_pages(coordinator, page_count=3, pages=[1, 2])
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category="Prospectus",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )
    coordinator.blackboard.toc_result = TocResult(
        candidates=[
            TocAnchorPage(page=17, png_path="/tmp/toc_anchor_page_17.png", source="text_scan")
        ],
        evidence=[
            AgentTocEvidence(
                page_index=17,
                source="vlm",
                confidence=0.05,
                reason="rejected",
            )
        ],
        method="none",
        notes="VLM anchor confirmation failed; TOC candidates left unconfirmed",
        failure_kind="confirm_failed",
    )

    calls: list[str] = []

    def fake_toc_extraction() -> None:
        calls.append("toc")
        coordinator.blackboard.toc_result = TocResult(toc_pages=[17], method="vlm_batch")
        coordinator.blackboard.toc_hierarchies = [
            {"toc_range": [17, 17], "toc_range_unit": "page", "toc_tree": {}}
        ]

    def fake_persist(_anatomy):
        calls.append("persist")

    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc_extraction)
    monkeypatch.setattr(coordinator, "_persist_ready_anatomy", fake_persist)

    monkeypatch.setattr(
        coordinator_module.ProfilePlanner,
        "propose",
        lambda self: (
            coordinator.blackboard.document_profile,
            None,
            ToolResult(status="ok", payload={}),
        ),
    )

    class FakeExecutor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self):
            coordinator.blackboard.shard_plan = ShardPlan(
                enabled=True,
                reason="too_large",
                shards=[
                    Shard(
                        shard_index=0,
                        page_start=1,
                        page_end=3,
                        page_offset=0,
                        anchor_type="forced_max_size",
                        anchor_evidence="fixture",
                        confidence=1.0,
                    )
                ],
            )
            return SimpleNamespace(
                success=True,
                verdict=SimpleNamespace(status="success", rationale="ok"),
                trace_summary={},
            )

    monkeypatch.setattr(coordinator_module, "ReActExecutor", FakeExecutor)

    anatomy = coordinator.run_structural()

    assert calls == ["toc", "persist"]
    assert anatomy.toc_result.toc_pages == [17]


def test_run_structural_skip_shard_plan_uses_placeholder_without_executor(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-structural-skip-shard",
        output_dir=str(tmp_path / "profile"),
    )
    (tmp_path / "profile").mkdir()
    _seed_preprobed_pages(coordinator, page_count=4)
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category="Prospectus",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )
    coordinator.blackboard.toc_result = TocResult(
        toc_pages=[1],
        method="vlm_batch",
        notes="ok",
    )
    coordinator.blackboard.toc_hierarchies = [
        {"toc_range": [1, 1], "toc_range_unit": "page", "toc_tree": {}}
    ]

    monkeypatch.setattr(
        coordinator,
        "_run_toc_extraction_pipeline",
        lambda: (_ for _ in ()).throw(
            AssertionError("existing TOC should not be re-extracted")
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "_persist_ready_anatomy",
        lambda _anatomy: None,
    )
    monkeypatch.setattr(
        coordinator_module.ProfilePlanner,
        "propose",
        lambda self: (
            coordinator.blackboard.document_profile,
            None,
            ToolResult(status="ok", payload={}),
        ),
    )

    class BoomExecutor:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("ReActExecutor must not run when skip_shard_plan")

        def run(self):  # pragma: no cover
            raise AssertionError("unreachable")

    monkeypatch.setattr(coordinator_module, "ReActExecutor", BoomExecutor)

    anatomy = coordinator.run_structural(skip_shard_plan=True)

    assert anatomy.shard_plan.enabled is False
    assert len(anatomy.shard_plan.shards) == 1
    assert anatomy.shard_plan.shards[0].page_start == 1
    assert anatomy.shard_plan.shards[0].page_end == 4
    assert anatomy.toc_result.toc_pages == [1]


def test_run_structural_trusts_rejected_all_toc_and_fails_open(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-rejected-all-toc-fail-open",
        output_dir=str(tmp_path / "profile"),
    )
    (tmp_path / "profile").mkdir()
    _seed_preprobed_pages(coordinator, page_count=3, pages=[1, 2])
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category="Prospectus",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )
    coordinator.blackboard.toc_result = TocResult(
        candidates=[
            TocAnchorPage(
                page=17,
                png_path="/tmp/toc_anchor_page_17.png",
                source="text_scan",
            )
        ],
        method="none",
        notes="VLM rejected all TOC anchor candidates",
        failure_kind="rejected_all",
    )

    def fake_toc_extraction() -> None:
        raise AssertionError("rejected_all should be trusted and not retried")

    calls: list[str] = []

    def fake_persist(_anatomy):
        calls.append("persist")

    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc_extraction)
    monkeypatch.setattr(coordinator, "_persist_ready_anatomy", fake_persist)

    monkeypatch.setattr(
        coordinator_module.ProfilePlanner,
        "propose",
        lambda self: (
            coordinator.blackboard.document_profile,
            None,
            ToolResult(status="ok", payload={}),
        ),
    )

    class FakeExecutor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self):
            coordinator.blackboard.shard_plan = ShardPlan(
                enabled=True,
                reason="too_large",
                shards=[
                    Shard(
                        shard_index=0,
                        page_start=1,
                        page_end=3,
                        page_offset=0,
                        anchor_type="forced_max_size",
                        anchor_evidence="fixture",
                        confidence=1.0,
                    )
                ],
            )
            return SimpleNamespace(
                success=True,
                verdict=SimpleNamespace(status="success", rationale="ok"),
                trace_summary={},
            )

    monkeypatch.setattr(coordinator_module, "ReActExecutor", FakeExecutor)

    anatomy = coordinator.run_structural()

    assert calls == ["persist"]
    assert anatomy.toc_result.failure_kind == "rejected_all"
    assert anatomy.toc_result.toc_pages == []


def test_run_coarse_runs_planner_then_text_scan_then_toc(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-planner-then-toc",
        output_dir=str(tmp_path / "profile"),
        settings={"toc_profile_enabled": True},
    )
    (tmp_path / "profile").mkdir()
    _seed_preprobed_pages(coordinator, page_count=3, pages=[1, 2])

    calls: list[str] = []

    def fake_toc_extraction() -> None:
        calls.append("toc")
        coordinator.blackboard.toc_result = TocResult(toc_pages=[17], method="vlm_batch")
        coordinator.blackboard.toc_hierarchies = [
            {"toc_range": [17, 17], "toc_range_unit": "page", "toc_tree": {}}
        ]

    def fake_text_scan() -> None:
        calls.append("text_scan")
        coordinator.blackboard.page_full_text_cache = {1: "a", 2: "b", 3: "c"}

    def fake_persist(_anatomy):
        calls.append("persist")

    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc_extraction)
    monkeypatch.setattr(coordinator, "_run_text_scan", fake_text_scan)
    monkeypatch.setattr(coordinator, "_persist_ready_anatomy", fake_persist)

    def fake_propose(_self):
        calls.append("planner")
        return (
            DocumentProfile(
                is_scanned=False,
                category="Prospectus",
                routing_category=PdfRoutingCategory.GENERIC.value,
            ),
            None,
            ToolResult(status="ok", payload={}),
        )

    monkeypatch.setattr(coordinator_module.ProfilePlanner, "propose", fake_propose)

    class FakeExecutor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self):
            coordinator.blackboard.shard_plan = ShardPlan(
                enabled=True,
                reason="too_large",
                shards=[
                    Shard(
                        shard_index=0,
                        page_start=1,
                        page_end=3,
                        page_offset=0,
                        anchor_type="forced_max_size",
                        anchor_evidence="fixture",
                        confidence=1.0,
                    )
                ],
            )
            return SimpleNamespace(
                success=True,
                verdict=SimpleNamespace(status="success", rationale="ok"),
                trace_summary={},
            )

    monkeypatch.setattr(coordinator_module, "ReActExecutor", FakeExecutor)

    coordinator.run_coarse()
    anatomy = coordinator.run_structural()

    assert calls == ["planner", "text_scan", "toc", "persist"]
    assert anatomy.toc_result.toc_pages == [17]


def test_run_text_scan_native_uses_read_page_texts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-scan-native",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category="Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )

    def fake_read(_pdf_path: str, pages: list[int], timeout: int = 300) -> dict[int, str]:
        assert pages == [1, 2]
        return {1: "a", 2: "b"}

    monkeypatch.setattr(coordinator_module, "read_page_texts", fake_read)
    coordinator._run_text_scan()
    assert coordinator.blackboard.page_full_text_cache == {1: "a", 2: "b"}


def test_run_text_scan_scanned_dispatches_ocr_pages(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-scan-ocr",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=True,
        category="Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )

    def fake_dispatch(name: str, ctx, args):
        assert name == "ocr.pages"
        assert args == {"pages": [1, 2]}
        ctx.blackboard.page_full_text_cache = {1: "ocr-1", 2: "ocr-2"}
        return ToolResult(status="ok", payload={})

    monkeypatch.setattr(coordinator_module.REGISTRY, "dispatch", fake_dispatch)
    coordinator._run_text_scan()
    assert coordinator.blackboard.page_full_text_cache == {1: "ocr-1", 2: "ocr-2"}


def test_anchor_confirmation_failure_requires_one_strict_retry(tmp_path: Path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-confirm-failed-not-suspect",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.toc_result = TocResult(
        candidates=[
            TocAnchorPage(
                page=17,
                png_path="/tmp/toc_anchor_page_17.png",
                source="text_scan",
            )
        ],
        method="none",
        notes="VLM anchor confirmation failed; TOC candidates left unconfirmed",
        failure_kind="confirm_failed",
    )

    assert coordinator._toc_result_requires_strict_retry() is True


def test_oversized_single_shard_plan_is_invalid() -> None:
    report = validate_shard_plan(
        ShardPlan(
            enabled=False,
            reason="not_needed",
            shards=[
                Shard(
                    shard_index=0,
                    page_start=1,
                    page_end=407,
                    page_offset=0,
                    anchor_type="forced_max_size",
                    anchor_evidence="final shard",
                    confidence=1.0,
                )
            ],
        ),
        page_count=407,
        min_pages=20,
        max_pages=200,
    )

    assert report.valid is False
    assert report.errors == ["shard 0 exceeds max_pages=200"]


def test_standard_pdf_profile_builds_page_toc_and_lightweight_anatomy_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_instances: list[object] = []
    fake_anatomy = object()
    init_settings: list[dict[str, object]] = []

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self.calls: list[str] = []
            init_settings.append(kwargs["settings"])
            self.blackboard = SimpleNamespace(
                page_count=2,
                doc_stats={"page_count": 2},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
                page_full_text_cache={},
            )
            fake_instances.append(self)

        def run_coarse(self) -> DocumentProfile:
            self.calls.append("run_coarse")
            self.blackboard.toc_result = TocResult(method="none")
            return DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self, *, skip_shard_plan: bool = False):
            self.calls.append("run_lightweight_anatomy")
            return fake_anatomy

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)
    monkeypatch.setattr(doc_profiler.settings, "PDF_PROFILE_TOC_ENABLED", True)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-page-toc",
        output_dir=str(tmp_path),
    )

    assert profile.toc.has_toc is False
    assert profile.toc.attempted is True
    assert profile.anatomy is fake_anatomy
    assert fake_instances[0].calls == ["run_coarse", "run_lightweight_anatomy"]
    assert init_settings[0]["toc_profile_enabled"] is True
    assert "toc_before_coarse" not in init_settings[0]


def test_standard_pdf_page_toc_kill_switch_builds_no_toc_anatomy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_anatomy = object()
    init_settings: list[dict[str, object]] = []

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self.calls: list[str] = []
            init_settings.append(kwargs["settings"])
            self.blackboard = SimpleNamespace(
                page_count=2,
                doc_stats={"page_count": 2},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
                page_full_text_cache={},
            )

        def run_coarse(self) -> DocumentProfile:
            self.calls.append("run_coarse")
            return DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self, *, skip_shard_plan: bool = False):
            self.calls.append("run_lightweight_anatomy")
            self.blackboard.toc_result = TocResult(
                method="none",
                notes="TOC profiling disabled by PDF_PROFILE_TOC_ENABLED",
            )
            self.blackboard.global_signals["toc_profile_attempted"] = False
            return fake_anatomy

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "PDF_PROFILE_TOC_ENABLED", False)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-page-toc-disabled",
        output_dir=str(tmp_path),
    )

    assert init_settings[0]["toc_profile_enabled"] is False
    assert "toc_before_coarse" not in init_settings[0]
    assert profile.toc.attempted is False
    assert profile.toc.has_toc is False
    assert profile.toc.notes == "TOC profiling disabled by PDF_PROFILE_TOC_ENABLED"
    assert profile.anatomy is fake_anatomy


def test_page_memory_forces_toc_profiling_despite_kill_switch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_anatomy = object()
    init_settings: list[dict[str, object]] = []

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self.calls: list[str] = []
            init_settings.append(kwargs["settings"])
            self.blackboard = SimpleNamespace(
                page_count=2,
                doc_stats={"page_count": 2},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
                page_full_text_cache={},
            )

        def run_coarse(self) -> DocumentProfile:
            self.calls.append("run_coarse")
            self.blackboard.toc_result = TocResult(method="none")
            return DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self, *, skip_shard_plan: bool = False):
            self.calls.append("run_lightweight_anatomy")
            return fake_anatomy

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)
    # Global kill switch OFF; page_memory track must still enable TOC.
    monkeypatch.setattr(doc_profiler.settings, "PDF_PROFILE_TOC_ENABLED", False)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-page-memory-toc-forced",
        output_dir=str(tmp_path),
        skip_shard_plan=True,
        oversized_policy="page_memory",
    )

    assert init_settings[0]["toc_profile_enabled"] is True
    assert "toc_before_coarse" not in init_settings[0]
    assert profile.anatomy is fake_anatomy


def test_page_memory_profile_bypasses_chunk_oversized_gate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_anatomy = object()
    fake_instances = []

    class FakeCoordinator:
        def __init__(self, **_kwargs) -> None:
            self.calls: list[str] = []
            self.blackboard = SimpleNamespace(
                page_count=201,
                doc_stats={"page_count": 201},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
                page_full_text_cache={},
            )
            fake_instances.append(self)

        def run_coarse(self) -> DocumentProfile:
            self.calls.append("run_coarse")
            return DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_structural(self, *, skip_shard_plan: bool = False):
            self.calls.append("run_structural")
            self.skip_shard_plan = skip_shard_plan
            return fake_anatomy

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)
    monkeypatch.setattr(doc_profiler.settings, "OVERSIZED_PDF_SHARD_ENABLED", False)

    profile = profile_document(
        str(tmp_path / "oversized.pdf"),
        "oversized.pdf",
        job_id="job-page-memory-oversized",
        output_dir=str(tmp_path),
        skip_shard_plan=True,
        oversized_policy="page_memory",
    )

    assert profile.anatomy is fake_anatomy
    assert fake_instances[0].calls == ["run_coarse", "run_structural"]
    assert fake_instances[0].skip_shard_plan is True


def test_standard_pdf_profile_maps_page_toc_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_anatomy = object()

    class FakeCoordinator:
        def __init__(self, **_kwargs) -> None:
            self.calls: list[str] = []
            self.blackboard = SimpleNamespace(
                page_count=2,
                doc_stats={"page_count": 2},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
                page_full_text_cache={},
            )

        def run_coarse(self) -> DocumentProfile:
            self.calls.append("run_coarse")
            self.blackboard.toc_result = TocResult(
                toc_pages=[2],
                evidence=[
                    AgentTocEvidence(
                        page_index=2,
                        source="vlm",
                        confidence=0.95,
                        reason="table of contents",
                    )
                ],
                method="vlm_batch",
            )
            self.blackboard.toc_hierarchies = [
                {"toc_range": [2, 2], "toc_range_unit": "page", "toc_tree": {}}
            ]
            return DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self, *, skip_shard_plan: bool = False):
            self.calls.append("run_lightweight_anatomy")
            return fake_anatomy

    fake_instances: list[FakeCoordinator] = []

    class CapturingCoordinator(FakeCoordinator):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            fake_instances.append(self)

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", CapturingCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-page-toc-evidence",
        output_dir=str(tmp_path),
    )

    assert fake_instances[0].calls == [
        "run_coarse",
        "run_lightweight_anatomy",
    ]
    assert profile.toc.has_toc is True
    assert profile.toc.attempted is True
    assert profile.toc.method == "vlm_batch"
    assert profile.toc.evidence[0].confidence == 0.95
    assert profile.anatomy is fake_anatomy


def test_oversized_atlas_surfaces_profile_toc_without_structural_anatomy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FakeCoordinator:
        def __init__(self, **_kwargs) -> None:
            self.blackboard = SimpleNamespace(
                page_count=250,
                doc_stats={"page_count": 250},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
                page_full_text_cache={},
            )

        def run_coarse(self) -> DocumentProfile:
            calls.append("run_coarse")
            self.blackboard.toc_result = TocResult(
                toc_pages=[4],
                evidence=[
                    AgentTocEvidence(
                        page_index=4,
                        source="vlm",
                        confidence=0.9,
                        reason="table of contents",
                    )
                ],
                method="vlm_batch",
            )
            self.blackboard.toc_hierarchies = [
                {"toc_range": [4, 4], "toc_range_unit": "page", "toc_tree": {}}
            ]
            return DocumentProfile(
                is_scanned=False,
                category="Engineering Atlas",
                routing_category=PdfRoutingCategory.ATLAS.value,
            )

        def run_structural(self, *, skip_shard_plan: bool = False):
            raise AssertionError("oversized atlas should not run structural anatomy")

        def run_lightweight_anatomy(self, *, skip_shard_plan: bool = False):
            raise AssertionError("oversized atlas should not run lightweight anatomy")

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)
    monkeypatch.setattr(doc_profiler.settings, "OVERSIZED_PDF_SHARD_ENABLED", True)
    monkeypatch.setattr(doc_profiler.settings, "OVERSIZED_PDF_SOFT_LIMIT", 500)

    profile = profile_document(
        str(tmp_path / "oversized-atlas.pdf"),
        "oversized-atlas.pdf",
        job_id="job-oversized-atlas",
        output_dir=str(tmp_path),
    )

    assert calls == ["run_coarse"]
    assert profile.is_atlas is True
    assert profile.anatomy is None
    assert profile.toc.has_toc is True
    assert profile.toc.attempted is True
    assert profile.toc.toc_pages == [4]


def _patch_atlas_page_pipeline(
    monkeypatch,
    page_texts: list[str],
) -> list[set[int]]:
    render_skip_pages: list[set[int]] = []

    def fake_run_in_child_process(func, *_args, **_kwargs):
        if func is atlas_parser._atlas_extract_texts_worker:
            return {"total_pages": len(page_texts), "page_texts": page_texts}

        if func is atlas_parser._atlas_render_pages_worker:
            _pdf_path, img_dir, skip_pages_list, _dpi, page_texts_list = _args
            skip_pages = set(skip_pages_list)
            render_skip_pages.append(skip_pages)
            page_data = []
            for page_idx, page_text in enumerate(page_texts_list):
                page_num = page_idx + 1
                if page_num in skip_pages:
                    continue
                img_name = f"page-{page_num}.jpg"
                Path(img_dir, img_name).write_bytes(f"image-{page_num}".encode())
                page_data.append((page_num, page_text, img_name))
            return {"page_data": page_data}

        raise AssertionError(f"unexpected atlas worker: {func}")

    monkeypatch.setattr(atlas_parser, "run_in_child_process", fake_run_in_child_process)
    monkeypatch.setattr(
        atlas_parser,
        "_vlm_extract_page_info",
        lambda _output_dir, img_name: f"Drawing info for {img_name}",
    )
    return render_skip_pages


def test_atlas_consumes_profile_toc_without_internal_detector(
    monkeypatch,
    tmp_path: Path,
) -> None:
    render_skip_pages = _patch_atlas_page_pipeline(
        monkeypatch,
        ["Cover", "Contents", "Drawing page"],
    )

    profile = SimpleNamespace(
        is_scanned=False,
        toc=ParserTocProfile(
            toc_pages=[2],
            hierarchies=[
                {"toc_range": [2, 2], "toc_range_unit": "page", "toc_tree": {}}
            ],
            source="pdf_vlm",
            method="vlm_batch",
            attempted=True,
        ),
    )

    df = atlas_parser.parse_atlas(
        str(tmp_path / "atlas.pdf"),
        str(tmp_path / "out"),
        {"stopwords": [], "model_name": "test-model"},
        relative_root="kb/atlas.pdf",
        profile=profile,
    )

    assert render_skip_pages == [{2}]
    assert len(df) == 2
    assert not any("[page-2]" in content for content in df["content"])
    toc_json = tmp_path / "out" / "toc_hierarchies.json"
    assert toc_json.exists()


def test_atlas_does_not_fallback_when_profile_toc_not_attempted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    render_skip_pages = _patch_atlas_page_pipeline(
        monkeypatch,
        ["Contents", "Drawing page"],
    )
    profile = SimpleNamespace(
        is_scanned=False,
        toc=ParserTocProfile(toc_pages=[2], attempted=False),
    )

    df = atlas_parser.parse_atlas(
        str(tmp_path / "atlas.pdf"),
        str(tmp_path / "out"),
        {
            "stopwords": [],
            "model_name": "content-model",
            "hierarchy_model_name": "hierarchy-model",
        },
        relative_root="kb/atlas.pdf",
        profile=profile,
    )

    assert render_skip_pages == [set()]
    assert len(df) == 2
    assert any("[page-1]" in content for content in df["content"])


def test_pdf_standard_single_pass_skips_markdown_toc_detection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    calls: dict[str, object] = {}

    def fake_parse_via_full(_pdf_path, _filename, out_dir, s3_key=None):
        calls["s3_key"] = s3_key
        Path(out_dir, "full.md").write_text("Contents\nBody\n", encoding="utf-8")

    def fake_parse_md(*_args, **kwargs):
        calls["skip_toc_detection"] = kwargs.get("skip_toc_detection")
        return {"ok": True}

    monkeypatch.setattr(pdf_parser, "parse_via_full", fake_parse_via_full)
    monkeypatch.setattr(pdf_parser, "parse_md", fake_parse_md)

    result = pdf_parser.parse_pdfs(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        str(output_dir),
        {"smart_title_parse": False},
        profile=SimpleNamespace(
            routing_category=PdfRoutingCategory.GENERIC,
            anatomy=None,
            page_count=2,
        ),
        s3_key="uploads/source.pdf",
    )

    assert result == {"ok": True}
    assert calls == {"s3_key": "uploads/source.pdf", "skip_toc_detection": True}


def test_pdf_shard_pipeline_accepts_single_shard_fast_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    calls: list[str] = []

    def fake_parse_via_full(pdf_path, filename, out_dir, s3_key=None):
        calls.append(f"parse:{filename}:{s3_key}")
        Path(out_dir, "full.md").write_text("1. Introduction\nBody\n", encoding="utf-8")

    def fail_split(*_args, **_kwargs):
        raise AssertionError("single shard without TOC should not split")

    def fake_eval_md_headings(md_lines, *_args, **_kwargs):
        return [f"# {line}" if line.startswith("1.") else line for line in md_lines]

    def fake_parse_md(*_args, **kwargs):
        calls.append("parse_md")
        return {"lines": kwargs["lines_with_heading"]}

    active_markdown_parser = importlib.import_module(
        "app.services.document_parser.formats.markdown.parser"
    )
    monkeypatch.setattr(pdf_parser, "parse_via_full", fake_parse_via_full)
    monkeypatch.setattr(shard_splitter, "split_pdf", fail_split)
    monkeypatch.setattr(
        active_markdown_parser,
        "eval_md_headings",
        fake_eval_md_headings,
    )
    monkeypatch.setattr(pdf_parser, "parse_md", fake_parse_md)

    profile = SimpleNamespace(
        anatomy=PageAnatomyMap(
            job_id="job-single-shard",
            file_path=str(tmp_path / "standard.pdf"),
            page_count=2,
            page_features=[_page_feature(1), _page_feature(2)],
            page_labels=[
                PageLabel(page=1, kind="normal", confidence=1.0),
                PageLabel(page=2, kind="normal", confidence=1.0),
            ],
            toc_result=TocResult(method="none"),
            h1_result=H1BoundaryResult(method="none"),
            shard_plan=ShardPlan(
                enabled=False,
                reason="not_needed",
                shards=[
                    Shard(
                        shard_index=0,
                        page_start=1,
                        page_end=2,
                        page_offset=0,
                        anchor_type="forced_max_size",
                        anchor_evidence="document within shard threshold",
                        confidence=1.0,
                    )
                ],
            ),
        )
    )

    result = pdf_parser._parse_pdf_via_shards(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        str(output_dir),
        {"smart_title_parse": False, "model_name": "test-model"},
        profile=profile,
        s3_key="uploads/source.pdf",
    )

    assert calls == ["parse:standard.pdf:uploads/source.pdf", "parse_md"]
    assert result["lines"] == ["# 1. Introduction", "Body"]


def test_pdf_shard_pipeline_does_not_use_markdown_toc_detector(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    heading_contexts: list[object] = []

    def fake_parse_via_full(_pdf_path, _filename, out_dir, s3_key=None):
        Path(out_dir, "full.md").write_text(
            "Contents\n1 Introduction .... 2\n1 Introduction\nBody\n",
            encoding="utf-8",
        )

    def fail_detect_tocs_in_texts(*_args, **_kwargs):
        raise AssertionError("PDF shard pipeline must not call markdown TOC detector")

    def fake_eval_md_headings(md_lines, *_args, **kwargs):
        heading_contexts.append(kwargs.get("toc_hierarchies"))
        return [f"# {line}" if line.startswith("1 ") else line for line in md_lines]

    active_markdown_parser = importlib.import_module(
        "app.services.document_parser.formats.markdown.parser"
    )
    active_toc_parser = importlib.import_module(
        "app.services.document_parser.structure.toc_parser"
    )
    monkeypatch.setattr(pdf_parser, "parse_via_full", fake_parse_via_full)
    monkeypatch.setattr(
        active_toc_parser,
        "detect_tocs_in_texts",
        fail_detect_tocs_in_texts,
    )
    monkeypatch.setattr(
        active_markdown_parser,
        "eval_md_headings",
        fake_eval_md_headings,
    )
    monkeypatch.setattr(
        pdf_parser,
        "parse_md",
        lambda *_args, **kwargs: {"lines": kwargs["lines_with_heading"]},
    )

    profile = SimpleNamespace(
        anatomy=PageAnatomyMap(
            job_id="job-missed-toc",
            file_path=str(tmp_path / "standard.pdf"),
            page_count=3,
            page_features=[_page_feature(1), _page_feature(2), _page_feature(3)],
            page_labels=[
                PageLabel(page=1, kind="normal", confidence=1.0),
                PageLabel(page=2, kind="normal", confidence=1.0),
                PageLabel(page=3, kind="normal", confidence=1.0),
            ],
            toc_result=TocResult(method="none"),
            h1_result=H1BoundaryResult(method="none"),
            shard_plan=ShardPlan(
                enabled=False,
                reason="not_needed",
                shards=[
                    Shard(
                        shard_index=0,
                        page_start=1,
                        page_end=3,
                        page_offset=0,
                        anchor_type="forced_max_size",
                        anchor_evidence="document within shard threshold",
                        confidence=1.0,
                    )
                ],
            ),
        )
    )

    result = pdf_parser._parse_pdf_via_shards(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        str(output_dir),
        {"smart_title_parse": False, "model_name": "test-model"},
        profile=profile,
    )

    assert heading_contexts == [None]
    assert result["lines"] == [
        "Contents",
        "# 1 Introduction .... 2",
        "# 1 Introduction",
        "Body",
    ]


def test_page_based_toc_demotes_front_matter_only_on_first_shard() -> None:
    toc_hierarchies = [
        {
            "toc_range": [2, 2],
            "toc_range_unit": "page",
            "toc_tree": {"Risk Factors": {}},
        }
    ]
    lines = [
        "1. Cover",
        "2. Legal Notice",
        "3. Risk Factors",
        "4. Business",
    ]

    first_shard = pred_titles(
        lines,
        doc_type="md",
        toc_hierarchies=toc_hierarchies,
        smart_parse=False,
        is_first_shard=True,
    )
    continuation = pred_titles(
        lines,
        doc_type="md",
        toc_hierarchies=toc_hierarchies,
        smart_parse=False,
        is_first_shard=False,
    )

    first_levels = dict(zip(first_shard["id"], first_shard["level"], strict=False))
    continuation_levels = dict(
        zip(continuation["id"], continuation["level"], strict=False)
    )
    assert first_levels[0] == -1
    assert first_levels[1] == -1
    assert first_levels[2] > 0
    assert continuation_levels[0] > 0
