from __future__ import annotations

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
from app.services.document_parser.profiling import doc_profiler
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.profiling.profile_model import (
    ParserDocumentProfile,
)
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory


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
        PageLabel(page=page, kind="normal") for page in probed_pages
    ]
    coordinator.blackboard.doc_stats = {"page_count": page_count}
    coordinator.blackboard.global_signals["page_kind_counts"] = {
        "normal": page_count
    }
    coordinator.blackboard.global_signals["assets_probed"] = True


def test_toc_anchor_text_scan_line_contains_keyword_and_split_repair() -> None:
    late_lines = [f"body line {idx}" for idx in range(60)] + ["目录"]
    split_lines = ["Table of", "Con", "tents"]
    # Containment (not whole-line equality): prefixed titles and body mentions hit.
    contains_lines = [
        "General table of contents",
        "Commentary provides guidance on minimum cement contents in different situations.",
        "The basic contents of a typical contract document are shown below:",
    ]

    keywords = toc_anchor_tool.TOC_KEYWORDS
    late_matches = toc_anchor_tool._find_toc_text_matches(  # noqa: SLF001
        late_lines, keywords=keywords
    )
    split_matches = toc_anchor_tool._find_toc_text_matches(  # noqa: SLF001
        split_lines, keywords=keywords
    )
    contains_matches = toc_anchor_tool._find_toc_text_matches(  # noqa: SLF001
        contains_lines, keywords=keywords
    )

    assert late_matches[0]["line_index"] == 60
    assert late_matches[0]["match_kind"] == "keyword:目录"
    assert split_matches[0]["match_kind"] == "keyword:table of contents"
    assert split_matches[0]["line_index"] == 0
    assert split_matches[0]["line_end_index"] == 2
    assert [m["match_kind"] for m in contains_matches] == [
        "keyword:table of contents",
        "keyword:contents",
        "keyword:contents",
    ]


def test_toc_extraction_raises_on_pipeline_failure(tmp_path: Path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "standard.pdf"),
        job_id="job-toc-fail-hard",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.page_count = 1
    coordinator.blackboard.page_features = [_page_feature()]

    def _fail_toc_extraction() -> None:
        raise RuntimeError("VLM JSON parse failed")

    coordinator._run_toc_extraction_pipeline = _fail_toc_extraction  # type: ignore[method-assign]

    try:
        coordinator._ensure_toc_profile(strict=False)
        raise AssertionError("expected TOC pipeline failure to raise")
    except RuntimeError as exc:
        assert "VLM JSON parse failed" in str(exc)

    assert coordinator.blackboard.toc_result is None
    assert coordinator.blackboard.toc_hierarchies is None


