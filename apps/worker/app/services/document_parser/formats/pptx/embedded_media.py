# pyright: reportAttributeAccessIssue=false
"""Separate embedded audio and video from a PowerPoint package.

Embedded media contributes nothing to the rendered PDF but dominates package
size and conversion time. This module reads the OOXML package, records every
video and audio reference with its slide and shape, and writes a slim package
for PDF conversion:

- Media shapes keep their poster picture, so the PDF looks like the original.
- Media links, ``p14:media`` extensions, media click actions and timing media
  nodes of those shapes are removed; a media part is dropped only when no
  relationship in the package still targets it.
- Hidden slides are made visible so slide numbers equal PDF page numbers.
- Transition and animation sounds are recorded as audio but stay in the
  package, because removing them means rewriting animation timing trees.

The module is pure: bytes in, bytes and records out. Storage and probing
belong to the caller.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from app.services.document_parser.support.identifiers import gen_str_codes

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
_PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_NAMESPACES = {"p": _P, "a": _A, "r": _R, "p14": _P14}

OFFICE_DOCUMENT_REL = f"{_R}/officeDocument"
SLIDE_REL = f"{_R}/slide"
VIDEO_REL = f"{_R}/video"
AUDIO_REL = f"{_R}/audio"
MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"
_MEDIA_REL_TYPES = frozenset({VIDEO_REL, AUDIO_REL, MEDIA_REL})
_P14_MEDIA_EXT_URI = "{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}"
_MEDIA_CLICK_ACTION = "ppaction://media"
_CONTENT_TYPES_MEMBER = "[Content_Types].xml"
_ROOT_RELS_MEMBER = "_rels/.rels"

_VIDEO_FILE_TAGS = (f"{{{_A}}}videoFile", f"{{{_A}}}quickTimeFile")
_AUDIO_FILE_TAGS = (f"{{{_A}}}audioFile", f"{{{_A}}}wavAudioFile")
_TIMING_MEDIA_TAGS = (f"{{{_P}}}video", f"{{{_P}}}audio")


MediaKind = Literal["video", "audio"]
MediaOrigin = Literal["embedded", "linked"]


class EmbeddedMediaSeparationError(ValueError):
    """The package structure does not allow its media to be separated."""


@dataclass(frozen=True)
class MediaReference:
    """One video or audio reference on one slide."""

    asset_id: str
    slide_number: int
    shape_id: str
    kind: MediaKind
    origin: MediaOrigin
    title: str
    # Shape box as [x0, y0, x1, y1] fractions of the slide; None without a
    # visible shape (sounds) or without an explicit transform.
    region: tuple[float, float, float, float] | None
    part_name: str | None
    external_url: str | None
    content_type: str | None


@dataclass(frozen=True)
class SeparatedPresentation:
    package_bytes: bytes
    references: tuple[MediaReference, ...]
    # Embedded media bytes keyed by package part name.
    media_parts: dict[str, bytes] = field(default_factory=dict)
    hidden_slide_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Relationship:
    rel_id: str
    rel_type: str
    target: str
    is_external: bool


def separate_embedded_media(
    package_bytes: bytes,
    *,
    source_name: str,
) -> SeparatedPresentation:
    """Return the slim package and the media references of a ``.pptx``."""
    try:
        source = zipfile.ZipFile(io.BytesIO(package_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise EmbeddedMediaSeparationError("The presentation is not a ZIP package") from exc
    with source:
        members = {info.filename: info for info in source.infolist()}
        content_types = _read_xml(source, members, _CONTENT_TYPES_MEMBER)
        presentation_part = _main_presentation_part(source, members)
        slide_width, slide_height = _slide_size(_read_xml(source, members, presentation_part))
        slide_parts = _ordered_slide_parts(source, members, presentation_part)

        rewritten: dict[str, bytes] = {}
        references: list[MediaReference] = []
        removed_targets: set[str] = set()
        hidden: list[int] = []
        for slide_number, slide_part in enumerate(slide_parts, start=1):
            slide = _SlideRewrite(
                source=source,
                members=members,
                content_types=content_types,
                slide_part=slide_part,
                slide_number=slide_number,
                source_name=source_name,
                slide_size=(slide_width, slide_height),
            )
            slide.run()
            references.extend(slide.references)
            removed_targets.update(slide.removed_targets)
            if slide.was_hidden:
                hidden.append(slide_number)
            if slide.changed:
                rewritten[slide_part] = _serialize(slide.root)
            if slide.rels_changed:
                rewritten[_rels_part(slide_part)] = _serialize(slide.rels_root)

        if not references and not hidden:
            return SeparatedPresentation(package_bytes=package_bytes, references=())

        still_targeted = _internal_targets(source, members, rewritten)
        dropped_parts = {part for part in removed_targets if part not in still_targeted}
        if dropped_parts:
            rewritten[_CONTENT_TYPES_MEMBER] = _serialize(
                _without_overrides(content_types, dropped_parts)
            )
        media_parts = {
            reference.part_name: source.read(reference.part_name)
            for reference in references
            if reference.part_name is not None
        }
        slim_bytes = _write_package(source, rewritten, dropped_parts)
    return SeparatedPresentation(
        package_bytes=slim_bytes,
        references=tuple(references),
        media_parts=media_parts,
        hidden_slide_numbers=tuple(hidden),
    )


class _SlideRewrite:
    """Record and strip the media of one slide."""

    def __init__(
        self,
        *,
        source: zipfile.ZipFile,
        members: dict[str, zipfile.ZipInfo],
        content_types: etree._Element,
        slide_part: str,
        slide_number: int,
        source_name: str,
        slide_size: tuple[int, int],
    ) -> None:
        self._content_types = content_types
        self._slide_part = slide_part
        self._slide_number = slide_number
        self._source_name = source_name
        self._slide_size = slide_size
        self._members = members
        self.root = _read_xml(source, members, slide_part)
        rels_part = _rels_part(slide_part)
        self.rels_root = (
            _read_xml(source, members, rels_part)
            if rels_part in members
            else etree.Element(f"{{{_PACKAGE_RELS}}}Relationships")
        )
        self._relationships = _relationships(self.rels_root)
        self.references: list[MediaReference] = []
        self.removed_targets: set[str] = set()
        self.was_hidden = False
        self.changed = False
        self.rels_changed = False
        self._seen: set[tuple[str, MediaKind]] = set()

    def run(self) -> None:
        stripped_shape_ids: set[str] = set()
        for picture in list(self.root.iter(f"{{{_P}}}pic")):
            shape_id = self._strip_media_picture(picture)
            if shape_id is not None:
                stripped_shape_ids.add(shape_id)
        if stripped_shape_ids:
            self._strip_timing_media(stripped_shape_ids)
        self._record_sounds()
        if self.root.get("show") == "0":
            del self.root.attrib["show"]
            self.was_hidden = True
            self.changed = True
        self._drop_unreferenced_media_relationships()

    def _strip_media_picture(self, picture: etree._Element) -> str | None:
        non_visual = picture.find("p:nvPicPr", _NAMESPACES)
        properties = non_visual.find("p:nvPr", _NAMESPACES) if non_visual is not None else None
        if properties is None:
            return None
        file_elements = [
            child
            for child in properties
            if child.tag in _VIDEO_FILE_TAGS or child.tag in _AUDIO_FILE_TAGS
        ]
        media_extensions = [
            extension
            for extension in properties.findall("p:extLst/p:ext", _NAMESPACES)
            if extension.get("uri") == _P14_MEDIA_EXT_URI
        ]
        if not file_elements and not media_extensions:
            return None
        drawing = non_visual.find("p:cNvPr", _NAMESPACES)
        if drawing is None or not drawing.get("id"):
            raise EmbeddedMediaSeparationError(
                f"Media shape without an id on slide {self._slide_number}"
            )
        shape_id = str(drawing.get("id"))

        rel_ids = [
            value
            for element in file_elements
            for value in (element.get(f"{{{_R}}}link"), element.get(f"{{{_R}}}embed"))
            if value
        ] + [
            value
            for extension in media_extensions
            for media in extension.findall("p14:media", _NAMESPACES)
            if (value := media.get(f"{{{_R}}}embed"))
        ]
        if not rel_ids:
            raise EmbeddedMediaSeparationError(
                f"Media shape {shape_id} has no media relationship on slide {self._slide_number}"
            )
        targets = [self._media_relationship(rel_id) for rel_id in rel_ids]
        embedded = [rel for rel in targets if not rel.is_external]
        linked = [rel for rel in targets if rel.is_external]
        part_name = self._resolve(embedded[0].target) if embedded else None
        content_type = _content_type(self._content_types, part_name) if part_name else None
        kind = self._media_kind(file_elements, content_type)
        region = _shape_region(picture, self._slide_size)

        key = (shape_id, kind)
        if key not in self._seen:
            # mc:AlternateContent may carry the same shape twice.
            self._seen.add(key)
            self.references.append(
                MediaReference(
                    asset_id=self._asset_id(shape_id, kind),
                    slide_number=self._slide_number,
                    shape_id=shape_id,
                    kind=kind,
                    origin="embedded" if part_name else "linked",
                    title=_shape_title(drawing),
                    region=region,
                    part_name=part_name,
                    external_url=None if part_name else linked[0].target,
                    content_type=content_type,
                )
            )

        for element in file_elements:
            properties.remove(element)
        for extension in media_extensions:
            extension_list = extension.getparent()
            extension_list.remove(extension)
            if len(extension_list) == 0:
                properties.remove(extension_list)
        for click in drawing.findall("a:hlinkClick", _NAMESPACES):
            if click.get("action") == _MEDIA_CLICK_ACTION:
                drawing.remove(click)
        self.removed_targets.update(self._resolve(rel.target) for rel in embedded)
        self.changed = True
        return shape_id

    def _media_relationship(self, rel_id: str) -> _Relationship:
        relationship = self._relationships.get(rel_id)
        if relationship is None or relationship.rel_type not in _MEDIA_REL_TYPES:
            raise EmbeddedMediaSeparationError(
                f"Media relationship {rel_id} is missing on slide {self._slide_number}"
            )
        if not relationship.is_external and self._resolve(relationship.target) not in self._members:
            raise EmbeddedMediaSeparationError(
                f"Media part for {rel_id} is missing on slide {self._slide_number}"
            )
        return relationship

    def _media_kind(
        self, file_elements: list[etree._Element], content_type: str | None
    ) -> MediaKind:
        if file_elements:
            return "video" if file_elements[0].tag in _VIDEO_FILE_TAGS else "audio"
        if content_type and content_type.startswith("video/"):
            return "video"
        if content_type and content_type.startswith("audio/"):
            return "audio"
        raise EmbeddedMediaSeparationError(
            f"Media kind cannot be determined on slide {self._slide_number}"
        )

    def _strip_timing_media(self, shape_ids: set[str]) -> None:
        for tag in _TIMING_MEDIA_TAGS:
            for node in list(self.root.iter(tag)):
                targets = {
                    target.get("spid")
                    for target in node.findall("p:cMediaNode/p:tgtEl/p:spTgt", _NAMESPACES)
                }
                if targets & shape_ids:
                    node.getparent().remove(node)

    def _record_sounds(self) -> None:
        sound_rel_ids = [
            value
            for element in (
                *self.root.iter(f"{{{_P}}}snd"),
                *self.root.iter(f"{{{_P}}}sndTgt"),
            )
            if (value := element.get(f"{{{_R}}}embed"))
        ]
        for ordinal, rel_id in enumerate(sound_rel_ids, start=1):
            relationship = self._media_relationship(rel_id)
            part_name = None if relationship.is_external else self._resolve(relationship.target)
            shape_id = f"sound-{ordinal}"
            self.references.append(
                MediaReference(
                    asset_id=self._asset_id(shape_id, "audio"),
                    slide_number=self._slide_number,
                    shape_id=shape_id,
                    kind="audio",
                    origin="embedded" if part_name else "linked",
                    title=posixpath.basename(relationship.target),
                    region=None,
                    part_name=part_name,
                    external_url=relationship.target if relationship.is_external else None,
                    content_type=(
                        _content_type(self._content_types, part_name) if part_name else None
                    ),
                )
            )

    def _drop_unreferenced_media_relationships(self) -> None:
        referenced = {
            value
            for element in self.root.iter()
            for name, value in element.attrib.items()
            if name.startswith(f"{{{_R}}}")
        }
        for relationship_element in list(self.rels_root):
            rel_id = relationship_element.get("Id")
            if (
                relationship_element.get("Type") in _MEDIA_REL_TYPES
                and rel_id not in referenced
            ):
                self.rels_root.remove(relationship_element)
                self.rels_changed = True

    def _resolve(self, target: str) -> str:
        return _resolve_target(self._slide_part, target)

    def _asset_id(self, shape_id: str, kind: MediaKind) -> str:
        return "asset_" + gen_str_codes(
            f"{self._source_name}::{self._slide_number}::{shape_id}::{kind}"
        )


def _xml_parser() -> etree.XMLParser:
    # Package XML is untrusted input: no entity expansion and no network access.
    # A parser per call, because lxml parsers must not be shared across threads.
    return etree.XMLParser(resolve_entities=False, no_network=True)


def _read_xml(
    source: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], part: str
) -> etree._Element:
    if part not in members:
        raise EmbeddedMediaSeparationError(f"Package part {part} is missing")
    try:
        return etree.fromstring(source.read(part), _xml_parser())
    except etree.XMLSyntaxError as exc:
        raise EmbeddedMediaSeparationError(f"Package part {part} is not valid XML") from exc


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _rels_part(part: str) -> str:
    directory, name = posixpath.split(part)
    return posixpath.join(directory, "_rels", f"{name}.rels")


def _resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _relationships(rels_root: etree._Element) -> dict[str, _Relationship]:
    return {
        str(element.get("Id")): _Relationship(
            rel_id=str(element.get("Id")),
            rel_type=str(element.get("Type") or ""),
            target=str(element.get("Target") or ""),
            is_external=element.get("TargetMode") == "External",
        )
        for element in rels_root
        if element.get("Id")
    }


def _main_presentation_part(
    source: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]
) -> str:
    root_rels = _relationships(_read_xml(source, members, _ROOT_RELS_MEMBER))
    for relationship in root_rels.values():
        if relationship.rel_type == OFFICE_DOCUMENT_REL:
            return _resolve_target("", relationship.target)
    raise EmbeddedMediaSeparationError("The package has no main presentation part")


def _slide_size(presentation: etree._Element) -> tuple[int, int]:
    size = presentation.find("p:sldSz", _NAMESPACES)
    if size is None:
        raise EmbeddedMediaSeparationError("The presentation has no slide size")
    try:
        width, height = int(size.get("cx", "")), int(size.get("cy", ""))
    except ValueError as exc:
        raise EmbeddedMediaSeparationError("The presentation slide size is invalid") from exc
    if width <= 0 or height <= 0:
        raise EmbeddedMediaSeparationError("The presentation slide size is invalid")
    return width, height


def _ordered_slide_parts(
    source: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], presentation_part: str
) -> list[str]:
    presentation = _read_xml(source, members, presentation_part)
    relationships = _relationships(_read_xml(source, members, _rels_part(presentation_part)))
    parts: list[str] = []
    for slide_id in presentation.findall("p:sldIdLst/p:sldId", _NAMESPACES):
        relationship = relationships.get(str(slide_id.get(f"{{{_R}}}id") or ""))
        if relationship is None or relationship.rel_type != SLIDE_REL:
            raise EmbeddedMediaSeparationError("A slide in the slide list has no slide part")
        parts.append(_resolve_target(presentation_part, relationship.target))
    return parts


def _shape_title(drawing: etree._Element) -> str:
    return str(drawing.get("name") or drawing.get("descr") or "").strip()


def _shape_region(
    picture: etree._Element, slide_size: tuple[int, int]
) -> tuple[float, float, float, float] | None:
    transform = picture.find("p:spPr/a:xfrm", _NAMESPACES)
    box = _transform_box(transform)
    if box is None:
        return None
    x, y, width, height = box
    for ancestor in picture.iterancestors(f"{{{_P}}}grpSp"):
        group = ancestor.find("p:grpSpPr/a:xfrm", _NAMESPACES)
        if group is None:
            continue
        offset = group.find("a:off", _NAMESPACES)
        extent = group.find("a:ext", _NAMESPACES)
        child_offset = group.find("a:chOff", _NAMESPACES)
        child_extent = group.find("a:chExt", _NAMESPACES)
        if offset is None or extent is None or child_offset is None or child_extent is None:
            continue
        child_width = int(child_extent.get("cx", "0"))
        child_height = int(child_extent.get("cy", "0"))
        scale_x = int(extent.get("cx", "0")) / child_width if child_width else 1.0
        scale_y = int(extent.get("cy", "0")) / child_height if child_height else 1.0
        x = int(offset.get("x", "0")) + (x - int(child_offset.get("x", "0"))) * scale_x
        y = int(offset.get("y", "0")) + (y - int(child_offset.get("y", "0"))) * scale_y
        width *= scale_x
        height *= scale_y
    slide_width, slide_height = slide_size
    return (
        x / slide_width,
        y / slide_height,
        (x + width) / slide_width,
        (y + height) / slide_height,
    )


def _transform_box(
    transform: etree._Element | None,
) -> tuple[float, float, float, float] | None:
    if transform is None:
        return None
    offset = transform.find("a:off", _NAMESPACES)
    extent = transform.find("a:ext", _NAMESPACES)
    if offset is None or extent is None:
        return None
    return (
        float(offset.get("x", "0")),
        float(offset.get("y", "0")),
        float(extent.get("cx", "0")),
        float(extent.get("cy", "0")),
    )


def _content_type(content_types: etree._Element, part_name: str) -> str | None:
    for override in content_types.findall(f"{{{_CONTENT_TYPES}}}Override"):
        if override.get("PartName") == f"/{part_name}":
            return override.get("ContentType")
    extension = posixpath.splitext(part_name)[1].lstrip(".").lower()
    for default in content_types.findall(f"{{{_CONTENT_TYPES}}}Default"):
        if str(default.get("Extension") or "").lower() == extension:
            return default.get("ContentType")
    return None


def _internal_targets(
    source: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    rewritten: dict[str, bytes],
) -> set[str]:
    targets: set[str] = set()
    for name in members:
        if not name.endswith(".rels"):
            continue
        payload = rewritten.get(name)
        root = (
            etree.fromstring(payload, _xml_parser())
            if payload is not None
            else _read_xml(source, members, name)
        )
        directory = posixpath.dirname(posixpath.dirname(name))
        owner = posixpath.join(directory, posixpath.basename(name)[: -len(".rels")])
        for relationship in _relationships(root).values():
            if not relationship.is_external:
                targets.add(_resolve_target(owner, relationship.target))
    return targets


def _without_overrides(content_types: etree._Element, parts: set[str]) -> etree._Element:
    for override in list(content_types.findall(f"{{{_CONTENT_TYPES}}}Override")):
        if str(override.get("PartName") or "").lstrip("/") in parts:
            content_types.remove(override)
    return content_types


def _write_package(
    source: zipfile.ZipFile, rewritten: dict[str, bytes], dropped_parts: set[str]
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as destination:
        for item in source.infolist():
            if item.filename in dropped_parts:
                continue
            data = rewritten.get(item.filename)
            destination.writestr(
                item,
                data if data is not None else source.read(item.filename),
                compress_type=item.compress_type,
            )
    return output.getvalue()
