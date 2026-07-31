"""Physical ZIP writing for Knowhere result packages."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from shared.services.storage.zip_result_resources import ZipResourceFileInfo

PackageProfile = Literal["chunk", "page_memory"]
DOC_PROFILE_FILENAME = "doc_profile.json"


@dataclass(frozen=True)
class ZipPackageWriteRequest:
    job_id: str
    add_dir: str
    formatted_chunks: list[dict[str, Any]]
    image_files: tuple[ZipResourceFileInfo, ...]
    table_files: tuple[ZipResourceFileInfo, ...]
    doc_nav: dict[str, Any] | None
    manifest: dict[str, Any]
    temp_dir: str | None
    package_profile: PackageProfile = "chunk"
    zip_file_name: str | None = None


@dataclass(frozen=True)
class ZipPackageArtifact:
    zip_file_path: str
    checksum: dict[str, str]
    zip_size: int


class ZipPackageWriter:
    """Write a prepared result package to a ZIP file."""

    def write(self, request: ZipPackageWriteRequest) -> ZipPackageArtifact:
        effective_temp_dir = request.temp_dir or tempfile.gettempdir()
        os.makedirs(effective_temp_dir, exist_ok=True)
        zip_name = request.zip_file_name or f"result_{request.job_id}.zip"
        zip_file_path = os.path.join(effective_temp_dir, zip_name)

        with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            chunks_json = json.dumps(
                {"chunks": request.formatted_chunks},
                ensure_ascii=False,
                indent=2,
            )
            zip_file.writestr("chunks.json", chunks_json.encode("utf-8"))

            if self._write_optional_file(
                zip_file,
                request.add_dir,
                DOC_PROFILE_FILENAME,
            ):
                logger.info("Added doc_profile.json to ZIP")

            if request.package_profile != "page_memory":
                self._write_optional_file(zip_file, request.add_dir, "full.md")
                if self._write_optional_file(
                    zip_file,
                    request.add_dir,
                    "toc_hierarchies.json",
                ):
                    logger.info("Added toc_hierarchies.json to ZIP")
                if self._write_optional_json_file(
                    zip_file,
                    request.add_dir,
                    "_doc_agent/trace.json",
                    "debug/trace.json",
                    compact_trace=True,
                ):
                    logger.info("Added debug/trace.json to ZIP")

            self._write_resource_files(zip_file, request.image_files, label="Image")
            self._write_resource_files(zip_file, request.table_files, label="Table")

            if request.doc_nav is not None:
                doc_nav_json = json.dumps(request.doc_nav, ensure_ascii=False, indent=2)
                zip_file.writestr("doc_nav.json", doc_nav_json.encode("utf-8"))
                logger.info("Added doc_nav.json")

            manifest_json = json.dumps(request.manifest, ensure_ascii=False, indent=2)
            zip_file.writestr("manifest.json", manifest_json.encode("utf-8"))

        checksum_value = _calculate_zip_checksum(zip_file_path)
        zip_size = os.path.getsize(zip_file_path)
        return ZipPackageArtifact(
            zip_file_path=zip_file_path,
            checksum={"algorithm": "sha256", "value": checksum_value},
            zip_size=zip_size,
        )

    def _write_optional_file(
        self,
        zip_file: zipfile.ZipFile,
        add_dir: str,
        filename: str,
    ) -> bool:
        file_path = os.path.join(add_dir, filename)
        if not os.path.exists(file_path):
            return False
        zip_file.write(file_path, filename)
        return True

    def _write_optional_json_file(
        self,
        zip_file: zipfile.ZipFile,
        add_dir: str,
        source_name: str,
        zip_name: str,
        *,
        compact_trace: bool = False,
    ) -> bool:
        file_path = os.path.join(add_dir, source_name)
        if not os.path.exists(file_path):
            return False
        try:
            with open(file_path, encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if compact_trace:
                payload = _compact_trace_payload(payload)
            zip_file.writestr(
                zip_name,
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            return True
        except Exception as exc:
            logger.warning(f"Failed to add {source_name} to ZIP: {exc}")
            return False

    def _write_resource_files(
        self,
        zip_file: zipfile.ZipFile,
        resources: tuple[ZipResourceFileInfo, ...],
        *,
        label: str,
    ) -> None:
        for resource in resources:
            source_path = resource["source_path"]
            if os.path.exists(source_path):
                zip_file.write(source_path, resource["zip_path"])
            else:
                logger.warning(f"{label} file not found: {source_path}")


def _calculate_zip_checksum(zip_file_path: str) -> str:
    sha256_hash = hashlib.sha256()
    with open(zip_file_path, "rb") as file_obj:
        for byte_block in iter(lambda: file_obj.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest().lower()


def _compact_trace_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    compacted = dict(payload)
    steps: list[Any] = []
    for step in compacted.get("steps") or []:
        if not isinstance(step, dict):
            steps.append(step)
            continue
        item = dict(step)
        observation = item.get("observation")
        if isinstance(observation, dict):
            compact_observation = dict(observation)
            compact_observation.pop("payload", None)
            item["observation"] = compact_observation
        steps.append(item)
    compacted["steps"] = steps
    return compacted
