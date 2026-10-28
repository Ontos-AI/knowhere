"""Native progressive profiling and cross-worker asset coverage contracts."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from io import BytesIO
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pymupdf
import pytest
from PIL import Image

from app.services.document_agent import coordinator as coordinator_module
from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.manifest import (
    DocumentProfile,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.pdf_text import page_body_content_map, page_content_map
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.profiling.profile_model import ParserDocumentProfile
from app.services.document_parser.support.stage_profiler import (
    cleanup_stage_tracker,
    init_stage_tracker,
)
from app.services.page_memory import memory_service, page_assets, page_tagger
from app.services.page_memory import scope_enrichment_service as enrichment_service
from app.services.page_memory.memory_service import PageMemoryInput
from app.services.page_memory.page_assets import PageAsset
from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.phase_a import (
    PHASE_B_ANATOMY_REL,
    PHASE_B_BLACKBOARD_REL,
    PHASE_B_SOURCE_PDF_REL,
    build_phase_a,
    persist_m0_inputs,
    persist_phase_b_inputs,
)
from shared.core.config import settings
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks


def _write_native_pdf(path: Path, *, include_text: bool = True) -> None:
    image_bytes: BytesIO = BytesIO()
    Image.new("RGB", (32, 32), "navy").save(image_bytes, format="PNG")
    with pymupdf.open() as document:
        for page_number in range(1, 4):
            page = document.new_page(width=600, height=800)
            if include_text:
                page.insert_text((40, 25), f"Header {page_number}")
                page.insert_text(
                    (40, 100), f"Complete native body for page {page_number}."
                )
                page.insert_text((40, 770), f"Footer {page_number}")
            if page_number == 2:
                for x in (50, 150, 250):
                    page.draw_line((x, 200), (x, 300))
                for y in (200, 250, 300):
                    page.draw_line((50, y), (250, y))
                if include_text:
                    for x, y, text in (
                        (60, 225, "A"),
                        (160, 225, "B"),
                        (60, 275, "1"),
                        (160, 275, "2"),
                    ):
                        page.insert_text((x, y), text)
            if page_number == 3:
                page.insert_image(
                    pymupdf.Rect(50, 200, 250, 400), stream=image_bytes.getvalue()
                )
        document.save(path)


def _make_classification(
    *, is_scanned: bool = False, routing_category: str = "generic"
) -> DocumentProfile:
    return DocumentProfile(
        is_scanned=is_scanned,
        category=["04000000"],
        category_label="Native contract report",
        routing_category=routing_category,
        language="en",
        header_y=0.08,
        footer_y=0.90,
    )


def _stub_classification(
    monkeypatch: pytest.MonkeyPatch, profile: DocumentProfile
) -> None:
    def classify(_profiler: object) -> tuple[DocumentProfile, ToolResult]:
        return profile, ToolResult(status="ok", payload={})

    monkeypatch.setattr(coordinator_module.CoarseProfiler, "classify", classify)


def test_native_progressive_baseline_keeps_text_source_and_eager_images_without_asset_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source: Path = tmp_path / "native.pdf"
    _write_native_pdf(source)
    _stub_classification(monkeypatch, _make_classification())
    monkeypatch.setattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)
    probe_calls: list[str] = []
    probe_page_assets = coordinator_module.probe_page_assets

    def record_asset_probe(
        ctx: ToolContext, arguments: dict[str, object]
    ) -> ToolResult:
        probe_calls.append(ctx.pdf_path)
        return probe_page_assets(ctx, arguments)

    monkeypatch.setattr(coordinator_module, "probe_page_assets", record_asset_probe)
    timings: dict[str, int] = init_stage_tracker()
    try:
        output_dir, dataframe = memory_service.run(
            PageMemoryInput(
                file_path=str(source),
                filename="native.pdf",
                output_dir=str(tmp_path / "output"),
                ingestion_mode="progressive",
            )
        )
    finally:
        cleanup_stage_tracker()

    assert probe_calls == [], (
        "native baseline must publish before detailed asset detection"
    )
    assert "document_profile.classification" in timings
    assert "document_profile.text_scan" in timings
    assert "document_profile.asset_probe" not in timings
    assert "page_memory.m0_render" in timings
    chunks = dataframe_to_chunks(dataframe)
    assert [chunk["metadata"]["page_nums"] for chunk in chunks] == [[1], [2], [3]]
    assert [chunk["path"] for chunk in chunks] == [
        "native.pdf/p1",
        "native.pdf/p2",
        "native.pdf/p3",
    ]
    assert (
        Path(output_dir) / PHASE_B_SOURCE_PDF_REL
    ).read_bytes() == source.read_bytes()
    for page_number, chunk in enumerate(chunks, start=1):
        assert f"Header {page_number}" in chunk["content"]
        assert f"Complete native body for page {page_number}." in chunk["content"]
        assert f"Footer {page_number}" in chunk["content"]
        assert chunk["metadata"]["body_text_preview"].startswith("Complete native body")
        assert "Header" not in chunk["metadata"]["body_text_preview"]
        assert chunk["metadata"]["embedding_status"] == "pending"
        assert "_page_embedding" not in chunk["metadata"]
        reference: str = chunk["metadata"]["page_assets"][0]["artifact_ref"]
        with Image.open(Path(output_dir) / reference) as image:
            assert image.width > 0 and image.height > 0
    blackboard = json.loads((Path(output_dir) / PHASE_B_BLACKBOARD_REL).read_text())
    assert blackboard["global_signals"]["assets_probed"] is False
    assert [feature["has_asset"] for feature in blackboard["page_features"]] == [
        None,
        None,
        None,
    ]
    assert [feature["table_count"] for feature in blackboard["page_features"]] == [
        None,
        None,
        None,
    ]
    assert blackboard["document_profile"]["header_y"] == 0.08
    assert blackboard["document_profile"]["footer_y"] == 0.90
    assert len(blackboard["page_full_text_bands"]) == 3
    restored_features, _ = enrichment_service._parse_features_and_labels(
        blackboard["page_features"], blackboard["page_labels"]
    )
    assert all(feature.has_asset is None for feature in restored_features)
    assert all(feature.table_count is None for feature in restored_features)


@pytest.mark.parametrize(
    ("is_scanned", "routing_category", "include_text", "stop_after"),
    [
        (True, "generic", True, "native_baseline"),
        (False, "scanned", True, "native_baseline"),
        (False, "generic", False, "native_baseline"),
        (False, "generic", True, None),
    ],
)
def test_scanned_empty_and_full_profiles_keep_detailed_asset_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_scanned: bool,
    routing_category: str,
    include_text: bool,
    stop_after: str | None,
) -> None:
    source: Path = tmp_path / "document.pdf"
    _write_native_pdf(source, include_text=include_text)
    _stub_classification(
        monkeypatch,
        _make_classification(is_scanned=is_scanned, routing_category=routing_category),
    )
    monkeypatch.setattr(
        ProfileCoordinator,
        "_run_toc_extraction_pipeline",
        lambda coordinator: setattr(
            coordinator.blackboard, "toc_result", TocResult(method="none")
        ),
    )
    profile = profile_document(
        str(source),
        output_dir=str(tmp_path / "profile"),
        oversized_policy="page_memory",
        stop_after=stop_after,
    )
    blackboard = getattr(profile, "blackboard")
    assert blackboard.global_signals["assets_probed"] is True
    assert all(feature.has_asset is not None for feature in blackboard.page_features)
    assert (profile.anatomy is not None) is (stop_after is None)


class _MemoryStorage:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files: dict[str, bytes] = dict(files)

    def upload_raw_file(
        self, *, relative_path: str, local_file_path: str, **_kwargs: object
    ) -> None:
        self.files[relative_path] = Path(local_file_path).read_bytes()

    def download_raw_to_temp(
        self, *, relative_path: str, temp_dir: str, suffix: str, **_kwargs: object
    ) -> str:
        downloaded: Path = (
            Path(temp_dir) / f"download-{Path(relative_path).name}{suffix}"
        )
        downloaded.write_bytes(self.files[relative_path])
        return str(downloaded)


@pytest.mark.parametrize("routing_category", ["generic", "atlas"])
def test_background_probe_precedes_toc_and_claim_anatomy_reaches_fine_asset_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing_category: str,
) -> None:
    initial: Path = tmp_path / "initial"
    initial.mkdir()
    source: Path = initial / PHASE_B_SOURCE_PDF_REL
    _write_native_pdf(source)
    coordinator = ProfileCoordinator(
        pdf_path=str(source),
        job_id="native-contract",
        output_dir=str(initial / "_doc_agent"),
    )
    coordinator._run_bootstrap()
    coordinator.blackboard.document_profile = _make_classification(
        routing_category=routing_category
    )
    coordinator._run_text_scan()
    # Persist the deferred baseline boundary, independent of foreground code.
    coordinator.blackboard.page_features = [
        replace(feature, has_asset=None)
        for feature in coordinator.blackboard.page_features
    ]
    profile = ParserDocumentProfile(
        file_type="pdf",
        page_count=3,
        page_full_text_cache=page_content_map(
            coordinator.blackboard.page_full_text_cache
        ),
        page_body_text_cache=page_body_content_map(
            coordinator.blackboard.page_full_text_cache
        ),
    )
    setattr(profile, "blackboard", coordinator.blackboard)
    initial_refs: list[str] = persist_m0_inputs(
        output_dir=str(initial), profile=profile
    )
    storage = _MemoryStorage(
        {reference: (initial / reference).read_bytes() for reference in initial_refs}
    )
    target = enrichment_service._ClaimedScopeTarget(
        document_id="doc-native",
        job_id="native-contract",
        user_id="owner",
        namespace="default",
        source_file_name="native.pdf",
        raw_prefix="results/native/",
        job_metadata={"page_memory_phase_b": initial_refs},
        page_memory_config=PageMemoryConfig(
            fine_min_pages=10, asset_summary_enabled=False
        ),
    )
    structure: Path = tmp_path / "structure-worker" / "native"
    structure.mkdir(parents=True)
    enrichment_service._download_phase_b_inputs(
        storage=storage,
        job_id=target.job_id,
        raw_prefix=target.raw_prefix,
        job_metadata=target.job_metadata,
        output_dir=str(structure),
    )
    events: list[str] = []
    probe_calls: list[str] = []
    probe_page_assets = coordinator_module.probe_page_assets

    def probe_deferred_assets(
        ctx: ToolContext, arguments: dict[str, object]
    ) -> ToolResult:
        assert all(
            feature.has_asset is None for feature in ctx.blackboard.page_features
        )
        probe_calls.append(ctx.pdf_path)
        return probe_page_assets(ctx, arguments)

    def reject_repeated_text_scan(_coordinator: ProfileCoordinator) -> None:
        raise AssertionError("persisted native bands must survive worker restart")

    def extract_toc(resumed: ProfileCoordinator) -> None:
        assert resumed.blackboard.global_signals["assets_probed"] is True, (
            "deferred asset probe must finish before TOC consumers"
        )
        assert [feature.has_asset for feature in resumed.blackboard.page_features] == [
            False,
            True,
            True,
        ]
        events.append("toc")
        resumed.blackboard.toc_result = TocResult(method="none")

    monkeypatch.setattr(ProfileCoordinator, "_run_toc_extraction_pipeline", extract_toc)
    monkeypatch.setattr(coordinator_module, "probe_page_assets", probe_deferred_assets)
    monkeypatch.setattr(ProfileCoordinator, "_run_text_scan", reject_repeated_text_scan)
    timings: dict[str, int] = init_stage_tracker()
    try:
        resumed = enrichment_service._resume_profile_from_m0_workspace(
            add_dir=str(structure), target=target, job_result_id="revision-native"
        )
    finally:
        cleanup_stage_tracker()
    assert events == ["toc"]
    assert len(probe_calls) == 1
    assert "document_profile.asset_probe" in timings
    assert "document_profile.classification" not in timings
    assert "document_profile.text_scan" not in timings
    assert resumed.anatomy is not None
    assert resumed.anatomy.page_features[1].table_count > 0
    assert resumed.anatomy.page_features[2].image_count == 1
    phase_a = build_phase_a(profile=resumed, filename="native.pdf")
    phase_b_refs: list[str] = persist_phase_b_inputs(
        output_dir=str(structure), profile=resumed, manifest=phase_a.manifest
    )
    mapping: dict[str, str] = {
        reference: f"phase_b/structure-claim/{Path(reference).name}"
        for reference in phase_b_refs
        if reference.startswith("phase_b/") and reference != PHASE_B_BLACKBOARD_REL
    }
    enrichment_service._upload_scope_artifacts(
        storage=storage,
        job_id=target.job_id,
        raw_prefix=target.raw_prefix,
        workspace_dir=str(structure.parent),
        artifact_refs=set(mapping),
        artifact_mapping=mapping,
    )
    anatomy = json.loads(storage.files[mapping[PHASE_B_ANATOMY_REL]])
    assert [feature["has_asset"] for feature in anatomy["page_features"]] == [
        False,
        True,
        True,
    ]
    fine_target = replace(
        target,
        job_metadata={
            "page_memory_phase_b": phase_b_refs,
            "page_memory_phase_b_artifacts": mapping,
        },
    )
    # Remove the initial worker's files; fine must use persisted claim outputs.
    shutil.rmtree(initial)
    shutil.rmtree(structure.parent)
    monkeypatch.delenv("IMAGE_MODEL", raising=False)

    def tag_pages(
        *, pages: list[PageRenderResult], **_kwargs: object
    ) -> list[PageTagResult]:
        return [
            PageTagResult(
                page_index=page.page_index,
                summary="Page summary",
                keywords=[],
                strategy_used="vlm_lite",
            )
            for page in pages
        ]

    selected_pages: list[int] = []

    def extract_assets(
        *, rendered_pages: list[PageRenderResult], output_dir: str, **_kwargs: object
    ) -> dict[int, list[PageAsset]]:
        selected_pages.extend(page.page_index for page in rendered_pages)
        image_dir: Path = Path(output_dir) / "images"
        image_dir.mkdir(exist_ok=True)
        for page in rendered_pages:
            shutil.copyfile(page.image_path, image_dir / f"asset-{page.page_index}.png")
        table_path: Path = Path(output_dir) / "tables" / "asset-2.html"
        table_path.parent.mkdir(exist_ok=True)
        table_path.write_text("<table><tr><td>A</td><td>B</td></tr></table>")
        return {
            page.page_index: [
                PageAsset(
                    asset_id=f"asset-{page.page_index}",
                    page_index=page.page_index,
                    asset_index=1,
                    kind="table" if page.page_index == 2 else "figure",
                    bbox_px=[0, 0, 20, 20],
                    width_px=20,
                    height_px=20,
                    width_pt=10,
                    height_pt=10,
                    image_path=page.image_path,
                    image_uri=f"images/asset-{page.page_index}.png",
                    html_uri="tables/asset-2.html" if page.page_index == 2 else "",
                    html_path=str(table_path) if page.page_index == 2 else "",
                    summary="Retained asset",
                )
            ]
            for page in rendered_pages
        }

    monkeypatch.setattr(page_tagger, "tag_pages", tag_pages)
    monkeypatch.setattr(page_assets, "extract_page_assets_from_renders", extract_assets)
    fine: Path = tmp_path / "fine-worker"
    fine.mkdir()
    output = enrichment_service._run_scope_in_workspace(
        workspace_dir=str(fine),
        target=fine_target,
        scope_id=phase_a.coarse_scopes[0].scope_id,
        storage=storage,
    )
    assert selected_pages == [2, 3]
    assert {chunk["type"] for chunk in output.chunks} >= {"page", "table", "image"}
    assert {
        page for chunk in output.chunks for page in chunk["metadata"]["page_nums"]
    } == {1, 2, 3}


def test_native_profile_boundary_can_defer_assets_before_producer_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source: Path = tmp_path / "native-boundary.pdf"
    _write_native_pdf(source)
    _stub_classification(monkeypatch, _make_classification())
    profile = profile_document(
        str(source),
        output_dir=str(tmp_path / "profile"),
        oversized_policy="page_memory",
        stop_after="native_baseline",
    )
    blackboard = getattr(profile, "blackboard")
    assert profile.page_count == 3
    assert blackboard.global_signals["assets_probed"] is False
    assert all(feature.has_asset is None for feature in blackboard.page_features)
    assert all(feature.table_count is None for feature in blackboard.page_features)
    assert all(
        f"Complete native body for page {page}." in text
        for page, text in profile.page_full_text_cache.items()
    )
