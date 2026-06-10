from __future__ import annotations

import importlib
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
from app.services.document_agent.validators import validate_shard_plan
from app.services.document_parser.formats.pdf import parser as pdf_parser
from app.services.document_parser.formats.pdf import shard_splitter
from app.services.document_parser.profiling import doc_profiler
from app.services.document_parser.profiling.doc_profiler import profile_document
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
        is_blank_like=False,
        text_lines_preview=["Section 1"],
    )


def test_run_toc_degrades_to_empty_result_on_standard_failure(tmp_path: Path) -> None:
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

    toc_result = coordinator.run_toc()

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
    coordinator.blackboard.page_count = 2
    coordinator.blackboard.page_features = [_page_feature(1), _page_feature(2)]
    coordinator.blackboard.page_labels = [
        PageLabel(page=1, kind="normal", confidence=1.0),
        PageLabel(page=2, kind="normal", confidence=1.0),
    ]
    coordinator.blackboard.doc_stats = {"page_count": 2}
    coordinator.blackboard.global_signals["page_kind_counts"] = {"normal": 2}
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
    coordinator.blackboard.page_count = 3
    coordinator.blackboard.page_features = [_page_feature(1), _page_feature(2)]
    coordinator.blackboard.page_labels = [
        PageLabel(page=1, kind="normal", confidence=1.0),
        PageLabel(page=2, kind="normal", confidence=1.0),
    ]
    coordinator.blackboard.doc_stats = {"page_count": 3}
    coordinator.blackboard.global_signals["page_kind_counts"] = {"normal": 3}
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

    def fake_h1_boundary() -> None:
        calls.append("h1")
        coordinator.blackboard.h1_result = H1BoundaryResult(method="toc_grep")

    def fake_persist(_anatomy):
        calls.append("persist")

    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc_extraction)
    monkeypatch.setattr(coordinator, "_run_h1_boundary_pipeline", fake_h1_boundary)
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

    assert calls[:2] == ["toc", "h1"]
    assert anatomy.toc_result.toc_pages == [17]


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
    coordinator.blackboard.page_count = 3
    coordinator.blackboard.page_features = [_page_feature(1), _page_feature(2)]
    coordinator.blackboard.page_labels = [
        PageLabel(page=1, kind="normal", confidence=1.0),
        PageLabel(page=2, kind="normal", confidence=1.0),
    ]
    coordinator.blackboard.doc_stats = {"page_count": 3}
    coordinator.blackboard.global_signals["page_kind_counts"] = {"normal": 3}
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

    def fake_h1_boundary() -> None:
        calls.append("h1")
        coordinator.blackboard.h1_result = H1BoundaryResult(method="none")

    def fake_persist(_anatomy):
        calls.append("persist")

    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc_extraction)
    monkeypatch.setattr(coordinator, "_run_h1_boundary_pipeline", fake_h1_boundary)
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

    assert calls == ["h1", "persist"]
    assert anatomy.toc_result.failure_kind == "rejected_all"
    assert anatomy.toc_result.toc_pages == []


def test_run_coarse_runs_toc_before_planner_for_oversized_and_reuses_planner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator = ProfileCoordinator(
        pdf_path=str(tmp_path / "oversized.pdf"),
        job_id="job-toc-before-coarse",
        output_dir=str(tmp_path / "profile"),
        settings={"toc_before_coarse_page_limit": 2},
    )
    (tmp_path / "profile").mkdir()
    coordinator.blackboard.page_count = 3
    coordinator.blackboard.page_features = [_page_feature(1), _page_feature(2)]
    coordinator.blackboard.page_labels = [
        PageLabel(page=1, kind="normal", confidence=1.0),
        PageLabel(page=2, kind="normal", confidence=1.0),
    ]
    coordinator.blackboard.doc_stats = {"page_count": 3}
    coordinator.blackboard.global_signals["page_kind_counts"] = {"normal": 3}

    calls: list[str] = []

    def fake_toc_extraction() -> None:
        calls.append("toc")
        coordinator.blackboard.toc_result = TocResult(toc_pages=[17], method="vlm_batch")
        coordinator.blackboard.toc_hierarchies = [
            {"toc_range": [17, 17], "toc_range_unit": "page", "toc_tree": {}}
        ]

    def fake_h1_boundary() -> None:
        calls.append("h1")
        coordinator.blackboard.h1_result = H1BoundaryResult(method="toc_grep")

    def fake_persist(_anatomy):
        calls.append("persist")

    monkeypatch.setattr(coordinator, "_run_toc_extraction_pipeline", fake_toc_extraction)
    monkeypatch.setattr(coordinator, "_run_h1_boundary_pipeline", fake_h1_boundary)
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

    assert calls == ["toc", "planner", "h1", "persist"]
    assert anatomy.toc_result.toc_pages == [17]


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


