"""Separate embedded media from a presentation before PDF conversion.

The slim package is written to ``work_dir`` for conversion only. Each distinct
embedded media part is probed, transcribed when it has audio, described from
its first frame when it is a video, and stored once per content hash under the
job's result prefix, with a manifest beside it. Media bytes never enter the
result ZIP or snapshot inventory. Every media reference becomes one video or
audio row, and the page row that shows it embeds that row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from io import BytesIO
import os
import posixpath
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from app.services.document_parser.formats.media.asr_task_store import (
    EmbeddedMediaAsrTaskStore,
)
from app.services.document_parser.formats.media.extract import (
    extract_first_frame,
    extract_source_audio,
)
from app.services.document_parser.formats.media.probe import MediaInfo, probe_media
from app.services.document_parser.formats.media.segments import segment_transcript
from app.services.document_parser.formats.media.transcriber import (
    FunAsrSpeechTranscriber,
    MediaAudio,
    SpeechTranscriber,
)
from app.services.document_parser.formats.pptx.embedded_media import (
    EmbeddedMediaSeparationError,
    MediaReference,
    SeparatedPresentation,
    separate_embedded_media,
)
from app.services.document_parser.support.identifiers import get_str_time

from shared.core.exceptions.domain_exceptions import (
    PresentationParsingException,
    ValidationException,
)
from shared.services.chunks.asset_descriptor import (
    EMBEDDED_MEDIA_DIR,
    EXTRACTED_ASSET_KEY,
    PageMedia,
    normalize_page_media_assets,
    page_media_connections,
    page_media_marker,
)
from shared.services.chunks.chunk_connections import merge_connections
from shared.services.chunks.dataframe_chunk_converter import parse_connect_to
from shared.services.storage.job_file_storage import JobFileStorage

EMBEDDED_MEDIA_MANIFEST_NAME = "manifest.json"
EMBEDDED_MEDIA_SEPARATION_FAILED = "EMBEDDED_MEDIA_SEPARATION_FAILED"
EMBEDDED_MEDIA_ASR_USAGE_TASK = "page_memory.embedded_media_asr"
EMBEDDED_MEDIA_FRAME_USAGE_TASK = "page_memory.embedded_media_frame"
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")

TranscriberFactory = Callable[[str], SpeechTranscriber]


@dataclass(frozen=True)
class PresentationMedia:
    """The package to convert, the media its pages carry, and one row per media item."""

    conversion_path: str
    page_media: PageMedia
    media_rows: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _ProbedPart:
    local_path: str
    safe_name: str
    size_bytes: int
    checksum_sha256: str
    duration_ms: int
    has_audio: bool


@dataclass(frozen=True)
class _PartContent:
    transcript: str
    description: str | None


@dataclass(frozen=True)
class _StoredPart:
    path: str
    size_bytes: int
    checksum_sha256: str
    duration_ms: int
    description: str | None


def separate_presentation_media(
    *,
    file_path: str,
    filename: str,
    job_id: str | None,
    work_dir: str,
    asset_model: str | None,
    storage: JobFileStorage | None = None,
    transcriber_factory: TranscriberFactory | None = None,
) -> PresentationMedia:
    """Return the package to convert (slim when anything was separated) and its media."""
    package_bytes = Path(file_path).read_bytes()
    try:
        separated = separate_embedded_media(package_bytes, source_name=filename)
    except EmbeddedMediaSeparationError as exc:
        raise _separation_failed(filename, str(exc), exc) from exc
    if not separated.references and not separated.hidden_slide_numbers:
        return PresentationMedia(conversion_path=file_path, page_media=PageMedia())
    if not job_id:
        raise _separation_failed(filename, "No job id to store embedded media under", None)

    job_storage = storage or JobFileStorage()
    probed_parts = _probe_media_parts(separated=separated, filename=filename, work_dir=work_dir)
    contents = _read_media_parts(
        separated=separated,
        probed_parts=probed_parts,
        filename=filename,
        job_id=job_id,
        work_dir=work_dir,
        asset_model=asset_model,
        transcriber_factory=transcriber_factory or _fun_asr_transcriber,
    )
    stored_parts = _store_media_parts(
        separated=separated,
        probed_parts=probed_parts,
        contents=contents,
        job_id=job_id,
        storage=job_storage,
    )
    manifest = _manifest(separated, stored_parts)
    _store_manifest(manifest=manifest, job_id=job_id, storage=job_storage)
    slim_path = os.path.join(work_dir, filename)
    with open(slim_path, "wb") as handle:
        handle.write(separated.package_bytes)
    logger.bind(
        filename=filename,
        media_count=len(separated.references),
        stored_part_count=len(contents),
        hidden_slide_count=len(separated.hidden_slide_numbers),
        original_bytes=len(package_bytes),
        slim_bytes=len(separated.package_bytes),
    ).info("[page_memory] Separated embedded presentation media")
    assets = normalize_page_media_assets(manifest["assets"])
    return PresentationMedia(
        conversion_path=slim_path,
        page_media=PageMedia.from_descriptors(assets, separated.hidden_slide_numbers),
        media_rows=tuple(
            _media_row(
                asset,
                transcript=(
                    contents[asset["checksum_sha256"]].transcript
                    if asset["checksum_sha256"] in contents
                    else ""
                ),
            )
            for asset in assets
        ),
    )


def attach_presentation_media(
    parsed_df: pd.DataFrame, presentation_media: PresentationMedia
) -> pd.DataFrame:
    """Give page rows their media, markers and edges; add one row per media item."""
    page_media = presentation_media.page_media
    if page_media.is_empty:
        return parsed_df
    rows = parsed_df.to_dict("records")
    for row in rows:
        if row.get("type") != "page":
            continue
        extra_metadata = row.get("extra_metadata")
        metadata: dict[str, Any] = dict(extra_metadata) if isinstance(extra_metadata, dict) else {}
        content = page_media.attach(
            content=str(row.get("content") or ""),
            metadata=metadata,
            pages=_row_pages(row.get("page_nums")),
        )
        row["extra_metadata"] = metadata
        row["content"] = content
        row["length"] = len(content)
        if connections := page_media_connections(metadata):
            row["connectto"] = json.dumps(
                merge_connections(parse_connect_to(row.get("connectto")), connections),
                ensure_ascii=False,
            )
    rows.extend(dict(row) for row in presentation_media.media_rows)
    return pd.DataFrame(rows, columns=parsed_df.columns)


def measure_embedded_media_durations(file_path: str) -> tuple[int, ...]:
    """Durations of a presentation's distinct embedded media, for billing."""
    package_bytes = Path(file_path).read_bytes()
    filename = Path(file_path).name
    try:
        separated = separate_embedded_media(package_bytes, source_name=filename)
    except EmbeddedMediaSeparationError as exc:
        raise _separation_failed(filename, str(exc), exc) from exc
    with tempfile.TemporaryDirectory(prefix="embedded-media-probe-") as work_dir:
        probed = _probe_media_parts(separated=separated, filename=filename, work_dir=work_dir)
    distinct = {part.checksum_sha256: part.duration_ms for part in probed.values()}
    return tuple(distinct.values())