def test_run_lightweight_anatomy_builds_single_shard_without_coarse_vlm(
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
        category=["OTHER"], category_label="Research Report",
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
    assert "budget" not in trace_data["summary"]


def test_run_coarse_runs_asset_probe_after_coarse_profile(monkeypatch, tmp_path: Path) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-asset-probe-after-coarse",
        output_dir=str(tmp_path / "profile"),
    )
    (tmp_path / "profile").mkdir()
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.page_features = [_page_feature(1), _page_feature(2)]
    coordinator.blackboard.page_labels = [
        PageLabel(page=1, kind="normal"),
        PageLabel(page=2, kind="normal"),
    ]
    coordinator.blackboard.doc_stats = {"page_count": 2}
    coordinator.blackboard.global_signals["page_kind_counts"] = {"normal": 2}

    calls: list[str] = []

    def fake_classify(_self):
        calls.append("coarse_profile")
        return (
            DocumentProfile(
                is_scanned=False,
                category=["OTHER"], category_label="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            ),
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

    monkeypatch.setattr(coordinator_module.CoarseProfiler, "classify", fake_classify)
    monkeypatch.setattr(coordinator_module, "probe_page_assets", fake_probe_page_assets)
    monkeypatch.setattr(coordinator_module, "aggregate_doc_stats", fake_aggregate)
    monkeypatch.setattr(coordinator, "_run_text_scan", fake_text_scan)
    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc)

    profile = coordinator.run_coarse()

    assert profile.category == ["OTHER"]
    assert profile.category_label == "Research Report"
    assert calls == [
        "coarse_profile",
        "text_scan",
        "probe.page_assets",
        "aggregate.doc_stats",
        "toc",
    ]
    assert coordinator.blackboard.global_signals["assets_probed"] is True


def test_parse_run_recorder_doc_profile_uses_final_anatomy_toc() -> None:
    recorder = ParseRunRecorder(job_id="job-doc-profile")
    recorder.set_doc_profile(
        ParserDocumentProfile(
            file_type="pdf",
            category=["04000000"],
            category_label="Financial Prospectus",
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
            PageLabel(page=1, kind="normal"),
            PageLabel(page=2, kind="normal"),
        ],
        toc_result=TocResult(toc_pages=[2], method="vlm_batch", notes="ok"),
        shard_plan=ShardPlan(enabled=False, reason="not_needed"),
        toc_hierarchies=[{"toc_range": [2, 2], "toc_tree": {}}],
        global_signals={"toc_profile_attempted": True},
    )

    doc_profile = recorder._doc_profile_for_plan()  # noqa: SLF001

    assert doc_profile is not None
    assert doc_profile["category"] == ["04000000"]
    assert doc_profile["category_label"] == "Financial Prospectus"
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
            category=["OTHER"],
            category_label="Atlas",
            routing_category=PdfRoutingCategory.ATLAS,
            page_count=12,
        )
    )

    assert len(db.rows) == 1
    assert db.rows[0].doc_profile["category"] == ["OTHER"]
    assert db.rows[0].doc_profile["category_label"] == "Atlas"
    assert db.rows[0].shard_plan is None

    recorder._anatomy = PageAnatomyMap(  # noqa: SLF001
        job_id="job-profile-only",
        file_path="/tmp/doc.pdf",
        page_count=12,
        page_features=[_page_feature(1)],
        page_labels=[PageLabel(page=1, kind="normal")],
        toc_result=TocResult(toc_pages=[2], method="vlm_batch", notes="ok"),
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
        category=["OTHER"], category_label="Prospectus",
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
        coordinator_module.CoarseProfiler,
        "classify",
        lambda self: (
            coordinator.blackboard.document_profile,
            ToolResult(status="ok", payload={}),
        ),
    )

    anatomy = coordinator.run_structural()

    assert calls == ["toc", "persist"]
    assert anatomy.toc_result.toc_pages == [17]


def test_run_structural_uses_single_shard_placeholder_without_finalize(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-structural-single-shard",
        output_dir=str(tmp_path / "profile"),
    )
    (tmp_path / "profile").mkdir()
    _seed_preprobed_pages(coordinator, page_count=4)
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category=["OTHER"], category_label="Prospectus",
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
        coordinator_module.CoarseProfiler,
        "classify",
        lambda self: (
            coordinator.blackboard.document_profile,
            ToolResult(status="ok", payload={}),
        ),
    )

    anatomy = coordinator.run_structural()

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
        category=["OTHER"], category_label="Prospectus",
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
        coordinator_module.CoarseProfiler,
        "classify",
        lambda self: (
            coordinator.blackboard.document_profile,
            ToolResult(status="ok", payload={}),
        ),
    )

    anatomy = coordinator.run_structural()

    assert calls == ["persist"]
    assert anatomy.toc_result.failure_kind == "rejected_all"
    assert anatomy.toc_result.toc_pages == []


def test_run_coarse_runs_coarse_profile_then_text_scan_then_toc(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-coarse-then-toc",
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

    def fake_classify(_self):
        calls.append("coarse_profile")
        return (
            DocumentProfile(
                is_scanned=False,
                category=["OTHER"], category_label="Prospectus",
                routing_category=PdfRoutingCategory.GENERIC.value,
            ),
            ToolResult(status="ok", payload={}),
        )

    monkeypatch.setattr(coordinator_module.CoarseProfiler, "classify", fake_classify)

    coordinator.run_coarse()
    anatomy = coordinator.run_structural()

    assert calls == ["coarse_profile", "text_scan", "toc", "persist"]
    assert anatomy.toc_result.toc_pages == [17]


def test_run_text_scan_native_uses_read_page_text_bands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services.document_agent.pdf_text import (
        LineDraft,
        PageTextBands,
        build_page_text_bands,
    )

    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-scan-native",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category=["OTHER"], category_label="Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
        header_y=0.06,
        footer_y=0.95,
    )
    page_one = build_page_text_bands(
        (
            LineDraft(text="h1", region="header"),
            LineDraft(text="a"),
            LineDraft(text="f1", region="footer"),
        )
    )
    page_two = build_page_text_bands(
        (
            LineDraft(text="b"),
            LineDraft(text="f2", region="footer"),
        )
    )

    def fake_read(
        _pdf_path: str,
        pages: list[int],
        *,
        header_y: float | None = None,
        footer_y: float | None = None,
        timeout: int = 300,
    ) -> dict[int, PageTextBands]:
        assert pages == [1, 2]
        assert header_y == 0.06
        assert footer_y == 0.95
        return {1: page_one, 2: page_two}

    monkeypatch.setattr(coordinator_module, "read_page_text_bands", fake_read)
    coordinator._run_text_scan()
    assert coordinator.blackboard.page_full_text_cache == {
        1: page_one,
        2: page_two,
    }
    assert coordinator.blackboard.page_text_search_view is None


