"""Separate embedded media from a presentation before PDF conversion.

The slim package is written to ``work_dir`` for conversion only. Each embedded
media part is probed, stored once per content hash under the job's result
prefix, and described in a manifest stored beside it. Media bytes never enter
the result ZIP or snapshot inventory.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.document_parser.formats.media.probe import probe_media
from app.services.document_parser.formats.pptx.embedded_media import (
    EmbeddedMediaSeparationError,
    MediaReference,
    SeparatedPresentation,
    separate_embedded_media,
)

from shared.core.exceptions.domain_exceptions import (
    PresentationParsingException,
    ValidationException,
)
from shared.services.storage.job_file_storage import JobFileStorage

EMBEDDED_MEDIA_DIR = "embedded_media"
EMBEDDED_MEDIA_MANIFEST_NAME = "manifest.json"
EMBEDDED_MEDIA_SEPARATION_FAILED = "EMBEDDED_MEDIA_SEPARATION_FAILED"
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class _StoredPart:
    path: str
    size_bytes: int
    checksum_sha256: str
    duration_ms: int


def separate_presentation_media(
    *,
    file_path: str,
    filename: str,
    job_id: str | None,
    work_dir: str,
    storage: JobFileStorage | None = None,
) -> str:
    """Return the path of the package to convert: slim when anything was separated."""
    package_bytes = Path(file_path).read_bytes()
    try:
        separated = separate_embedded_media(package_bytes, source_name=filename)
    except EmbeddedMediaSeparationError as exc:
        raise _separation_failed(filename, str(exc), exc) from exc
    if not separated.references and not separated.hidden_slide_numbers:
        return file_path
    if not job_id:
        raise _separation_failed(filename, "No job id to store embedded media under", None)

    job_storage = storage or JobFileStorage()
    stored_parts = _store_media_parts(
        separated=separated,
        filename=filename,
        job_id=job_id,
        work_dir=work_dir,
        storage=job_storage,
    )
    _store_manifest(
        manifest=_manifest(separated, stored_parts),
        job_id=job_id,
        storage=job_storage,
    )
    slim_path = os.path.join(work_dir, filename)
    with open(slim_path, "wb") as handle:
        handle.write(separated.package_bytes)
    logger.bind(
        filename=filename,
        media_count=len(separated.references),
        stored_part_count=len(stored_parts),
        hidden_slide_count=len(separated.hidden_slide_numbers),
        original_bytes=len(package_bytes),
        slim_bytes=len(separated.package_bytes),
    ).info("[page_memory] Separated embedded presentation media")
    return slim_path


def _store_media_parts(
    *,
    separated: SeparatedPresentation,
    filename: str,
    job_id: str,
    work_dir: str,
    storage: JobFileStorage,
) -> dict[str, _StoredPart]:
    stored_by_part: dict[str, _StoredPart] = {}
    stored_by_checksum: dict[str, _StoredPart] = {}
    content_types = {
        reference.part_name: reference.content_type
        for reference in separated.references
        if reference.part_name is not None
    }
    for part_name, payload in separated.media_parts.items():
        checksum = hashlib.sha256(payload).hexdigest()
        existing = stored_by_checksum.get(checksum)
        if existing is not None:
            stored_by_part[part_name] = existing
            continue
        safe_name = _UNSAFE_NAME_CHARACTERS.sub("_", posixpath.basename(part_name))
        local_path = os.path.join(work_dir, f"{checksum}-{safe_name}")
        with open(local_path, "wb") as handle:
            handle.write(payload)
        duration_ms = _duration_ms(local_path, filename=filename, part_name=part_name)
        relative_path = f"{EMBEDDED_MEDIA_DIR}/{checksum[:12]}-{safe_name}"
        with open(local_path, "rb") as handle:
            storage.upload_fileobj(
                handle,
                f"{storage.build_result_raw_prefix(job_id=job_id)}{relative_path}",
                bucket=storage.results_bucket,
                content_type=content_types.get(part_name),
            )
        stored = _StoredPart(
            path=relative_path,
            size_bytes=len(payload),
            checksum_sha256=checksum,
            duration_ms=duration_ms,
        )
        stored_by_checksum[checksum] = stored
        stored_by_part[part_name] = stored
    return stored_by_part


def _duration_ms(local_path: str, *, filename: str, part_name: str) -> int:
    try:
        duration_ms = probe_media(local_path).duration_ms
    except ValidationException as exc:
        raise _separation_failed(
            filename, f"Media part {part_name} could not be probed", exc
        ) from exc
    if duration_ms is None:
        raise _separation_failed(
            filename, f"Media part {part_name} has no readable duration", None
        )
    return duration_ms


def _manifest(
    separated: SeparatedPresentation, stored_parts: dict[str, _StoredPart]
) -> dict[str, Any]:
    return {
        "hidden_slide_numbers": list(separated.hidden_slide_numbers),
        "assets": [
            _asset_descriptor(reference, stored_parts.get(reference.part_name or ""))
            for reference in separated.references
        ],
    }


def _asset_descriptor(
    reference: MediaReference, stored: _StoredPart | None
) -> dict[str, Any]:
    return {
        "asset_id": reference.asset_id,
        "kind": reference.kind,
        "origin": reference.origin,
        "title": reference.title,
        "page_nums": [reference.slide_number],
        "region": (
            {
                "page_num": reference.slide_number,
                "bbox": list(reference.region),
                "coord_system": "page_fraction",
            }
            if reference.region is not None
            else None
        ),
        "content_type": reference.content_type,
        "size_bytes": stored.size_bytes if stored else None,
        "checksum_sha256": stored.checksum_sha256 if stored else None,
        "duration_ms": stored.duration_ms if stored else None,
        "ref": (
            {"kind": "artifact", "path": stored.path}
            if stored
            else {"kind": "external", "url": reference.external_url}
        ),
    }


def _store_manifest(*, manifest: dict[str, Any], job_id: str, storage: JobFileStorage) -> None:
    storage.upload_fileobj(
        BytesIO(json.dumps(manifest, ensure_ascii=False).encode("utf-8")),
        f"{storage.build_result_raw_prefix(job_id=job_id)}"
        f"{EMBEDDED_MEDIA_DIR}/{EMBEDDED_MEDIA_MANIFEST_NAME}",
        bucket=storage.results_bucket,
        content_type="application/json",
    )


def _separation_failed(
    filename: str, internal_message: str, exc: Exception | None
) -> PresentationParsingException:
    return PresentationParsingException(
        user_message=(
            "The embedded audio or video in this presentation could not be "
            "separated for parsing."
        ),
        reason=EMBEDDED_MEDIA_SEPARATION_FAILED,
        internal_message=f"{internal_message}: {filename}",
        original_exception=exc,
    )
