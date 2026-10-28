"""TXT ingestion preserves word separators through canonical chunk conversion."""

from __future__ import annotations

from pathlib import Path

import pytest


_UPLOAD_TEXT: str = (
    "Hipo Analytics Acceptance\n\n"
    "This is a synthetic document created for an authorized production upload check "
    "on 27 October 2026.\n\n"
    "Acceptance marker: HIPO_ANALYTICS_ACCEPTANCE_20261027.\n\n"
    "The sample project describes a small library with three document categories: "
    "product notes, research notes, and release records. Successful parsing should "
    "make this document ready for reading and retrieval.\n\n"
    "This fixture contains no customer information, credentials, financial records, "
    "or personal data.\n"
)


def _complete_without_table_of_contents(
    _client: object,
    **_arguments: object,
) -> str:
    return '{"toc_start": null, "toc_end": null, "confidence": "high"}'


@pytest.fixture(autouse=True)
def text_model_boundary(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.ai.openai_compatible_client_sync import (
        OpenAICompatibleClientSync,
    )

    # Keep external model I/O deterministic; parsing and chunk conversion stay real.
    monkeypatch.setattr(
        OpenAICompatibleClientSync,
        "chat_completion",
        _complete_without_table_of_contents,
    )


def _parse_text_chunks(tmp_path: Path, text_content: str) -> list[str]:
    from app.services.document_parser.orchestration.parse_output import ParseOutput
    from app.services.document_parser.parse_service import checkerboard_parse_output
    from shared.services.chunks.dataframe_chunk_converter import (
        JsonValue,
        dataframe_to_chunks,
    )

    text_path: Path = tmp_path / "sample.txt"
    text_path.write_bytes(text_content.encode("utf-8"))
    parse_output: ParseOutput = checkerboard_parse_output(
        file_full_path=str(text_path),
        filename="sample.txt",
        output_dir=str(tmp_path / "parser-output"),
        internal_output_filename="sample.txt",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )
    assert parse_output.parsed_df is not None
    chunks: list[dict[str, JsonValue]] = dataframe_to_chunks(parse_output.parsed_df)
    text_chunks: list[str] = []
    for chunk in chunks:
        if chunk["type"] != "text":
            continue
        content: JsonValue = chunk["content"]
        assert isinstance(content, str)
        text_chunks.append(content)
    return text_chunks


@pytest.mark.parametrize("line_separator", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_txt_upload_preserves_english_word_separators(
    tmp_path: Path,
    line_separator: str,
) -> None:
    text_content: str = _UPLOAD_TEXT.replace("\n", line_separator)
    expected_content: str = "\n".join(line for line in _UPLOAD_TEXT.splitlines() if line)

    assert _parse_text_chunks(tmp_path, text_content) == [expected_content]


@pytest.mark.parametrize(
    "text_content",
    ["A B", "A  B", "A\tB", "A\u00a0B", "中文 English words"],
    ids=["space", "repeated-spaces", "tab", "nonbreaking-space", "mixed-language"],
)
def test_txt_upload_preserves_interior_whitespace(
    tmp_path: Path,
    text_content: str,
) -> None:
    assert _parse_text_chunks(tmp_path, text_content) == [text_content]
