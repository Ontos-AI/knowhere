from __future__ import annotations

import os
from pathlib import Path

from app.services.document_parser.support.internal_parse_name import (
    normalize_internal_parse_name,
    prepare_internal_parse_input,
)


def test_normalize_internal_parse_name_decodes_url_encoded_filename() -> None:
    encoded_filename = (
        "%E4%B8%AD%E6%96%87%E7%AE%80%E5%8E%86-%E9%9B%B7%E7%BF%94-"
        "%E4%B8%AD%E7%A7%91%E9%99%A2%285%29.doc"
    )

    normalized_name = normalize_internal_parse_name(encoded_filename)

    assert normalized_name == "中文简历-雷翔-中科院-5-.doc"


def test_prepare_internal_parse_input_handles_long_encoded_filename(
    tmp_path,
) -> None:
    temporary_file_path = tmp_path / "temporary.pdf"
    temporary_file_path.write_bytes(b"pdf")
    encoded_filename = (
        "%E9%99%84%E4%BB%B65.%E5%8D%97%E4%BA%AC%E4%BF%A1%E6%81%AF%E5%B7%A5%E7%A8%8B"
        * 20
        + ".pdf"
    )

    prepared_input = prepare_internal_parse_input(
        str(temporary_file_path),
        encoded_filename,
        fallback_ext=".pdf",
        prefer_fallback_ext=True,
    )
    prepared_file_path = Path(prepared_input.file_path)
    file_exists = prepared_file_path.exists()
    file_contents = prepared_file_path.read_bytes()

    assert len(os.fsencode(prepared_input.internal_filename)) <= 240
    assert file_exists
    assert file_contents == b"pdf"
