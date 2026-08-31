from __future__ import annotations

import os

from app.services.document_parser.support.internal_parse_name import (
    prepare_internal_parse_input,
)


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

    assert len(os.fsencode(prepared_input.internal_filename)) <= 240
    assert os.path.exists(prepared_input.file_path)
    assert open(prepared_input.file_path, "rb").read() == b"pdf"
