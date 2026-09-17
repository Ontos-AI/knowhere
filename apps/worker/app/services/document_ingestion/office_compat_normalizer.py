from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NoReturn

from app.services.document_parser.conversion.legacy_converter import (
    normalize_docx_variant,
    normalize_xlsx_variant,
)
from app.services.document_parser.orchestration.office_container_inspection import (
    inspect_office_container,
    read_override_content_type,
)
from loguru import logger

from shared.core.exceptions.domain_exceptions import ValidationException

OLE_CFBF_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")

_STANDARD_MAIN_CONTENT_TYPES: dict[str, str] = {
    ".docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml"
    ),
}
_MAIN_PART_BY_EXTENSION: dict[str, str] = {
    ".docx": "/word/document.xml",
    ".xlsx": "/xl/workbook.xml",
}
_VARIANT_CONTENT_TYPE_MARKERS: tuple[str, ...] = (
    "macroEnabled.main+xml",
    "template.main+xml",
)
_NORMALIZED_OUTPUT_DIRNAME = "office_compat"


@dataclass(frozen=True)
class OfficeCompatNormalization:
    file_path: str
    conversion: dict[str, object] | None = None


def normalize_office_source(file_path: str) -> OfficeCompatNormalization:
    """Convert known DOCX/XLSX OOXML variants; reject encrypted OLE containers."""
    extension = os.path.splitext(file_path)[1].lower()
    if extension not in _STANDARD_MAIN_CONTENT_TYPES:
        return OfficeCompatNormalization(file_path=file_path)

    if _has_ole_cfbf_signature(file_path):
        _raise_encrypted_office_file(extension)

    inspection = inspect_office_container(file_path)
    if inspection is None or inspection.content_types_xml is None:
        return OfficeCompatNormalization(file_path=file_path)

    content_type = read_override_content_type(
        inspection.content_types_xml,
        _MAIN_PART_BY_EXTENSION[extension],
    )
    if content_type is None:
        return OfficeCompatNormalization(file_path=file_path)
    if content_type == _STANDARD_MAIN_CONTENT_TYPES[extension]:
        return OfficeCompatNormalization(file_path=file_path)
    if not _is_known_ooxml_variant(content_type):
        return OfficeCompatNormalization(file_path=file_path)

    outdir = os.path.join(os.path.dirname(file_path), _NORMALIZED_OUTPUT_DIRNAME)
    if extension == ".docx":
        converted_path, _converted_name = normalize_docx_variant(file_path, outdir)
    else:
        converted_path, _converted_name = normalize_xlsx_variant(file_path, outdir)
    logger.info(
        "Office compatibility conversion applied: "
        f"source_path={file_path}, normalized_path={converted_path}, "
        f"content_type={content_type}"
    )
    return OfficeCompatNormalization(
        file_path=converted_path,
        conversion={"content_type": content_type},
    )


def _has_ole_cfbf_signature(file_path: str) -> bool:
    with open(file_path, "rb") as handle:
        header = handle.read(len(OLE_CFBF_SIGNATURE))
    return header == OLE_CFBF_SIGNATURE


def _is_known_ooxml_variant(content_type: str) -> bool:
    return any(marker in content_type for marker in _VARIANT_CONTENT_TYPE_MARKERS)


def _raise_encrypted_office_file(extension: str) -> NoReturn:
    raise ValidationException(
        user_message=(
            f"Invalid file: the uploaded {extension} file is encrypted or "
            "password-protected. Please unlock the file and upload again."
        ),
        violations=[
            {
                "field": "file",
                "description": "Encrypted or password-protected Office file",
            }
        ],
    )