def _row_pages(page_nums: object) -> list[int]:
    return [int(item) for item in str(page_nums or "").split(",") if item.strip().isdigit()]


def _media_row(asset: dict[str, Any], *, transcript: str) -> dict[str, Any]:
    description = asset["summary"] or ""
    content = "\n\n".join(
        part for part in (page_media_marker(asset), description, transcript) if part
    )
    return {
        "content": content,
        "path": f"{EMBEDDED_MEDIA_DIR}/{asset['asset_id']}",
        "type": asset["kind"],
        "length": len(content),
        "keywords": "",
        "summary": description,
        "know_id": asset["asset_id"],
        "tokens": "",
        "connectto": "",
        "addtime": get_str_time(),
        "page_nums": ",".join(str(page) for page in asset["page_nums"]),
        "entities": "",
        "asset_title": asset["title"] or "",
        "extra_metadata": {EXTRACTED_ASSET_KEY: asset},
    }


def _probe_media_parts(
    *,
    separated: SeparatedPresentation,
    filename: str,
    work_dir: str,
) -> dict[str, _ProbedPart]:
    """Write each distinct media part once and probe it."""
    probed_by_part: dict[str, _ProbedPart] = {}
    probed_by_checksum: dict[str, _ProbedPart] = {}
    for part_name, payload in separated.media_parts.items():
        checksum = hashlib.sha256(payload).hexdigest()
        existing = probed_by_checksum.get(checksum)
        if existing is not None:
            probed_by_part[part_name] = existing
            continue
        safe_name = _UNSAFE_NAME_CHARACTERS.sub("_", posixpath.basename(part_name))
        local_path = os.path.join(work_dir, f"{checksum}-{safe_name}")
        with open(local_path, "wb") as handle:
            handle.write(payload)
        info, duration_ms = _probe(local_path, filename=filename, part_name=part_name)
        probed = _ProbedPart(
            local_path=local_path,
            safe_name=safe_name,
            size_bytes=len(payload),
            checksum_sha256=checksum,
            duration_ms=duration_ms,
            has_audio=info.has_audio,
        )
        probed_by_checksum[checksum] = probed
        probed_by_part[part_name] = probed
    return probed_by_part


