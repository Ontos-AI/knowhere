"""Phase A I/O: coarse rows, frozen manifest, and result-upload handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_ingestion import (
    success_finalization as success_finalization_mod,
)  # noqa: E402
from app.services.document_ingestion.parse_result_package import (  # noqa: E402
    GeneratedResultPackage,
    ParseArtifact,
    ParseResultPackage,
)
from app.services.page_memory.phase_a import (  # noqa: E402
    PHASE_B_ANATOMY_REL,
    PHASE_B_BLACKBOARD_REL,
    PHASE_B_BODY_TEXT_REL,
    PHASE_B_FULL_TEXT_REL,
    PHASE_B_MANIFEST_REL,
    PHASE_B_SOURCE_PDF_REL,
    build_coarse_text_dataframe,
    build_phase_a,
    next_title_by_path_from_manifest,
    persist_phase_b_inputs,
    require_phase_a_manifest,
    scopes_from_phase_a_manifest,
)
from app.services.page_memory.skeleton_extractor import SectionSkeleton  # noqa: E402
from shared.models.schemas.page_memory_config import PageMemoryConfig  # noqa: E402
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks  # noqa: E402
from shared.services.storage.result_storage import JobResultStorage  # noqa: E402


def _anatomy(*, toc_pages: list[int] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        page_features=[],
        page_labels=[],
        toc_result=SimpleNamespace(toc_pages=toc_pages or []),
        to_dict=lambda: {"page_count": 4, "toc_pages": toc_pages or []},
    )


def _profile(
    *,
    skeletons: list[SectionSkeleton],
    page_texts: dict[int, str],
    body_texts: dict[int, str],
    toc_pages: list[int] | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    from app.services.page_memory import skeleton_extractor

    monkeypatch.setattr(
        skeleton_extractor,
        "extract_section_skeletons",
        lambda **kwargs: skeletons,
    )
    return SimpleNamespace(
        page_count=4,
        anatomy=_anatomy(toc_pages=toc_pages),
        page_full_text_cache=page_texts,
        page_body_text_cache=body_texts,
    )


def test_phase_a_without_anatomy_falls_back_to_root_spanning_all_pages() -> None:
    profile = SimpleNamespace(
        page_count=5,
        anatomy=None,
        page_full_text_cache={page: f"text {page}" for page in range(1, 6)},
        page_body_text_cache={},
    )

    phase_a = build_phase_a(
        profile=profile,
        filename="atlas.pdf",
        page_memory_config=PageMemoryConfig.default(),
    )

    assert [(s.title, s.start_page, s.end_page) for s in phase_a.skeletons] == [
        ("Root", 1, 5)
    ]
    assert [scope.scope_id for scope in phase_a.coarse_scopes] == ["p1-5"]


def test_phase_a_writes_coarse_rows_and_frozen_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skeletons = [
        SectionSkeleton("doc.pdf/A", 1, 1, 2, "A", "doc.pdf"),
        SectionSkeleton("doc.pdf/B", 1, 3, 4, "B", "doc.pdf"),
    ]
    page_texts = {1: "page one", 2: "page two", 3: "page three", 4: "page four"}
    body_texts = {1: "one", 2: "two", 3: "three", 4: "four"}
    profile = _profile(
        skeletons=skeletons,
        page_texts=page_texts,
        body_texts=body_texts,
        monkeypatch=monkeypatch,
    )

    phase_a = build_phase_a(
        profile=profile,
        filename="doc.pdf",
        page_memory_config=PageMemoryConfig.default(),
    )
    (tmp_path / PHASE_B_SOURCE_PDF_REL).write_bytes(b"%PDF")
    written = persist_phase_b_inputs(
        output_dir=str(tmp_path),
        profile=profile,
        manifest=phase_a.manifest,
    )

    assert [scope.scope_id for scope in phase_a.coarse_scopes] == ["p1-2", "p3-4"]
    assert list(phase_a.coarse_df["path"]) == ["doc.pdf/A", "doc.pdf/B"]
    assert list(phase_a.coarse_df["content"]) == [
        "page one\n\npage two",
        "page three\n\npage four",
    ]
    extra = list(phase_a.coarse_df["extra_metadata"])
    assert extra[0]["scope_id"] == "p1-2"
    assert extra[1]["scope_id"] == "p3-4"
    assert extra[0]["enrichment_state"] == "coarse"
    assert extra[0]["body_text_preview"] == "one two"
    assert extra[1]["body_text_preview"] == "three four"

    chunks = dataframe_to_chunks(phase_a.coarse_df)
    assert [chunk["metadata"]["scope_id"] for chunk in chunks] == ["p1-2", "p3-4"]
    assert {chunk["metadata"]["enrichment_state"] for chunk in chunks} == {"coarse"}
    assert chunks[0]["metadata"]["page_nums"] == [1, 2]
    assert chunks[0]["content"] == "page one\n\npage two"

    manifest_path = tmp_path / PHASE_B_MANIFEST_REL
    anatomy_path = tmp_path / PHASE_B_ANATOMY_REL
    full_text_path = tmp_path / PHASE_B_FULL_TEXT_REL
    body_text_path = tmp_path / PHASE_B_BODY_TEXT_REL
    assert manifest_path.is_file()
    assert anatomy_path.is_file()
    assert full_text_path.is_file()
    assert body_text_path.is_file()
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk == phase_a.manifest
    assert [entry["scope_id"] for entry in on_disk["scopes"]] == ["p1-2", "p3-4"]
    assert on_disk["scopes"][0]["next_title_slice"] == {"doc.pdf/A": "B"}
    assert on_disk["scopes"][0]["processing_pages"] == [1, 2]
    assert json.loads(full_text_path.read_text(encoding="utf-8"))["1"] == "page one"
    assert json.loads(body_text_path.read_text(encoding="utf-8"))["3"] == "three"
    assert set(written) == {
        PHASE_B_SOURCE_PDF_REL,
        PHASE_B_MANIFEST_REL,
        PHASE_B_ANATOMY_REL,
        PHASE_B_FULL_TEXT_REL,
        PHASE_B_BODY_TEXT_REL,
    }

    restored = scopes_from_phase_a_manifest(
        require_phase_a_manifest(
            {
                "scope_manifest": on_disk,
            }
        )
    )
    assert [scope["scope_id"] for scope in restored] == ["p1-2", "p3-4"]
    assert restored[0]["skeletons"][0].title == "A"
    assert next_title_by_path_from_manifest(on_disk)["doc.pdf/A"] == "B"


def test_coarse_rows_omit_excluded_toc_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    skeletons = [SectionSkeleton("doc.pdf/A", 1, 1, 4, "A", "doc.pdf")]
    page_texts = {1: "page one", 2: "toc text", 3: "page three", 4: "page four"}
    profile = _profile(
        skeletons=skeletons,
        page_texts=page_texts,
        body_texts=page_texts,
        toc_pages=[2],
        monkeypatch=monkeypatch,
    )
    phase_a = build_phase_a(
        profile=profile,
        filename="doc.pdf",
        page_memory_config=PageMemoryConfig.default(),
    )
    chunks = dataframe_to_chunks(phase_a.coarse_df)
    assert len(chunks) == 1
    assert chunks[0]["metadata"]["page_nums"] == [1, 3, 4]
    assert chunks[0]["content"] == "page one\n\npage three\n\npage four"
    assert phase_a.manifest["scopes"][0]["processing_pages"] == [1, 3, 4]
    assert phase_a.manifest["scopes"][0]["excluded_toc_pages"] == [2]


def test_progressive_run_returns_m0_text_baseline_without_toc_or_scope_vlm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.page_memory import memory_service, page_tagger
    from app.services.page_memory.memory_service import PageMemoryInput

    skeletons = [
        SectionSkeleton("doc.pdf/A", 1, 1, 2, "A", "doc.pdf"),
        SectionSkeleton("doc.pdf/B", 1, 3, 4, "B", "doc.pdf"),
    ]
    page_texts = {1: "one", 2: "two", 3: "three", 4: "four"}
    profile = _profile(
        skeletons=skeletons,
        page_texts=page_texts,
        body_texts=page_texts,
        monkeypatch=monkeypatch,
    )
    profile.page_count = 4
    profile.blackboard = SimpleNamespace(
        page_count=4,
        document_profile=None,
        page_features=[],
        page_labels=[],
        doc_stats={},
        global_signals={"assets_probed": True},
        page_full_text_cache=page_texts,
    )

    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF")
    captured_stop_after: list[object] = []

    def fail_if_tagged(**kwargs: object) -> list[object]:
        raise AssertionError(f"M0 must not tag pages: {kwargs.keys()}")

    def fake_profile(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        captured_stop_after.append(kwargs.get("stop_after"))
        return profile

    monkeypatch.setattr(
        memory_service,
        "normalize_to_pdf",
        lambda **kwargs: (str(source), "doc.pdf"),
    )
    monkeypatch.setattr(memory_service, "profile_document", fake_profile)
    monkeypatch.setattr(page_tagger, "tag_pages", fail_if_tagged)
    monkeypatch.setattr(page_tagger, "tag_page_titles", fail_if_tagged)

    output_dir, dataframe = memory_service.run(
        PageMemoryInput(
            file_path=str(source),
            filename="doc.pdf",
            output_dir=str(tmp_path / "out"),
            page_memory_config=PageMemoryConfig(asset_extraction_enabled=False),
            ingestion_mode="progressive",
        )
    )
    assert captured_stop_after == ["asset_probe"]
    chunks = dataframe_to_chunks(dataframe)
    assert [chunk["metadata"]["enrichment_state"] for chunk in chunks] == [
        "text",
        "text",
        "text",
        "text",
    ]
    assert [chunk["metadata"]["scope_id"] for chunk in chunks] == [
        "p1-1",
        "p2-2",
        "p3-3",
        "p4-4",
    ]
    assert [chunk["path"] for chunk in chunks] == [
        "doc.pdf/p1",
        "doc.pdf/p2",
        "doc.pdf/p3",
        "doc.pdf/p4",
    ]
    assert (Path(output_dir) / PHASE_B_BLACKBOARD_REL).is_file()
    assert (Path(output_dir) / PHASE_B_FULL_TEXT_REL).is_file()
    assert (Path(output_dir) / PHASE_B_SOURCE_PDF_REL).is_file()
    assert not (Path(output_dir) / PHASE_B_MANIFEST_REL).exists()


def test_full_mode_run_invokes_full_page_dataframe_and_writes_no_phase_b_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.page_memory import memory_service
    from app.services.page_memory.memory_service import PageMemoryInput

    skeletons = [SectionSkeleton("doc.pdf/A", 1, 1, 8, "A", "doc.pdf")]
    page_texts = {i: f"p{i}" for i in range(1, 9)}
    profile = _profile(
        skeletons=skeletons,
        page_texts=page_texts,
        body_texts=page_texts,
        monkeypatch=monkeypatch,
    )
    profile.page_count = 8
    profile.toc = SimpleNamespace(has_toc=True)

    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF")
    called_full: list[bool] = []
    captured_stop_after: list[object] = []

    def fake_profile(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        captured_stop_after.append(kwargs.get("stop_after"))
        return profile

    def fake_build_page_df(**kwargs: object) -> pd.DataFrame:
        del kwargs
        called_full.append(True)
        return build_coarse_text_dataframe(
            page_texts={1: "full row"},
            body_text_by_page={1: "full row"},
            page_count=1,
            filename="doc.pdf",
        )

    monkeypatch.setattr(
        memory_service,
        "normalize_to_pdf",
        lambda **kwargs: (str(source), "doc.pdf"),
    )
    monkeypatch.setattr(memory_service, "profile_document", fake_profile)
    monkeypatch.setattr(
        memory_service, "_build_page_dataframe", fake_build_page_df
    )

    output_dir, _df = memory_service.run(
        PageMemoryInput(
            file_path=str(source),
            filename="doc.pdf",
            output_dir=str(tmp_path / "out_full"),
            page_memory_config=PageMemoryConfig(asset_extraction_enabled=False),
            ingestion_mode="full",
        )
    )
    assert captured_stop_after == [None]
    assert called_full == [True]
    assert not (Path(output_dir) / PHASE_B_MANIFEST_REL).exists()
    assert not (Path(output_dir) / PHASE_B_BLACKBOARD_REL).exists()


def test_page_memory_upload_m0_marks_text_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_dir = tmp_path / "result_m0"
    result_dir.mkdir()
    (result_dir / "phase_b").mkdir()
    (result_dir / PHASE_B_SOURCE_PDF_REL).write_bytes(b"%PDF")
    (result_dir / PHASE_B_BLACKBOARD_REL).write_text("{}", encoding="utf-8")
    (result_dir / PHASE_B_FULL_TEXT_REL).write_text("{}", encoding="utf-8")
    (result_dir / PHASE_B_BODY_TEXT_REL).write_text("{}", encoding="utf-8")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")

    class FakeStorage:
        def upload(self, **kwargs: object) -> SimpleNamespace:
            refs = sorted(kwargs["artifact_refs"] or [])
            return SimpleNamespace(
                zip_key="results/job-m0/result.zip",
                raw_prefix="results/job-m0/",
                raw_files={ref: f"results/job-m0/{ref}" for ref in refs},
            )

    persisted: dict[str, object] = {}
    monkeypatch.setattr(
        success_finalization_mod,
        "persist_job_metadata_updates",
        lambda **kw: persisted.update(kw["metadata_updates"]),  # type: ignore[arg-type]
    )
    success_finalization_mod._upload_result_package(  # noqa: SLF001
        result_package=ParseResultPackage(
            artifact=ParseArtifact(add_dir=str(result_dir), dataframe=pd.DataFrame()),
            chunks=[],
        ),
        generated_package=GeneratedResultPackage(
            zip_file_path=str(zip_path),
            checksum_value="abc",
            statistics={},
            zip_size=3,
        ),
        job_id="job-m0",
        result_storage_factory=FakeStorage,
        job_context=SimpleNamespace(
            job_metadata={
                "parse_track": "page_memory",
                "ingestion_mode": "progressive",
            }
        ),  # type: ignore[arg-type]
    )
    assert persisted["enrichment_status"] == "text"
    assert persisted["page_memory_phase_b"] == [
        PHASE_B_SOURCE_PDF_REL,
        PHASE_B_BLACKBOARD_REL,
        PHASE_B_FULL_TEXT_REL,
        PHASE_B_BODY_TEXT_REL,
    ]


def test_page_memory_upload_without_manifest_does_not_mark_coarse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / PHASE_B_SOURCE_PDF_REL).write_bytes(b"%PDF")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")

    class FakeStorage:
        def upload(self, **kwargs: object) -> SimpleNamespace:
            del kwargs
            return SimpleNamespace(
                zip_key="results/job-1/result.zip",
                raw_prefix="results/job-1/",
                raw_files={"source.pdf": "results/job-1/source.pdf"},
            )

    persisted: dict[str, object] = {}

    def capture_metadata(**kwargs: object) -> None:
        persisted.update(kwargs["metadata_updates"])  # type: ignore[arg-type]

    monkeypatch.setattr(
        success_finalization_mod,
        "persist_job_metadata_updates",
        capture_metadata,
    )
    success_finalization_mod._upload_result_package(  # noqa: SLF001
        result_package=ParseResultPackage(
            artifact=ParseArtifact(add_dir=str(result_dir), dataframe=pd.DataFrame()),
            chunks=[],
        ),
        generated_package=GeneratedResultPackage(
            zip_file_path=str(zip_path),
            checksum_value="abc",
            statistics={},
            zip_size=3,
        ),
        job_id="job-1",
        result_storage_factory=FakeStorage,
        job_context=SimpleNamespace(job_metadata={"parse_track": "page_memory"}),  # type: ignore[arg-type]
    )
    assert persisted["result_raw_prefix"] == "results/job-1/"
    assert "enrichment_status" not in persisted
    assert "page_memory_phase_b" not in persisted


def test_result_storage_uploads_phase_b_internal_files(tmp_path: Path) -> None:
    class FakeStorageAdapter:
        def __init__(self) -> None:
            self.uploaded_keys: list[str] = []

        def upload_file(
            self, local_path: str, key: str, bucket: str | None = None
        ) -> dict[str, str]:
            del local_path, bucket
            self.uploaded_keys.append(key)
            return {"key": key}

        def generate_presigned_url(self, *args, **kwargs) -> str:
            del args, kwargs
            return "https://assets.example.test/file"

    result_dir = tmp_path / "result"
    (result_dir / "phase_b").mkdir(parents=True)
    (result_dir / "pages").mkdir()
    (result_dir / PHASE_B_SOURCE_PDF_REL).write_bytes(b"source")
    (result_dir / PHASE_B_MANIFEST_REL).write_text("{}", encoding="utf-8")
    (result_dir / "pages" / "page-1.png").write_bytes(b"skip")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")

    adapter = FakeStorageAdapter()
    storage = JobResultStorage(
        results_bucket="test-results",
        storage_adapter=adapter,  # type: ignore[arg-type]
    )
    bundle = storage.upload(
        job_id="job-1",
        result_dir=str(result_dir),
        zip_file_path=str(zip_path),
        artifact_refs={
            PHASE_B_SOURCE_PDF_REL,
            PHASE_B_MANIFEST_REL,
            "pages/page-1.png",
        },
    )

    assert set(bundle.raw_files) == {PHASE_B_SOURCE_PDF_REL, PHASE_B_MANIFEST_REL}
    assert "results/job-1/phase_b/manifest.json" in adapter.uploaded_keys
    assert "results/job-1/pages/page-1.png" not in adapter.uploaded_keys
