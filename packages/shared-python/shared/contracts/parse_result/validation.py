"""Shared producer/consumer validation without rewriting or extracting artifacts."""

from __future__ import annotations

import json
import math
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from shared.core.exceptions.domain_exceptions import ParseResultContractException
from .models import Chunks, DocNav, Manifest, SCHEMA_VERSION


@dataclass(frozen=True)
class ValidatedParseResult:
    manifest: Manifest
    chunks: Chunks
    doc_nav: DocNav | None
    warnings: tuple[str, ...]


def _fail(artifact: str, field: str, reason: str) -> NoReturn:
    raise ParseResultContractException(
        violations=[{"artifact": artifact, "field": field, "reason": reason}],
        schema_version=SCHEMA_VERSION,
    ) from None


def _model(
    model: type[BaseModel],
    payload: Any,
    artifact: str,
    allow_legacy: bool,
    warnings: list[str],
) -> Any:
    if not isinstance(payload, dict):
        _fail(artifact, "$", "object_required")
    value = dict(payload)
    if "schema_version" not in value and allow_legacy:
        value["schema_version"] = SCHEMA_VERSION
        warnings.append(f"{artifact}: missing schema_version treated as legacy v1")
    version = value.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        _fail(artifact, "schema_version", "unsupported_schema_version")
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        # Suppress the raw validation traceback so logs cannot expose input values.
        # Never copy Pydantic input/ctx/message, which may contain document text.
        raise ParseResultContractException(
            violations=[
                {
                    "artifact": artifact,
                    "field": (
                        "HIERARCHY"
                        if item["loc"] and item["loc"][0] == "HIERARCHY"
                        else ".".join(map(str, item["loc"])) or "$"
                    ),
                    "reason": item["type"],
                }
                for item in exc.errors(include_input=False, include_context=False)
            ],
            schema_version=SCHEMA_VERSION,
        ) from None


def validate_parse_result(
    manifest: Any,
    chunks: Any,
    doc_nav: Any,
    *,
    allow_legacy: bool = True,
) -> ValidatedParseResult:
    """Validate v1 JSON roots. Compatibility reads accept old unversioned ZIPs.

    Producers pass allow_legacy=False, requiring explicit versions and doc_nav.
    Unknown fields remain permitted for additive backwards-compatible evolution.
    """
    warnings: list[str] = []
    parsed_manifest: Manifest = _model(
        Manifest, manifest, "manifest.json", allow_legacy, warnings
    )
    parsed_chunks: Chunks = _model(
        Chunks, chunks, "chunks.json", allow_legacy, warnings
    )
    parsed_nav: DocNav | None = None
    if doc_nav is None:
        if (
            allow_legacy
            and isinstance(manifest, dict)
            and "schema_version" not in manifest
            and isinstance(chunks, dict)
            and "schema_version" not in chunks
        ):
            warnings.append("doc_nav.json: absent in legacy v1 package")
        else:
            _fail("doc_nav.json", "$", "required_artifact_missing")
    else:
        parsed_nav = _model(DocNav, doc_nav, "doc_nav.json", allow_legacy, warnings)

    counts = {kind: 0 for kind in ("text", "image", "table", "page")}
    for index, chunk in enumerate(parsed_chunks.chunks):
        counts[chunk.type] += 1
        metadata = chunk.metadata
        if metadata.file_path:
            _validate_asset_path(
                metadata.file_path, "chunks.json", f"chunks.{index}.metadata.file_path"
            )
        for asset_index, asset in enumerate(metadata.page_assets or []):
            _validate_asset_path(
                asset.artifact_ref,
                "chunks.json",
                f"chunks.{index}.metadata.page_assets.{asset_index}.artifact_ref",
            )
        for connection_index, connection in enumerate(metadata.connect_to or []):
            if not isinstance(connection, str) and connection.position:
                position = connection.position
                if position.start > position.end or position.end > len(chunk.content):
                    _fail(
                        "chunks.json",
                        f"chunks.{index}.metadata.connect_to.{connection_index}.position",
                        "invalid_span",
                    )

    expected = {
        "total_chunks": len(parsed_chunks.chunks),
        **{f"{kind}_chunks": count for kind, count in counts.items()},
    }
    for artifact, stats in [
        ("manifest.json", parsed_manifest.statistics),
        ("doc_nav.json", parsed_nav.stats if parsed_nav else None),
    ]:
        if stats is None:
            continue
        for field, count in expected.items():
            if getattr(stats, field) != count:
                _fail(
                    artifact,
                    f"{'statistics' if artifact == 'manifest.json' else 'stats'}.{field}",
                    "chunk_count_mismatch",
                )
    return ValidatedParseResult(
        parsed_manifest, parsed_chunks, parsed_nav, tuple(warnings)
    )