def _read_media_parts(
    *,
    separated: SeparatedPresentation,
    probed_parts: dict[str, _ProbedPart],
    filename: str,
    job_id: str,
    work_dir: str,
    asset_model: str | None,
    transcriber_factory: TranscriberFactory,
) -> dict[str, _PartContent]:
    """Transcript and first-frame description of each distinct part, by checksum."""
    references_by_checksum: dict[str, list[MediaReference]] = {}
    for reference in separated.references:
        probed = probed_parts.get(reference.part_name or "")
        if probed is not None:
            references_by_checksum.setdefault(probed.checksum_sha256, []).append(reference)
    probed_by_checksum = {part.checksum_sha256: part for part in probed_parts.values()}
    contents: dict[str, _PartContent] = {}
    for checksum, probed in probed_by_checksum.items():
        references = references_by_checksum.get(checksum, [])
        title = next((reference.title for reference in references if reference.title), None)
        part_dir = os.path.join(work_dir, checksum)
        contents[checksum] = _PartContent(
            transcript=(
                _transcribe(
                    probed,
                    filename=filename,
                    job_id=job_id,
                    output_dir=part_dir,
                    transcriber=transcriber_factory(checksum),
                )
                if probed.has_audio
                else ""
            ),
            description=(
                _describe_first_frame(
                    probed,
                    title=title,
                    output_dir=part_dir,
                    asset_model=asset_model,
                )
                if any(reference.kind == "video" for reference in references)
                else None
            ),
        )
    return contents


def _transcribe(
    probed: _ProbedPart,
    *,
    filename: str,
    job_id: str,
    output_dir: str,
    transcriber: SpeechTranscriber,
) -> str:
    """Speaker-labelled transcript; empty when nothing was said."""
    try:
        audio_path = extract_source_audio(probed.local_path, output_dir)
    except ValidationException as exc:
        raise _separation_failed(
            filename, f"Audio of media part {probed.safe_name} could not be extracted", exc
        ) from exc
    transcript = transcriber.transcribe(
        MediaAudio(path=audio_path, job_id=job_id),
        diarize=True,
        language_hint=None,
        usage_task=EMBEDDED_MEDIA_ASR_USAGE_TASK,
    )
    return "\n\n".join(segment.text for segment in segment_transcript(transcript.sentences))


def _describe_first_frame(
    probed: _ProbedPart,
    *,
    title: str | None,
    output_dir: str,
    asset_model: str | None,
) -> str | None:
    """Describe the video's first frame; None when no frame or description comes back."""
    from shared.services.ai.summary.engine import summarize

    try:
        frame_path = extract_first_frame(probed.local_path, output_dir)
    except ValidationException as exc:
        logger.warning(
            "[page_memory] Embedded video {} has no readable first frame: {}",
            probed.safe_name,
            exc.internal_message,
        )
        return None
    result = summarize(
        mode="asset",
        image_paths=[frame_path],
        text=title or "",
        model=asset_model,
        usage_task=EMBEDDED_MEDIA_FRAME_USAGE_TASK,
        asset_title_hint=title or "",
    )
    return result.summary or None


def _fun_asr_transcriber(checksum_sha256: str) -> SpeechTranscriber:
    return FunAsrSpeechTranscriber(task_store=EmbeddedMediaAsrTaskStore(checksum_sha256))


def _store_media_parts(
    *,
    separated: SeparatedPresentation,
    probed_parts: dict[str, _ProbedPart],
    contents: dict[str, _PartContent],
    job_id: str,
    storage: JobFileStorage,
) -> dict[str, _StoredPart]:
    content_types = {
        reference.part_name: reference.content_type
        for reference in separated.references
        if reference.part_name is not None
    }
    stored_by_checksum: dict[str, _StoredPart] = {}
    stored_by_part: dict[str, _StoredPart] = {}
    for part_name, probed in probed_parts.items():
        stored = stored_by_checksum.get(probed.checksum_sha256)
        if stored is None:
            relative_path = (
                f"{EMBEDDED_MEDIA_DIR}/{probed.checksum_sha256[:12]}-{probed.safe_name}"
            )
            with open(probed.local_path, "rb") as handle:
                storage.upload_fileobj(
                    handle,
                    f"{storage.build_result_raw_prefix(job_id=job_id)}{relative_path}",
                    bucket=storage.results_bucket,
                    content_type=content_types.get(part_name),
                )
            stored = _StoredPart(
                path=relative_path,
                size_bytes=probed.size_bytes,
                checksum_sha256=probed.checksum_sha256,
                duration_ms=probed.duration_ms,
                description=contents[probed.checksum_sha256].description,
            )
            stored_by_checksum[probed.checksum_sha256] = stored
        stored_by_part[part_name] = stored
    return stored_by_part


def _probe(local_path: str, *, filename: str, part_name: str) -> tuple[MediaInfo, int]:
    try:
        info = probe_media(local_path)
    except ValidationException as exc:
        raise _separation_failed(
            filename, f"Media part {part_name} could not be probed", exc
        ) from exc
    if info.duration_ms is None:
        raise _separation_failed(
            filename, f"Media part {part_name} has no readable duration", None
        )
    return info, info.duration_ms


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
        "summary": stored.description if stored else None,
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
