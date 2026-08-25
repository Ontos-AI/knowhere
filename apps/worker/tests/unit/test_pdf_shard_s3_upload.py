"""Split shards must be uploaded to S3 before MinerU URL mode starts."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import (
    PageAnatomyMap,
    Shard,
    ShardPlan,
    TocResult,
)
from app.services.document_parser.formats.pdf import parser as pdf_parser
from shared.core.exceptions.domain_exceptions import StorageServiceException


def test_upload_temp_shard_pdfs_writes_each_local_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Mock()
    storage.verify_upload_exists.return_value = {"exists": True}
    monkeypatch.setattr(pdf_parser, "JobFileStorage", Mock(return_value=storage))

    pdf_parser._upload_temp_shard_pdfs(
        ["/tmp/shard_0.pdf", "/tmp/shard_1.pdf"],
        [
            "tmp/mineru-shards/job_abc/shard_0.pdf",
            "tmp/mineru-shards/job_abc/shard_1.pdf",
        ],
    )

    assert storage.upload_source_file.call_args_list == [
        call("/tmp/shard_0.pdf", "tmp/mineru-shards/job_abc/shard_0.pdf"),
        call("/tmp/shard_1.pdf", "tmp/mineru-shards/job_abc/shard_1.pdf"),
    ]
    assert storage.verify_upload_exists.call_args_list == [
        call("tmp/mineru-shards/job_abc/shard_0.pdf"),
        call("tmp/mineru-shards/job_abc/shard_1.pdf"),
    ]


def _toc_excluded_profile(tmp_path: Path, job_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        anatomy=PageAnatomyMap(
            job_id=job_id,
            file_path=str(tmp_path / "doc.pdf"),
            page_count=2,
            page_features=[],
            page_labels=[],
            toc_result=TocResult(toc_pages=[1], method="vlm_batch"),
            shard_plan=ShardPlan(
                enabled=True,
                reason="too_large",
                shards=[
                    Shard(
                        shard_index=0,
                        page_start=1,
                        page_end=2,
                        page_offset=0,
                        anchor_type="toc_leaf_boundary",
                        anchor_evidence="Intro",
                    )
                ],
            ),
        )
    )


def test_shard_pipeline_uploads_before_mineru(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def fake_split_pdf(pdf_path, shards, work_dir, exclude_pages=None):
        events.append("split")
        shard_path = Path(work_dir) / "shard_0.pdf"
        shard_path.write_bytes(b"%PDF-1.4")
        return [str(shard_path)], None

    class _FakeStorage:
        def upload_source_file(
            self, local_file_path: str, storage_key: str
        ) -> dict[str, str]:
            events.append(f"upload:{storage_key}")
            assert Path(local_file_path).exists()
            return {"etag": "ok"}

        def verify_upload_exists(self, storage_key: str) -> dict[str, bool]:
            events.append(f"verify:{storage_key}")
            return {"exists": True}

        def delete_upload_file(self, storage_key: str) -> bool:
            events.append(f"delete:{storage_key}")
            return True

    def fake_parse_via_full(shard_pdf, shard_filename, shard_out, s3_key=None):
        events.append(f"parse:{s3_key}")
        Path(shard_out).mkdir(parents=True, exist_ok=True)
        (Path(shard_out) / "full.md").write_text("# Intro\nBody\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.services.document_parser.formats.pdf.shard_splitter.split_pdf",
        fake_split_pdf,
    )
    monkeypatch.setattr(pdf_parser, "JobFileStorage", _FakeStorage)
    monkeypatch.setattr(pdf_parser, "parse_via_full", fake_parse_via_full)
    monkeypatch.setattr(
        "app.services.document_parser.formats.markdown.parser.eval_md_headings",
        lambda md_lines, *args, **kwargs: list(md_lines),
    )
    monkeypatch.setattr(
        pdf_parser,
        "parse_md",
        lambda *_args, **kwargs: {"ok": True, "lines": kwargs["lines_with_heading"]},
    )
    monkeypatch.setattr(
        "app.services.document_parser.formats.pdf.shard_merger.merge_images",
        lambda *_args, **_kwargs: None,
    )

    pdf_parser._parse_pdf_via_shards(
        str(tmp_path / "doc.pdf"),
        "doc.pdf",
        str(tmp_path / "out"),
        {"smart_title_parse": False, "model_name": "test-model"},
        profile=_toc_excluded_profile(tmp_path, "job-upload-first"),
        s3_key="uploads/job-upload-first.pdf",
        job_id="job-upload-first",
    )

    assert events[:4] == [
        "split",
        "upload:tmp/mineru-shards/job-upload-first/shard_0.pdf",
        "verify:tmp/mineru-shards/job-upload-first/shard_0.pdf",
        "parse:tmp/mineru-shards/job-upload-first/shard_0.pdf",
    ]


def test_failed_shard_upload_does_not_start_mineru(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parse_via_full = Mock()

    def fake_split_pdf(pdf_path, shards, work_dir, exclude_pages=None):
        shard_path = Path(work_dir) / "shard_0.pdf"
        shard_path.write_bytes(b"%PDF-1.4")
        return [str(shard_path)], None

    class _FailingStorage:
        def upload_source_file(
            self, local_file_path: str, storage_key: str
        ) -> dict[str, str]:
            raise StorageServiceException(
                internal_message="S3 upload failed",
                operation="upload_local_file",
            )

        def delete_upload_file(self, storage_key: str) -> bool:
            return False

    monkeypatch.setattr(
        "app.services.document_parser.formats.pdf.shard_splitter.split_pdf",
        fake_split_pdf,
    )
    monkeypatch.setattr(pdf_parser, "JobFileStorage", _FailingStorage)
    monkeypatch.setattr(pdf_parser, "parse_via_full", parse_via_full)

    with pytest.raises(StorageServiceException):
        pdf_parser._parse_pdf_via_shards(
            str(tmp_path / "doc.pdf"),
            "doc.pdf",
            str(tmp_path / "out"),
            {"smart_title_parse": False, "model_name": "test-model"},
            profile=_toc_excluded_profile(tmp_path, "job-upload-fail"),
            s3_key="uploads/job-upload-fail.pdf",
            job_id="job-upload-fail",
        )

    parse_via_full.assert_not_called()


def test_unverified_shard_upload_does_not_start_mineru(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parse_via_full = Mock()

    def fake_split_pdf(pdf_path, shards, work_dir, exclude_pages=None):
        shard_path = Path(work_dir) / "shard_0.pdf"
        shard_path.write_bytes(b"%PDF-1.4")
        return [str(shard_path)], None

    class _InvisibleStorage:
        def upload_source_file(
            self, local_file_path: str, storage_key: str
        ) -> dict[str, str]:
            return {"etag": "ok"}

        def verify_upload_exists(self, storage_key: str) -> dict[str, bool]:
            return {"exists": False}

        def delete_upload_file(self, storage_key: str) -> bool:
            return True

    monkeypatch.setattr(
        "app.services.document_parser.formats.pdf.shard_splitter.split_pdf",
        fake_split_pdf,
    )
    monkeypatch.setattr(pdf_parser, "JobFileStorage", _InvisibleStorage)
    monkeypatch.setattr(pdf_parser, "parse_via_full", parse_via_full)

    with pytest.raises(StorageServiceException, match="not visible"):
        pdf_parser._parse_pdf_via_shards(
            str(tmp_path / "doc.pdf"),
            "doc.pdf",
            str(tmp_path / "out"),
            {"smart_title_parse": False, "model_name": "test-model"},
            profile=_toc_excluded_profile(tmp_path, "job-upload-invisible"),
            s3_key="uploads/job-upload-invisible.pdf",
            job_id="job-upload-invisible",
        )

    parse_via_full.assert_not_called()
