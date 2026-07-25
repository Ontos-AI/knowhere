from __future__ import annotations

import zipfile
from dataclasses import dataclass

from app.services.document_parser.orchestration.format_router import DocumentFormat

from shared.core.exceptions.domain_exceptions import ValidationException


@dataclass(frozen=True)
class _OfficeContainerRequirement:
    extension: str
    document_label: str
    required_member: str

    @property
    def user_message(self) -> str:
        return (
            f"Invalid file: the uploaded {self.extension} file is not a valid "
            f"{self.document_label}. Please check the file and upload again."
        )

    @property
    def violation_description(self) -> str:
        return (
            f"Expected a valid {self.extension[1:].upper()} ZIP package containing "
            f"{self.required_member}"
        )


_CONTENT_TYPES_MEMBER: str = "[Content_Types].xml"
_OFFICE_CONTAINER_REQUIREMENTS: dict[
    DocumentFormat,
    _OfficeContainerRequirement,
] = {
    DocumentFormat.DOCX: _OfficeContainerRequirement(
        extension=".docx",
        document_label="Word document",
        required_member="word/document.xml",
    ),
    DocumentFormat.XLSX: _OfficeContainerRequirement(
        extension=".xlsx",
        document_label="Excel workbook",
        required_member="xl/workbook.xml",
    ),
    DocumentFormat.PPTX: _OfficeContainerRequirement(
        extension=".pptx",
        document_label="PowerPoint presentation",
        required_member="ppt/presentation.xml",
    ),
}


def validate_office_container(
    file_path: str,
    document_format: DocumentFormat,
) -> None:
    """Validate OOXML containers before dispatching to archive-based parsers."""
    requirement = _OFFICE_CONTAINER_REQUIREMENTS.get(document_format)
    if requirement is None:
        return

    if not zipfile.is_zipfile(file_path):
        _raise_invalid_office_file(requirement)

    try:
        with zipfile.ZipFile(file_path, "r") as archive:
            member_names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise _build_invalid_office_file_exception(requirement) from exc

    if (
        _CONTENT_TYPES_MEMBER not in member_names
        or requirement.required_member not in member_names
    ):
        _raise_invalid_office_file(requirement)


def _raise_invalid_office_file(requirement: _OfficeContainerRequirement) -> None:
    raise _build_invalid_office_file_exception(requirement)


def _build_invalid_office_file_exception(
    requirement: _OfficeContainerRequirement,
) -> ValidationException:
    return ValidationException(
        user_message=requirement.user_message,
        violations=[
            {
                "field": "file",
                "description": requirement.violation_description,
            }
        ],
    )


__all__ = ["validate_office_container"]
