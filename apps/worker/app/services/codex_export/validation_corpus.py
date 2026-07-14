"""Safe, repository-relative document corpus for Codex export validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping


CORPUS_SCHEMA_VERSION = "codex-validation-corpus/1.0"
_DOCUMENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SUPPORTED_SUFFIXES = {".pdf", ".docx"}
_EXPECTED_STATUSES = {"completed", "failed"}


@dataclass(frozen=True)
class ValidationDocument:
    document_id: str
    root_name: str
    relative_path: Path
    source_path: Path
    tags: tuple[str, ...]
    language: str
    pages: tuple[int, ...]
    expected_status: str


@dataclass(frozen=True)
class ValidationCorpus:
    schema_version: str
    documents: tuple[ValidationDocument, ...]


def _require_string(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _safe_relative_path(raw_path: str) -> Path:
    posix_path = PurePosixPath(raw_path.replace("\\", "/"))
    windows_path = PureWindowsPath(raw_path)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("document path must be relative")
    if ".." in posix_path.parts:
        raise ValueError("document path may not escape its root")
    if posix_path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ValueError("document must be a PDF or DOCX file")
    return Path(*posix_path.parts)


def _load_record(
    raw: object,
    *,
    roots: Mapping[str, Path],
    seen_ids: set[str],
) -> ValidationDocument:
    if not isinstance(raw, dict):
        raise ValueError("each corpus document must be an object")

    document_id = _require_string(raw, "id")
    if not _DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise ValueError(f"invalid document id: {document_id!r}")
    if document_id in seen_ids:
        raise ValueError(f"duplicate document id: {document_id}")
    seen_ids.add(document_id)

    root_name = _require_string(raw, "root")
    if root_name not in roots:
        raise ValueError(f"unknown corpus root: {root_name}")
    root = roots[root_name].resolve(strict=True)
    relative_path = _safe_relative_path(_require_string(raw, "path"))
    source_path = (root / relative_path).resolve(strict=False)
    if not source_path.is_relative_to(root):
        raise ValueError("document path may not escape its root")
    if not source_path.is_file():
        raise ValueError(f"document does not exist: {relative_path.as_posix()}")

    raw_tags = raw.get("tags")
    if not isinstance(raw_tags, list) or not all(
        isinstance(tag, str) and tag.strip() for tag in raw_tags
    ):
        raise ValueError("tags must be a list of non-empty strings")
    tags = tuple(tag.strip() for tag in raw_tags)

    language = _require_string(raw, "language")
    raw_pages = raw.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages or not all(
        isinstance(page, int) and not isinstance(page, bool) and page > 0
        for page in raw_pages
    ):
        raise ValueError("pages must contain positive integers")
    pages = tuple(raw_pages)

    expected_status = _require_string(raw, "expected_status")
    if expected_status not in _EXPECTED_STATUSES:
        raise ValueError("expected_status must be completed or failed")

    return ValidationDocument(
        document_id=document_id,
        root_name=root_name,
        relative_path=relative_path,
        source_path=source_path,
        tags=tags,
        language=language,
        pages=pages,
        expected_status=expected_status,
    )


def load_validation_corpus(
    corpus_path: Path,
    *,
    roots: Mapping[str, Path],
) -> ValidationCorpus:
    """Load and validate a corpus without allowing paths outside named roots."""

    try:
        payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load validation corpus: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("validation corpus must be an object")
    if payload.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {CORPUS_SCHEMA_VERSION}")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise ValueError("documents must be a non-empty list")

    seen_ids: set[str] = set()
    documents = tuple(
        _load_record(raw, roots=roots, seen_ids=seen_ids) for raw in raw_documents
    )
    return ValidationCorpus(
        schema_version=CORPUS_SCHEMA_VERSION,
        documents=documents,
    )
