from __future__ import annotations

import os
from enum import Enum

from app.services.document_parser.orchestration import format_adapters
from app.services.document_parser.orchestration.format_adapters import (
    DocumentParseAdapter,
)
from shared.core.exceptions.domain_exceptions import ValidationException


class DocumentFormat(str, Enum):
    TEXT = "text"
    FRAGMENT = "fragment"
    IMAGE = "image"
    PDF = "pdf"
    DOC = "doc"
    DOCX = "docx"
    XLS = "xls"
    XLSX = "xlsx"
    PPTX = "pptx"
    MARKDOWN = "markdown"
    JSON = "json"


SUPPORTED_FILE_TYPES: tuple[str, ...] = (
    ".txt",
    ".fragment",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".pptx",
    ".md",
    ".json",
)


def resolve_document_format(file_path: str) -> DocumentFormat:
    extension = os.path.splitext(file_path)[1].lower()
    if extension == ".fragment":
        return DocumentFormat.FRAGMENT
    if extension == ".txt":
        return DocumentFormat.TEXT
    if extension in (".png", ".jpg", ".jpeg"):
        return DocumentFormat.IMAGE
    if extension == ".pdf":
        return DocumentFormat.PDF
    if extension == ".doc":
        return DocumentFormat.DOC
    if extension == ".docx":
        return DocumentFormat.DOCX
    if extension == ".xls":
        return DocumentFormat.XLS
    if extension == ".xlsx":
        return DocumentFormat.XLSX
    if extension == ".pptx":
        return DocumentFormat.PPTX
    if extension == ".md":
        return DocumentFormat.MARKDOWN
    if extension == ".json":
        return DocumentFormat.JSON

    raise ValidationException(
        user_message=f"Unsupported file type: {extension}",
        violations=[
            {
                "field": "file_type",
                "description": f"Must be one of: {', '.join(SUPPORTED_FILE_TYPES)}",
            }
        ],
    )


def get_document_parse_adapter(document_format: DocumentFormat) -> DocumentParseAdapter:
    adapter_by_format: dict[DocumentFormat, DocumentParseAdapter] = {
        DocumentFormat.FRAGMENT: format_adapters.FragmentParseAdapter(document_format),
        DocumentFormat.TEXT: format_adapters.TextParseAdapter(document_format),
        DocumentFormat.IMAGE: format_adapters.ImageParseAdapter(document_format),
        DocumentFormat.PDF: format_adapters.PdfParseAdapter(document_format),
        DocumentFormat.DOC: format_adapters.DocParseAdapter(document_format),
        DocumentFormat.DOCX: format_adapters.DocxParseAdapter(document_format),
        DocumentFormat.XLS: format_adapters.XlsParseAdapter(document_format),
        DocumentFormat.XLSX: format_adapters.XlsxParseAdapter(document_format),
        DocumentFormat.PPTX: format_adapters.PptxParseAdapter(document_format),
        DocumentFormat.MARKDOWN: format_adapters.MarkdownParseAdapter(document_format),
        DocumentFormat.JSON: format_adapters.JsonParseAdapter(document_format),
    }
    return adapter_by_format[document_format]