def _safe_member(name: str) -> bool:
    parts = name.rstrip("/").split("/")
    return (
        bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and "\x00" not in name
        and all(part not in {"", ".", ".."} for part in parts)
        and ":" not in parts[0]
    )


def _validate_asset_path(path: str, artifact: str, field: str) -> None:
    if (
        not _safe_member(path)
        or path.endswith("/")
        or PurePosixPath(path).parts[0]
        not in {"images", "tables", "page_citation_assets"}
    ):
        _fail(artifact, field, "invalid_asset_path")


def validate_parse_result_archive(
    path: str,
    *,
    allow_legacy: bool = True,
    max_json_bytes: int | None = 128 * 1024 * 1024,
) -> ValidatedParseResult:
    """Read ZIP metadata without extraction; reject missing assets/unsafe members.

    max_json_bytes bounds each JSON member before decompression for SDK consumers.
    The producer may use None for its own locally generated package.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(set(names)) != len(names):
                _fail("ZIP", "members", "duplicate_member")
            # ZipInfo.filename truncates at NUL; validate the original name.
            if any(not _safe_member(info.orig_filename) for info in infos):
                _fail("ZIP", "members", "unsafe_member")
            files = {info.filename for info in infos if not info.is_dir()}
            payloads: dict[str, Any] = {}
            for name in ("manifest.json", "chunks.json", "doc_nav.json"):
                if name not in files:
                    if name == "doc_nav.json" and allow_legacy:
                        payloads[name] = None
                        continue
                    _fail(name, "$", "required_artifact_missing")
                if (
                    max_json_bytes is not None
                    and archive.getinfo(name).file_size > max_json_bytes
                ):
                    _fail(name, "$", "json_size_limit")
                try:
                    payloads[name] = json.loads(
                        archive.read(name),
                        parse_constant=_invalid_json_constant,
                        parse_float=_finite_json_float,
                        object_pairs_hook=_unique_json_object,
                    )
                except (ValueError, UnicodeError):
                    _fail(name, "$", "invalid_json")
            validated = validate_parse_result(
                payloads["manifest.json"],
                payloads["chunks.json"],
                payloads["doc_nav.json"],
                allow_legacy=allow_legacy,
            )
            for index, chunk in enumerate(validated.chunks.chunks):
                paths = [
                    (chunk.metadata.file_path, f"chunks.{index}.metadata.file_path")
                ]
                paths.extend(
                    (
                        asset.artifact_ref,
                        f"chunks.{index}.metadata.page_assets.{asset_index}.artifact_ref",
                    )
                    for asset_index, asset in enumerate(
                        chunk.metadata.page_assets or []
                    )
                )
                for asset_path, field in paths:
                    if asset_path and asset_path not in files:
                        _fail("chunks.json", field, "referenced_asset_missing")
            return validated
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        NotImplementedError,
        zlib.error,
        EOFError,
    ):
        _fail("ZIP", "$", "unreadable_archive")


def _invalid_json_constant(value: str) -> NoReturn:
    raise ValueError("Non-finite JSON number")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number