def test_run_text_scan_scanned_uses_read_page_text_bands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services.document_agent.pdf_text import (
        LineDraft,
        PageTextBands,
        build_page_text_bands,
    )

    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-scan-bands",
        output_dir=str(tmp_path / "profile"),
    )
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.document_profile = DocumentProfile(
        is_scanned=True,
        category=["OTHER"], category_label="Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
        header_y=0.06,
        footer_y=0.95,
    )
    page_one = build_page_text_bands(
        (
            LineDraft(text="CONTENTS", region="header"),
            LineDraft(text="body"),
        )
    )
    page_two = build_page_text_bands((LineDraft(text="f2", region="footer"),))
    dispatched: list[str] = []

    def fake_read(
        _pdf_path: str,
        pages: list[int],
        *,
        header_y: float | None = None,
        footer_y: float | None = None,
        timeout: int = 300,
    ) -> dict[int, PageTextBands]:
        assert pages == [1, 2]
        assert header_y == 0.06
        assert footer_y == 0.95
        return {1: page_one, 2: page_two}

    def fake_dispatch(name: str, _ctx, _args):
        dispatched.append(name)
        return ToolResult(status="ok", payload={})

    monkeypatch.setattr(coordinator_module, "read_page_text_bands", fake_read)
    monkeypatch.setattr(coordinator_module.REGISTRY, "dispatch", fake_dispatch)
    coordinator._run_text_scan()
    assert dispatched == []
    assert coordinator.blackboard.page_full_text_cache == {
        1: page_one,
        2: page_two,
    }
    assert coordinator.blackboard.page_text_search_view is None


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
                )
            ],
        ),
        page_count=407,
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
                category=["OTHER"], category_label="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self):
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
                category=["OTHER"], category_label="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self):
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
                category=["OTHER"], category_label="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self):
            self.calls.append("run_lightweight_anatomy")
            return fake_anatomy

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)
    # Kill switch OFF for chunk; page_memory must still force TOC on.
    monkeypatch.setattr(doc_profiler.settings, "PDF_PROFILE_TOC_ENABLED", False)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-page-memory-toc-forced",
        output_dir=str(tmp_path),
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
                category=["OTHER"], category_label="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_structural(self):
            self.calls.append("run_structural")
            return fake_anatomy

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)
    monkeypatch.setattr(doc_profiler.settings, "OVERSIZED_PDF_SHARD_ENABLED", False)

    profile = profile_document(
        str(tmp_path / "oversized.pdf"),
        "oversized.pdf",
        job_id="job-page-memory-oversized",
        output_dir=str(tmp_path),
        oversized_policy="page_memory",
    )

    assert profile.anatomy is fake_anatomy
    assert fake_instances[0].calls == ["run_coarse", "run_structural"]


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
                category=["OTHER"], category_label="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_lightweight_anatomy(self):
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
    assert profile.toc.evidence[0].source == "vlm"
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
                category=["OTHER"], category_label="Engineering Atlas",
                routing_category=PdfRoutingCategory.ATLAS.value,
            )

        def run_structural(self):
            raise AssertionError("oversized atlas should not run structural anatomy")

        def run_lightweight_anatomy(self):
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


