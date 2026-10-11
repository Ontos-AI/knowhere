from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock
import zipfile

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-contract-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.contracts.parse_result import validate_parse_result_archive
from shared.core.exceptions.domain_exceptions import ParseResultContractException
from shared.services.storage.zip_result_schema import ZipResultSchemaBuilder
from shared.services.storage.zip_result_service import ZipResultService


def test_actual_writer_produces_valid_text_table_image_and_page_archive(tmp_path):
    for directory in ("images", "tables", "page_citation_assets"):
        (tmp_path / directory).mkdir()
    (tmp_path / "images" / "picture.png").write_bytes(b"synthetic image bytes")
    (tmp_path / "tables" / "table.html").write_text(
        "<table><tr><td>cell</td></tr></table>"
    )
    (tmp_path / "page_citation_assets" / "page-3.webp").write_bytes(
        b"synthetic page bytes"
    )
    fixtures = Path(__file__).parent / "fixtures" / "parse_result"
    chunks = json.loads((fixtures / "chunks.json").read_text())["chunks"]
    nav = json.loads((fixtures / "doc_nav.json").read_text())
    nav.pop("schema_version")
    (tmp_path / "doc_nav.json").write_text(json.dumps(nav))
    path, checksum, statistics, size = ZipResultService().generate_zip_package(
        job_id="job-test",
        chunks=chunks,
        add_dir=str(tmp_path),
        source_file_name="sample.pdf",
        data_id=None,
        job_metadata={},
        temp_dir=str(tmp_path),
    )
    validated = validate_parse_result_archive(path, allow_legacy=False)
    assert validated.doc_nav.model_extra["top_summary"] == nav["top_summary"]
    assert statistics["page_chunks"] == 1
    assert checksum["algorithm"] == "sha256"
    assert size == os.path.getsize(path)
    with zipfile.ZipFile(path) as archive:
        assert all(
            json.loads(archive.read(name))["schema_version"] == 1
            for name in ("chunks.json", "manifest.json", "doc_nav.json")
        )
    assert "schema_version" not in nav


def test_empty_document_still_has_complete_navigation(tmp_path):
    path, _, stats, _ = ZipResultService().generate_zip_package(
        job_id="empty",
        chunks=[],
        add_dir=str(tmp_path),
        source_file_name="empty.txt",
        data_id=None,
        job_metadata={},
        temp_dir=str(tmp_path),
    )
    result = validate_parse_result_archive(path, allow_legacy=False)
    assert result.chunks.chunks == []
    assert result.doc_nav.sections == []
    assert stats["total_chunks"] == 0


def test_invalid_formatted_shape_prevents_writer_call(tmp_path):
    schema = ZipResultSchemaBuilder()
    schema.format_chunks = Mock(return_value=[{"chunk_id": "bad", "type": "unknown"}])
    writer = Mock()
    with pytest.raises(ParseResultContractException):
        ZipResultService(
            schema_builder=schema, package_writer=writer
        ).generate_zip_package(
            job_id="bad",
            chunks=[],
            add_dir=str(tmp_path),
            source_file_name="sample.txt",
            data_id=None,
            job_metadata={},
            temp_dir=str(tmp_path),
        )
    writer.write.assert_not_called()


def test_navigation_failure_prevents_writer_call(tmp_path):
    schema = ZipResultSchemaBuilder()
    schema.build_doc_nav = Mock(side_effect=ValueError("navigation failure"))
    writer = Mock()
    with pytest.raises(ParseResultContractException) as error:
        ZipResultService(
            schema_builder=schema, package_writer=writer
        ).generate_zip_package(
            job_id="bad",
            chunks=[],
            add_dir=str(tmp_path),
            source_file_name="sample.txt",
            data_id=None,
            job_metadata={},
            temp_dir=str(tmp_path),
        )
    assert error.value.details["violations"][0]["artifact"] == "doc_nav.json"
    writer.write.assert_not_called()


def test_missing_referenced_asset_discards_archive(tmp_path):
    chunks = [
        {
            "chunk_id": "table-missing",
            "type": "table",
            "content": "tables/missing.html",
            "path": "tables/missing.html",
            "metadata": {},
        }
    ]
    with pytest.raises(ParseResultContractException):
        ZipResultService().generate_zip_package(
            job_id="missing",
            chunks=chunks,
            add_dir=str(tmp_path),
            source_file_name="sample.txt",
            data_id=None,
            job_metadata={},
            temp_dir=str(tmp_path),
        )
    assert not (tmp_path / "result_missing.zip").exists()


def test_repeated_image_reference_writes_one_asset_member(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "logo.png").write_bytes(b"synthetic logo bytes")
    chunks = [
        {
            "chunk_id": chunk_id,
            "type": "image",
            "content": "logo",
            "path": "images/logo.png",
            "metadata": {"file_path": "images/logo.png"},
        }
        for chunk_id in ("logo-1", "logo-2")
    ]
    path, _, stats, _ = ZipResultService().generate_zip_package(
        job_id="repeated",
        chunks=chunks,
        add_dir=str(tmp_path),
        source_file_name="sample.docx",
        data_id=None,
        job_metadata={},
        temp_dir=str(tmp_path),
    )
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist().count("images/logo.png") == 1
    assert stats["image_chunks"] == 2
    validate_parse_result_archive(path, allow_legacy=False)


@pytest.mark.parametrize("sections", ["existing", None, "missing"])
def test_explicit_unsupported_navigation_version_is_not_relabelled(tmp_path, sections):
    fixtures = Path(__file__).parent / "fixtures" / "parse_result"
    nav = json.loads((fixtures / "doc_nav.json").read_text())
    nav["schema_version"] = 2
    if sections is None:
        nav["sections"] = None
    elif sections == "missing":
        nav.pop("sections")
    (tmp_path / "doc_nav.json").write_text(json.dumps(nav))
    writer = Mock()
    with pytest.raises(ParseResultContractException) as error:
        ZipResultService(package_writer=writer).generate_zip_package(
            job_id="unsupported",
            chunks=[],
            add_dir=str(tmp_path),
            source_file_name="sample.pdf",
            data_id=None,
            job_metadata={},
            temp_dir=str(tmp_path),
        )
    assert (
        error.value.details["violations"][0]["reason"] == "unsupported_schema_version"
    )
    writer.write.assert_not_called()
