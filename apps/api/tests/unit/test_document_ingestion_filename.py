from app.services.document_ingestion.creation_service import (
    _resolve_url_source_file_name,
)
from app.services.jobs.result_projection import _resolve_source_file_name


def test_resolve_url_source_file_name_decodes_percent_encoded_path() -> None:
    source_url = (
        "https://files.example.test/"
        "%E4%B8%AD%E6%96%87%E7%AE%80%E5%8E%86-%E9%9B%B7%E7%BF%94-"
        "%E4%B8%AD%E7%A7%91%E9%99%A2%285%29.doc"
    )

    source_file_name = _resolve_url_source_file_name(
        source_url=source_url,
        file_extension=".doc",
    )

    assert source_file_name == "中文简历-雷翔-中科院(5).doc"


def test_resolve_url_source_file_name_prefers_explicit_file_name() -> None:
    source_file_name = _resolve_url_source_file_name(
        source_url="https://files.example.test/original.md",
        file_extension=".md",
        file_name="hipo-upload-optimization_a884bd1b.plan.md",
    )

    assert source_file_name == "hipo-upload-optimization_a884bd1b.plan.md"


def test_resolve_url_source_file_name_appends_extension_when_missing() -> None:
    source_file_name = _resolve_url_source_file_name(
        source_url="https://files.example.test/original.md",
        file_extension=".md",
        file_name="uploaded-plan",
    )

    assert source_file_name == "uploaded-plan.md"


def test_resolve_source_file_name_prefers_request_file_name_over_url_basename() -> None:
    file_name = _resolve_source_file_name(
        {
            "source_url": "https://files.example.test/original.md",
            "file_name": "hipo-upload-optimization_a884bd1b.plan.md",
        }
    )

    assert file_name == "hipo-upload-optimization_a884bd1b.plan.md"
