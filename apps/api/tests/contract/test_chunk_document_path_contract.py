from typing import cast

import pytest

from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path
from shared.services.storage.zip_result_schema import ZipResultSchemaBuilder


@pytest.mark.parametrize(
    "chunk_path",
    [
        "acme.com/report.pdf/Intro/Subsection",
        "team/foo.fragment/Intro/Subsection",
        "team/foo.atlas/Intro/Subsection",
        "team/photo.png/Intro/Subsection",
        "images/report.pdf/Intro/Subsection",
        "tables/report.pdf/Intro/Subsection",
        "client.pdf/report.pdf/Intro/Subsection",
    ],
)
def test_should_read_legacy_namespace_chunk_paths_as_document_sections(
    chunk_path: str,
) -> None:
    source_file_name = chunk_path.split("/")[1]

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_legacy_path",
                "type": "text",
                "content": "legacy path content",
                "path": chunk_path,
                "metadata": {"summary": "legacy path summary"},
            }
        ],
        source_file_name,
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])
    children = cast(list[dict[str, object]], sections[0]["children"])

    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name=source_file_name,
        )
        == "Intro / Subsection"
    )
    assert sections[0]["title"] == "Intro"
    assert sections[0]["path"] == "/".join(chunk_path.split("/")[:3])
    assert children[0]["title"] == "Subsection"
    assert children[0]["path"] == chunk_path


@pytest.mark.parametrize(
    "chunk_path",
    [
        "images/photo.png",
        "tables/table.html",
    ],
)
def test_should_keep_media_resource_paths_at_root(chunk_path: str) -> None:
    assert section_path_from_chunk_path(chunk_path) == "Root"


def test_should_not_treat_dotted_section_titles_as_legacy_document_files() -> None:
    chunk_path = "report.pdf/1. Introduction/Details"

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_numbered_section",
                "type": "text",
                "content": "numbered section content",
                "path": chunk_path,
                "metadata": {"summary": "numbered section summary"},
            }
        ],
        "report.pdf",
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])
    children = cast(list[dict[str, object]], sections[0]["children"])

    assert section_path_from_chunk_path(chunk_path) == "1. Introduction / Details"
    assert sections[0]["title"] == "1. Introduction"
    assert sections[0]["path"] == "report.pdf/1. Introduction"
    assert children[0]["title"] == "Details"
    assert children[0]["path"] == chunk_path


def test_should_read_arrow_delimited_document_paths_as_section_paths() -> None:
    chunk_path = "report.pdf-->Intro-->Subsection"

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_arrow_delimited_path",
                "type": "text",
                "content": "arrow delimited content",
                "path": chunk_path,
                "metadata": {"summary": "arrow delimited summary"},
            }
        ],
        "report.pdf",
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])
    children = cast(list[dict[str, object]], sections[0]["children"])

    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name="report.pdf",
        )
        == "Intro / Subsection"
    )
    assert sections[0]["title"] == "Intro"
    assert sections[0]["path"] == "report.pdf/Intro"
    assert children[0]["title"] == "Subsection"
    assert children[0]["path"] == "report.pdf/Intro/Subsection"


def test_should_preserve_literal_arrow_text_in_slash_paths() -> None:
    chunk_path = "report.pdf/Inputs --> Outputs/Details"

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_literal_arrow_heading",
                "type": "text",
                "content": "literal arrow content",
                "path": chunk_path,
                "metadata": {"summary": "literal arrow summary"},
            }
        ],
        "report.pdf",
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])
    children = cast(list[dict[str, object]], sections[0]["children"])

    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name="report.pdf",
        )
        == "Inputs --> Outputs / Details"
    )
    assert sections[0]["title"] == "Inputs --> Outputs"
    assert sections[0]["path"] == "report.pdf/Inputs --> Outputs"
    assert children[0]["title"] == "Details"
    assert children[0]["path"] == chunk_path


def test_should_preserve_single_level_literal_arrow_headings() -> None:
    chunk_path = "report.pdf/Inputs --> Outputs"

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_single_arrow_heading",
                "type": "text",
                "content": "single arrow heading content",
                "path": chunk_path,
                "metadata": {"summary": "single arrow heading summary"},
            }
        ],
        "report.pdf",
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])

    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name="report.pdf",
        )
        == "Inputs --> Outputs"
    )
    assert sections[0]["title"] == "Inputs --> Outputs"
    assert sections[0]["path"] == chunk_path
    assert sections[0]["children"] == []


def test_should_preserve_filename_like_arrow_text_below_document_root() -> None:
    chunk_path = "report.pdf/input.csv-->output/Details"

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_filename_arrow_heading",
                "type": "text",
                "content": "filename arrow heading content",
                "path": chunk_path,
                "metadata": {"summary": "filename arrow heading summary"},
            }
        ],
        "report.pdf",
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])
    children = cast(list[dict[str, object]], sections[0]["children"])

    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name="report.pdf",
        )
        == "input.csv-->output / Details"
    )
    assert sections[0]["title"] == "input.csv-->output"
    assert sections[0]["path"] == "report.pdf/input.csv-->output"
    assert children[0]["title"] == "Details"
    assert children[0]["path"] == chunk_path


def test_should_preserve_filename_like_section_titles_for_new_paths() -> None:
    chunk_path = "report.pdf/appendix.pdf/Details"

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_filename_section",
                "type": "text",
                "content": "filename-like section content",
                "path": chunk_path,
                "metadata": {"summary": "filename-like section summary"},
            }
        ],
        "report.pdf",
    )

    sections = cast(list[dict[str, object]], doc_nav["sections"])
    children = cast(list[dict[str, object]], sections[0]["children"])

    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name="report.pdf",
        )
        == "appendix.pdf / Details"
    )
    assert sections[0]["title"] == "appendix.pdf"
    assert sections[0]["path"] == "report.pdf/appendix.pdf"
    assert children[0]["title"] == "Details"
    assert children[0]["path"] == chunk_path
