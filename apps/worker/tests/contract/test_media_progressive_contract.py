"""Media progressive baseline upload, read-hit eligibility, and transcript refine."""

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

import pandas as pd
import pytest

from shared.models.schemas.progressive_ingestion import StoredSnapshot

_TRANSCRIPT = {
    "media": {"kind": "audio", "duration_ms": 4000},
    "sentences": [
        {"start_ms": 0, "end_ms": 1000, "text": "welcome", "speaker_id": "0"},
        {"start_ms": 1000, "end_ms": 2000, "text": "price talk", "speaker_id": "1"},
    ],
}


def test_media_progressive_upload_keeps_transcript_as_refine_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion import (
        success_finalization as success_finalization_mod,
    )
    from app.services.document_ingestion.parse_result_package import (
        GeneratedResultPackage,
        ParseArtifact,
        ParseResultPackage,
    )
    from app.services.document_parser.formats.media.extract import SOURCE_AUDIO_REL
    from app.services.document_parser.formats.media.parser import MEDIA_TRANSCRIPT_REL

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / MEDIA_TRANSCRIPT_REL).write_text(json.dumps(_TRANSCRIPT), encoding="utf-8")
    (result_dir / SOURCE_AUDIO_REL).write_bytes(b"RIFF")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")
    uploaded: list[str] = []

    class FakeStorage:
        def upload_raw_file(self, **kwargs: object) -> None:
            uploaded.append(str(kwargs["relative_path"]))
            assert kwargs["raw_prefix"] == "results/job-media/"

        def upload(self, **kwargs: object) -> SimpleNamespace:
            refs = sorted(kwargs["artifact_refs"] or [])
            return SimpleNamespace(
                zip_key="results/job-media/result.zip",
                raw_prefix="results/job-media/snap/",
                raw_files={ref: f"results/job-media/snap/{ref}" for ref in refs},
            )

    persisted: dict[str, object] = {}
    monkeypatch.setattr(
        success_finalization_mod,
        "persist_job_metadata_updates",
        lambda **kw: persisted.update(kw["metadata_updates"]),
    )
    success_finalization_mod._upload_result_package(
        result_package=ParseResultPackage(
            artifact=ParseArtifact(add_dir=str(result_dir), dataframe=pd.DataFrame()),
            chunks=[
                {
                    "chunk_id": "seg-1",
                    "type": "text",
                    "content": "S1: welcome",
                    "path": "talk.mp3",
                    "metadata": {"time_ranges": [{"start_ms": 0, "end_ms": 1000}]},
                }
            ],
        ),
        generated_package=GeneratedResultPackage(
            zip_file_path=str(zip_path),
            checksum_value="abc",
            statistics={},
            zip_size=3,
        ),
        job_id="job-media",
        result_storage_factory=FakeStorage,
        job_context=SimpleNamespace(
            job_metadata={
                "parse_track": "media",
                "ingestion_mode": "progressive",
                "source_file_name": "talk.mp3",
            }
        ),
    )
    assert persisted["enrichment_status"] == "text"
    assert persisted["chunk_progressive_artifact_refs"] == [MEDIA_TRANSCRIPT_REL]
    assert persisted["result_raw_prefix"] == "results/job-media/"
    assert uploaded == [MEDIA_TRANSCRIPT_REL]


@pytest.mark.parametrize(
    ("parse_track", "eligible"),
    [("media", True), ("chunk", True), ("sheet", False), ("page_memory", False)],
)
def test_read_hit_queues_whole_file_final_for_media_baselines(
    parse_track: str,
    eligible: bool,
) -> None:
    from shared.services.jobs.lifecycle.scope_enrichments import (
        _get_pending_chunk_final_state,
    )
    from shared.services.jobs.progressive_ingestion import initialize_baseline_state

    state = initialize_baseline_state(
        snapshot=StoredSnapshot(
            snapshot_id="snapshot_media",
            stage="baseline",
            job_id="job_media",
            job_result_id="rev_media",
            document_id="doc_media",
            checksum="c" * 64,
            archive_key="results/job_media.zip",
        ),
        missing_pages=[],
    )
    pending = _get_pending_chunk_final_state(
        {"enrichment_status": "text", "progressive": state.model_dump(mode="json")},
        {"parse_track": parse_track, "ingestion_mode": "progressive"},
    )
    assert (pending is not None) is eligible


def test_media_refine_rebuilds_chapters_from_transcript_without_source_or_asr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion import chunk_enrichment_service as service
    from app.services.document_ingestion.chunk_enrichment_service import (
        _ChunkFinalTarget,
        _run_full_chunk_parse,
    )
    from app.services.document_parser.formats.media.parser import (
        MEDIA_TRANSCRIPT_REL,
        parse_media_transcript,
    )
    from app.services.document_parser.orchestration.parse_session import (
        _resolve_output_paths,
    )

    output_root = tmp_path / "output"
    _relative_root, add_dir = _resolve_output_paths(
        filename="talk.mp3",
        internal_output_filename="talk.mp3",
        output_dir=str(output_root),
    )
    Path(add_dir).mkdir(parents=True, exist_ok=True)
    (Path(add_dir) / MEDIA_TRANSCRIPT_REL).write_text(
        json.dumps(_TRANSCRIPT), encoding="utf-8"
    )

    def _refuse_download(**_kwargs: object) -> str:
        raise AssertionError("media refine must not download the source file")

    monkeypatch.setattr(service, "_download_original_source", _refuse_download)
    monkeypatch.setitem(
        parse_media_transcript.__globals__,
        "chapter_titles",
        lambda _segments: ["Welcome", "Pricing"],
    )
    target = _ChunkFinalTarget(
        document_id="doc_media",
        job_id="job_media",
        user_id="u1",
        namespace="default",
        source_file_name="talk.mp3",
        s3_key="uploads/talk.mp3",
        raw_prefix="results/job_media/",
        job_metadata={"parse_track": "media", "ingestion_mode": "progressive"},
    )

    parsed_df, resolved_dir = _run_full_chunk_parse(
        target=target,
        input_dir=str(tmp_path / "input"),
        output_root=str(output_root),
        add_dir=add_dir,
    )

    assert resolved_dir == add_dir
    assert list(parsed_df["path"]) == ["talk.mp3/Welcome", "talk.mp3/Pricing"]
    assert list(parsed_df["content"]) == ["S1: welcome", "S2: price talk"]
    assert json.loads(str(parsed_df.iloc[0]["extra_metadata"]))["media"] == {
        "kind": "audio",
        "duration_ms": 4000,
    }