def test_standard_pdf_profile_toc_flag_off_preserves_current_behavior(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_instances: list[object] = []

    class FakeCoordinator:
        def __init__(self, **_kwargs) -> None:
            self.calls: list[str] = []
            self.blackboard = SimpleNamespace(
                page_count=2,
                doc_stats={"page_count": 2},
                global_signals={},
                toc_result=None,
                toc_hierarchies=None,
            )
            fake_instances.append(self)

        def run_coarse(self) -> DocumentProfile:
            self.calls.append("run_coarse")
            return DocumentProfile(
                is_scanned=False,
                category="Research Report",
                routing_category=PdfRoutingCategory.GENERIC.value,
            )

        def run_toc(self) -> TocResult:
            self.calls.append("run_toc")
            raise AssertionError("run_toc should be flag-gated for standard PDFs")

        def run_lightweight_anatomy(self):
            self.calls.append("run_lightweight_anatomy")
            raise AssertionError("lightweight anatomy should be flag-gated")

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", FakeCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "PDF_PROFILE_TOC_ENABLED", False)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-flag-off",
        output_dir=str(tmp_path),
    )

    assert profile.toc.has_toc is False
    assert profile.anatomy is None
    assert fake_instances[0].calls == ["run_coarse"]


def test_standard_pdf_profile_toc_flag_on_builds_toc_and_lightweight_anatomy(
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

        def run_toc(self) -> TocResult:
            self.calls.append("run_toc")
            raise AssertionError("run_toc should be no-op after TOC-before-coarse")

        def run_lightweight_anatomy(self):
            self.calls.append("run_lightweight_anatomy")
            return fake_anatomy

    fake_instances: list[FakeCoordinator] = []

    class CapturingCoordinator(FakeCoordinator):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            fake_instances.append(self)

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", CapturingCoordinator)
    monkeypatch.setattr(doc_profiler.settings, "PDF_PROFILE_TOC_ENABLED", True)
    monkeypatch.setattr(doc_profiler.settings, "MAX_PDF_PAGE_LIMIT", 200)

    profile = profile_document(
        str(tmp_path / "standard.pdf"),
        "standard.pdf",
        job_id="job-flag-on",
        output_dir=str(tmp_path),
    )

    assert fake_instances[0].calls == [
        "run_coarse",
        "run_lightweight_anatomy",
    ]
    assert profile.toc.has_toc is True
    assert profile.toc.method == "vlm_batch"
    assert profile.toc.evidence[0].confidence == 0.95
    assert profile.anatomy is fake_anatomy


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


def test_pdf_first_shard_reuses_markdown_toc_detector_when_profile_misses_toc(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    detector_calls: list[list[str]] = []
    heading_contexts: list[object] = []

    def fake_parse_via_full(_pdf_path, _filename, out_dir, s3_key=None):
        Path(out_dir, "full.md").write_text(
            "Contents\n1 Introduction .... 2\n1 Introduction\nBody\n",
            encoding="utf-8",
        )

    def fake_detect_tocs_in_texts(md_lines, **_kwargs):
        detector_calls.append(list(md_lines))
        return (
            [
                {
                    "toc_range": [0, 1],
                    "toc_range_unit": "line",
                    "toc_tree": {"Introduction": {}},
                }
            ],
            ["1 Introduction", "Body"],
        )

    def fake_eval_md_headings(md_lines, *_args, **kwargs):
        heading_contexts.append(kwargs.get("toc_hierarchies"))
        return [f"# {line}" if line.startswith("1 ") else line for line in md_lines]

    active_markdown_parser = importlib.import_module(
        "app.services.document_parser.formats.markdown.parser"
    )
    monkeypatch.setattr(pdf_parser, "parse_via_full", fake_parse_via_full)
    monkeypatch.setattr(pdf_parser, "detect_tocs_in_texts", fake_detect_tocs_in_texts)
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

    assert len(detector_calls) == 1
    assert heading_contexts[0][0]["toc_range_unit"] == "line"
    assert result["lines"] == ["# 1 Introduction", "Body"]


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
