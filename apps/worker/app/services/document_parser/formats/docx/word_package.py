"""Normalize Word OOXML packages so python-docx can open them.

python-docx only accepts the standard WordprocessingML main document content
type. Macro-enabled (``.docm``) and template (``.dotx`` / ``.dotm``) packages
are otherwise identical ZIP+XML documents and fail with:

    file '...' is not a Word file, content type is
    'application/vnd.ms-word.document.macroEnabled.main+xml'
"""

from __future__ import annotations

import io
import zipfile
from typing import BinaryIO

from docx import Document
from docx.opc.constants import CONTENT_TYPE as CT
from loguru import logger

_CONTENT_TYPES_MEMBER = "[Content_Types].xml"
_WML_DOCUMENT_MAIN = CT.WML_DOCUMENT_MAIN
_WORD_MAIN_CONTENT_TYPES_TO_REWRITE: tuple[str, ...] = (
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.ms-word.template.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
)


def normalize_word_package_bytes(package_bytes: bytes) -> bytes:
    """Rewrite template/macro-enabled main content types to standard ``.docx``."""
    if not zipfile.is_zipfile(io.BytesIO(package_bytes)):
        return package_bytes

    with zipfile.ZipFile(io.BytesIO(package_bytes), "r") as source:
        try:
            content_types_xml = source.read(_CONTENT_TYPES_MEMBER)
        except KeyError:
            return package_bytes

        rewritten_xml, original_type = _rewrite_word_main_content_type(content_types_xml)
        if rewritten_xml == content_types_xml:
            return package_bytes

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as destination:
            for item in source.infolist():
                data = (
                    rewritten_xml
                    if item.filename == _CONTENT_TYPES_MEMBER
                    else source.read(item.filename)
                )
                destination.writestr(
                    item.filename,
                    data,
                    compress_type=item.compress_type,
                )

    logger.info(
        "Normalized Word OOXML content type {!r} to standard document.main+xml",
        original_type,
    )
    return output.getvalue()


def open_word_document(source: str | bytes | BinaryIO):
    """Open a Word OOXML package, including ``.docm`` / ``.dotx`` / ``.dotm``."""
    if isinstance(source, bytes):
        package_bytes = source
    elif isinstance(source, str):
        with open(source, "rb") as handle:
            package_bytes = handle.read()
    else:
        package_bytes = source.read()

    return Document(io.BytesIO(normalize_word_package_bytes(package_bytes)))


def _rewrite_word_main_content_type(content_types_xml: bytes) -> tuple[bytes, str | None]:
    rewritten = content_types_xml
    matched_type: str | None = None
    for content_type in _WORD_MAIN_CONTENT_TYPES_TO_REWRITE:
        marker = content_type.encode("ascii")
        if marker not in rewritten:
            continue
        rewritten = rewritten.replace(marker, _WML_DOCUMENT_MAIN.encode("ascii"))
        matched_type = content_type
        break
    return rewritten, matched_type
