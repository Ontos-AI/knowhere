from __future__ import annotations

import pytest

from app.services.document_parser.orchestration.format_router import (
    SUPPORTED_FILE_TYPES,
    DocumentFormat,
    get_document_parse_adapter,
    resolve_document_format,
)


@pytest.mark.parametrize("extension", SUPPORTED_FILE_TYPES)
def test_supported_extension_resolves_to_an_adapter(extension: str) -> None:
    document_format = resolve_document_format(f"sample{extension}")
    adapter = get_document_parse_adapter(document_format)
    assert adapter.document_format is document_format


def test_adapter_formats_and_supported_extensions_are_inverses() -> None:
    resolved_formats = {
        resolve_document_format(f"sample{extension}")
        for extension in SUPPORTED_FILE_TYPES
    }
    assert resolved_formats == set(DocumentFormat)
    for document_format in DocumentFormat:
        adapter = get_document_parse_adapter(document_format)
        assert adapter.document_format is document_format
