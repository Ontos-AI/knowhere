"""Resource discovery for Knowhere ZIP result packages."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from loguru import logger
from PIL import Image

from shared.core.exceptions.domain_exceptions import StorageServiceException

ZipResourceFileInfo = dict[str, Any]


@dataclass(frozen=True)
class ZipPackageResources:
    image_files: tuple[ZipResourceFileInfo, ...]
    table_files: tuple[ZipResourceFileInfo, ...]

    @property
    def image_files_map(self) -> dict[str, ZipResourceFileInfo]:
        return {str(image["id"]): image for image in self.image_files}

    @property
    def table_files_map(self) -> dict[str, ZipResourceFileInfo]:
        return {str(table["id"]): table for table in self.table_files}


class ZipResourceCollector:
    """Resolve chunk resource references to files that belong in the result ZIP."""

    def collect(
        self,
        *,
        chunks: list[dict[str, Any]],
        add_dir: str,
    ) -> ZipPackageResources:
        images_dir = os.path.join(add_dir, "images")
        tables_dir = os.path.join(add_dir, "tables")
        return ZipPackageResources(
            image_files=tuple(self._collect_image_files(chunks, images_dir)),
            table_files=tuple(self._collect_table_files(chunks, tables_dir)),
        )

    def _collect_image_files(
        self,
        chunks: list[dict[str, Any]],
        images_dir: str,
    ) -> list[ZipResourceFileInfo]:
        image_files: list[ZipResourceFileInfo] = []
        if not os.path.exists(images_dir):
            has_image_chunks = any(chunk.get("type", "") == "image" for chunk in chunks)
            if has_image_chunks:
                raise StorageServiceException(
                    internal_message=(
                        "Image directory not found for ZIP packaging: "
                        f"images_dir={images_dir}"
                    ),
                    operation="collect_image_files",
                )
            return image_files

        image_files_map = _collect_files_by_name(images_dir)

        for chunk in chunks:
            chunk_id = chunk.get("chunk_id") or chunk.get("know_id")
            chunk_type = chunk.get("type", "")

            if chunk_type != "image":
                continue

            metadata = chunk.get("metadata", {})
            candidate_names: list[str] = []
            metadata_file_path = ""
            if metadata and isinstance(metadata, dict):
                metadata_file_path = str(metadata.get("file_path") or "").strip()
                _add_candidate(candidate_names, metadata.get("file_path"))
                _add_candidate(candidate_names, metadata.get("original_name"))

            if metadata_file_path.startswith("images/"):
                original_name = os.path.basename(metadata_file_path)
            else:
                original_name = None

            original_path = chunk.get("path", "")
            if original_path:
                _add_candidate(candidate_names, original_path)
                normalized_path = original_path.replace("-->", "/")
                original_name = os.path.basename(normalized_path)

            source_path, matched_name, ext = _resolve_image_source_path(
                image_files_map,
                candidate_names,
            )
            if matched_name:
                original_name = matched_name

            if not source_path:
                raise StorageServiceException(
                    internal_message=(
                        "Cannot resolve image file for ZIP packaging: "
                        f"chunk_id={chunk_id}, candidates={candidate_names}"
                    ),
                    operation="collect_image_files",
                )

            width = None
            height = None
            try:
                with Image.open(source_path) as img:
                    width, height = img.size
            except Exception as exc:
                logger.debug(
                    f"Failed to read image dimensions for ZIP resource {source_path}: {exc}"
                )

            file_size = os.path.getsize(source_path)
            zip_path = _resolve_image_zip_path(
                metadata=metadata,
                original_name=original_name,
                chunk_id=str(chunk_id),
                ext=ext,
            )

            image_files.append(
                {
                    "id": str(chunk_id),
                    "file_path": zip_path,
                    "original_name": original_name or f"image_{chunk_id}.{ext}",
                    "size_bytes": file_size,
                    "format": ext.lower(),
                    "width": width,
                    "height": height,
                    "source_path": source_path,
                    "zip_path": zip_path,
                }
            )

        return image_files

    def _collect_table_files(
        self,
        chunks: list[dict[str, Any]],
        tables_dir: str,
    ) -> list[ZipResourceFileInfo]:
        table_files: list[ZipResourceFileInfo] = []
        if not os.path.exists(tables_dir):
            return table_files

        table_files_map = {
            filename: file_path
            for filename, file_path in _collect_files_by_name(tables_dir).items()
            if filename.endswith(".html")
        }

        for chunk in chunks:
            chunk_id = chunk.get("chunk_id") or chunk.get("know_id")
            chunk_type = chunk.get("type", "")

            if chunk_type != "table":
                continue

            metadata = chunk.get("metadata", {})
            candidate_names: list[str] = []
            if metadata and isinstance(metadata, dict):
                _add_candidate(candidate_names, metadata.get("file_path"))
                _add_candidate(candidate_names, metadata.get("original_name"))

            original_path = chunk.get("path", "")
            if original_path:
                normalized_path = original_path.replace("-->", "/")
                original_name = os.path.basename(normalized_path)
                _add_candidate(candidate_names, original_path)
            else:
                original_name = None

            source_path, matched_name = _resolve_table_source_path(
                table_files_map,
                candidate_names,
            )
            if matched_name:
                original_name = matched_name

            if not source_path:
                raise StorageServiceException(
                    internal_message=(
                        "Cannot resolve table file for ZIP packaging: "
                        f"chunk_id={chunk_id}, candidates={candidate_names}"
                    ),
                    operation="collect_table_files",
                )

            file_size = os.path.getsize(source_path)
            zip_path = _resolve_table_zip_path(
                metadata=metadata,
                original_name=original_name,
                chunk_id=str(chunk_id),
            )

            table_files.append(
                {
                    "id": str(chunk_id),
                    "file_path": zip_path,
                    "original_name": original_name or f"table_{chunk_id}.html",
                    "size_bytes": file_size,
                    "format": "html",
                    "source_path": source_path,
                    "zip_path": zip_path,
                }
            )

        return table_files


def _collect_files_by_name(directory_path: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for filename in os.listdir(directory_path):
        file_path = os.path.join(directory_path, filename)
        if os.path.isfile(file_path):
            files[filename] = file_path
    return files


def _add_candidate(candidates: list[str], value: str | None) -> None:
    if not value:
        return
    candidate = os.path.basename(str(value).strip().replace("-->", "/"))
    if not candidate:
        return
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1].strip()
    if candidate and candidate not in candidates:
        candidates.append(candidate)


def _resolve_image_source_path(
    image_files_map: dict[str, str],
    candidates: list[str],
) -> tuple[str | None, str | None, str]:
    for candidate in candidates:
        if candidate in image_files_map:
            _, ext = os.path.splitext(candidate)
            return image_files_map[candidate], candidate, ext.lstrip(".") or "jpg"

        stem, _ = os.path.splitext(candidate)
        if stem:
            stem_matches = [
                filename
                for filename in image_files_map
                if os.path.splitext(filename)[0] == stem
            ]
            if len(stem_matches) == 1:
                matched = stem_matches[0]
                _, matched_ext = os.path.splitext(matched)
                return (
                    image_files_map[matched],
                    matched,
                    matched_ext.lstrip(".") or "jpg",
                )

    return None, None, "jpg"


def _resolve_table_source_path(
    table_files_map: dict[str, str],
    candidates: list[str],
) -> tuple[str | None, str | None]:
    for candidate in candidates:
        if candidate in table_files_map:
            return table_files_map[candidate], candidate

        stem, _ = os.path.splitext(candidate)
        if stem:
            stem_matches = [
                filename
                for filename in table_files_map
                if os.path.splitext(filename)[0] == stem
            ]
            if len(stem_matches) == 1:
                matched = stem_matches[0]
                return table_files_map[matched], matched

    return None, None


def _resolve_image_zip_path(
    *,
    metadata: Any,
    original_name: str | None,
    chunk_id: str,
    ext: str,
) -> str:
    if metadata and isinstance(metadata, dict):
        zip_file_path = metadata.get("file_path")
        if zip_file_path and zip_file_path.startswith("images/"):
            return zip_file_path
        if original_name:
            return f"images/{original_name}"
        return f"images/{chunk_id}.{ext}"

    if original_name:
        return f"images/{original_name}"
    return f"images/{chunk_id}.{ext}"


def _resolve_table_zip_path(
    *,
    metadata: Any,
    original_name: str | None,
    chunk_id: str,
) -> str:
    if metadata and isinstance(metadata, dict):
        zip_file_path = metadata.get("file_path")
        if zip_file_path and zip_file_path.startswith("tables/"):
            return zip_file_path
        if original_name:
            return f"tables/{original_name}"
        return f"tables/{chunk_id}.html"

    if original_name:
        return f"tables/{original_name}"
    return f"tables/{chunk_id}.html"
