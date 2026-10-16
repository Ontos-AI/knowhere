from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import zipfile

import pytest

from shared.contracts.parse_result import validate_parse_result_archive
from shared.core.exceptions.domain_exceptions import ParseResultContractException
from shared.services.storage.zip_package_writer import (
    ZipPackageWriter,
    ZipPackageWriteRequest,
)


@pytest.fixture
def package_request(tmp_path):
    fixtures = Path(__file__).parent / "fixtures" / "parse_result"
    manifest = json.loads((fixtures / "manifest.json").read_text())
    nav = json.loads((fixtures / "doc_nav.json").read_text())
    chunks = json.loads((fixtures / "chunks.json").read_text())["chunks"]
    manifest.pop("schema_version")
    nav.pop("schema_version")
    resources = []
    for name, content in [
        ("images/picture.png", b"picture"),
        ("tables/table.html", b"<table></table>"),
        ("page_citation_assets/page-3.webp", b"page"),
    ]:
        source = tmp_path / name
        source.parent.mkdir()
        source.write_bytes(content)
        resources.append({"source_path": str(source), "zip_path": name})
    return ZipPackageWriteRequest(
        job_id="writer",
        add_dir=str(tmp_path),
        formatted_chunks=chunks,
        image_files=(resources[0],),
        table_files=(resources[1],),
        page_citation_files=(resources[2],),
        manifest=manifest,
        doc_nav=nav,
        temp_dir=str(tmp_path),
    )


def test_writer_adds_versions_preserves_extensions_and_repeated_asset(package_request):
    request = package_request
    request = replace(package_request, image_files=request.image_files * 2)
    artifact = ZipPackageWriter().write(request)
    result = validate_parse_result_archive(artifact.zip_file_path, allow_legacy=False)
    assert result.doc_nav.model_extra["top_summary"] == request.doc_nav["top_summary"]
    assert [chunk.type for chunk in result.chunks.chunks] == [
        "text",
        "image",
        "table",
        "page",
    ]
    assert "schema_version" not in request.manifest
    assert "schema_version" not in request.doc_nav
    with zipfile.ZipFile(artifact.zip_file_path) as archive:
        assert archive.namelist().count("images/picture.png") == 1
    assert artifact.zip_size == Path(artifact.zip_file_path).stat().st_size
    assert artifact.checksum["algorithm"] == "sha256"


def test_writer_discards_conflicting_duplicate_assets(package_request, tmp_path):
    request = package_request
    source = tmp_path / "conflicting.png"
    source.write_bytes(b"different content")
    request = replace(
        request,
        image_files=(
            *request.image_files,
            {"source_path": str(source), "zip_path": "images/picture.png"},
        ),
    )
    with pytest.raises(ParseResultContractException) as error:
        ZipPackageWriter().write(request)
    assert error.value.details["violations"][0]["reason"] == "conflicting_asset_path"
    assert not (tmp_path / "result_writer.zip").exists()


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing_nav", "required_artifact_missing"),
        ("unsupported_version", "unsupported_schema_version"),
        ("missing_asset", "referenced_asset_missing"),
        ("unsafe_member", "unsafe_member"),
    ],
)
def test_writer_never_returns_invalid_archive(package_request, tmp_path, case, reason):
    request = package_request
    if case == "missing_nav":
        request = replace(package_request, doc_nav=None)
    elif case == "unsupported_version":
        request = replace(package_request, manifest={**request.manifest, "schema_version": 2})
    elif case == "missing_asset":
        request = replace(package_request, image_files=())
    else:
        request = replace(
            request,
            image_files=({**request.image_files[0], "zip_path": "../picture.png"},),
        )
    with pytest.raises(ParseResultContractException) as error:
        ZipPackageWriter().write(request)
    assert error.value.details["violations"][0]["reason"] == reason
    assert not (tmp_path / "result_writer.zip").exists()
