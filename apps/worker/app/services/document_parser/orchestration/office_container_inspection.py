from __future__ import annotations

import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

CONTENT_TYPES_MEMBER: str = "[Content_Types].xml"
CONTENT_TYPES_NAMESPACE: str = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)


@dataclass(frozen=True)
class OfficeContainerInspection:
    member_names: frozenset[str]
    content_types_xml: bytes | None


def inspect_office_container(file_path: str) -> OfficeContainerInspection | None:
    """Return ZIP members and Content_Types.xml when the path is a readable ZIP."""
    if not zipfile.is_zipfile(file_path):
        return None
    try:
        with zipfile.ZipFile(file_path, "r") as archive:
            member_names = frozenset(archive.namelist())
            content_types_xml = (
                archive.read(CONTENT_TYPES_MEMBER)
                if CONTENT_TYPES_MEMBER in member_names
                else None
            )
    except zipfile.BadZipFile:
        return None
    return OfficeContainerInspection(
        member_names=member_names,
        content_types_xml=content_types_xml,
    )


def read_override_content_type(
    content_types_xml: bytes,
    part_name: str,
) -> str | None:
    """Return the Override ContentType for ``part_name``, or None if absent."""
    try:
        root = ElementTree.fromstring(content_types_xml)
    except ElementTree.ParseError:
        return None

    override_tag = f"{{{CONTENT_TYPES_NAMESPACE}}}Override"
    for override in root.findall(override_tag):
        if override.get("PartName") == part_name:
            return override.get("ContentType")
    return None


__all__ = [
    "CONTENT_TYPES_MEMBER",
    "CONTENT_TYPES_NAMESPACE",
    "OfficeContainerInspection",
    "inspect_office_container",
    "read_override_content_type",
]