def test_find_toc_anchor_pages_scanned_unions_text_and_visual(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services.document_agent.manifest import ToolContext
    from app.services.document_agent.pdf_text import LineDraft, build_page_text_bands
    from app.services.document_agent.state import ProfileBlackboard

    blackboard = ProfileBlackboard(page_count=20)
    blackboard.document_profile = DocumentProfile(
        is_scanned=True,
        category=["OTHER"],
        category_label="Scanned Book",
        routing_category="scanned",
    )
    blackboard.page_labels = [PageLabel(page=page, kind="normal") for page in range(1, 21)]
    blackboard.page_full_text_cache = {
        14: build_page_text_bands((LineDraft(text="CONTENTS"),)),
        15: build_page_text_bands((LineDraft(text="CONTENTS"),)),
    }
    ctx = ToolContext(
        pdf_path=str(tmp_path / "scan.pdf"),
        job_id="job-scan-toc",
        blackboard=blackboard,
        trace=None,
        output_dir=str(tmp_path / "out"),
        settings={"toc_visual_candidate_pages": [14, 15, 16]},
    )

    def fake_render(_worker, _pdf_path, pages, output_dir, _dpi, timeout=120):
        del timeout
        results = []
        for page in pages:
            png = Path(output_dir) / f"toc_anchor_page_{page}.png"
            png.write_bytes(b"png")
            results.append({"page": page, "png_path": str(png)})
        return {"ok": True, "results": results}

    monkeypatch.setattr(toc_anchor_tool, "run_in_child_process", fake_render)
    result = toc_anchor_tool.find_toc_anchor_pages(ctx, {})
    pages = [item.page for item in blackboard.toc_anchor_pages]
    sources = {item.page: item.source for item in blackboard.toc_anchor_pages}
    assert result.status == "ok"
    assert pages == [14, 15, 16]
    assert sources == {14: "hybrid", 15: "hybrid", 16: "visual"}
    assert result.output_summary is not None
    assert result.output_summary["narrowing"]["union"] == 3


def test_find_toc_anchor_pages_empty_text_and_visual_returns_no_anchors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services.document_agent.manifest import ToolContext
    from app.services.document_agent.state import ProfileBlackboard

    blackboard = ProfileBlackboard(page_count=20)
    blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category=["OTHER"],
        category_label="Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )
    blackboard.page_labels = [PageLabel(page=page, kind="normal") for page in range(1, 21)]
    blackboard.page_full_text_cache = {}
    ctx = ToolContext(
        pdf_path=str(tmp_path / "empty.pdf"),
        job_id="job-empty-toc",
        blackboard=blackboard,
        trace=None,
        output_dir=str(tmp_path / "out"),
        settings={},
    )

    def fail_if_rendered(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("empty text and visual candidates must not render pages")

    monkeypatch.setattr(toc_anchor_tool, "run_in_child_process", fail_if_rendered)
    result = toc_anchor_tool.find_toc_anchor_pages(ctx, {})
    assert result.status == "ok"
    assert blackboard.toc_anchor_pages == []
    assert result.payload == {"anchor_count": 0}
    assert result.output_summary is not None
    assert result.output_summary["narrowing"]["raw"] == 0
    assert "head_window" not in result.output_summary["narrowing"]


def test_find_toc_anchor_pages_born_digital_drops_consecutive_duplicates_and_keeps_visual(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services.document_agent.manifest import ToolContext
    from app.services.document_agent.pdf_text import LineDraft, build_page_text_bands
    from app.services.document_agent.state import ProfileBlackboard

    blackboard = ProfileBlackboard(page_count=20)
    blackboard.document_profile = DocumentProfile(
        is_scanned=False,
        category=["OTHER"],
        category_label="Report",
        routing_category=PdfRoutingCategory.GENERIC.value,
    )
    blackboard.page_labels = [PageLabel(page=page, kind="normal") for page in range(1, 21)]
    blackboard.page_full_text_cache = {
        10: build_page_text_bands((LineDraft(text="Contents"),)),
        11: build_page_text_bands((LineDraft(text="Contents"),)),
    }
    ctx = ToolContext(
        pdf_path=str(tmp_path / "native.pdf"),
        job_id="job-native-toc",
        blackboard=blackboard,
        trace=None,
        output_dir=str(tmp_path / "out"),
        settings={"toc_visual_candidate_pages": [4]},
    )

    def fake_render(_worker, _pdf_path, pages, output_dir, _dpi, timeout=120):
        del timeout
        results = []
        for page in pages:
            png = Path(output_dir) / f"toc_anchor_page_{page}.png"
            png.write_bytes(b"png")
            results.append({"page": page, "png_path": str(png)})
        return {"ok": True, "results": results}

    monkeypatch.setattr(toc_anchor_tool, "run_in_child_process", fake_render)
    result = toc_anchor_tool.find_toc_anchor_pages(ctx, {})
    assert result.status == "ok"
    assert [item.page for item in blackboard.toc_anchor_pages] == [4]
    assert [item.source for item in blackboard.toc_anchor_pages] == ["visual"]
    assert result.output_summary is not None
    assert result.output_summary["narrowing"]["after_dedup"] == 0
    assert result.output_summary["narrowing"]["union"] == 1
