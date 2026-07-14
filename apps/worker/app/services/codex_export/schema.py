"""Portable data contracts for Codex review package derivatives."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


BLOCK_SCHEMA_VERSION = "codex-document-block/1.0"


def deterministic_id(prefix: str, *parts: object, length: int = 20) -> str:
    encoded = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:length]}"


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExtractionFinding:
    finding_id: str
    severity: str
    category: str
    message: str
    document_id: str
    block_id: str | None
    page_number: int | None
    native_verification_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "document_id": self.document_id,
            "block_id": self.block_id,
            "page_number": self.page_number,
            "native_verification_required": self.native_verification_required,
        }


@dataclass
class DocumentBlock:
    schema_version: str
    document_id: str
    block_id: str
    sequence: int
    block_type: str
    text: str
    structured_content: dict[str, Any]
    content_sha256: str
    section: dict[str, Any]
    source_locator: dict[str, Any]
    assets: list[dict[str, Any]]
    provenance: dict[str, Any]
    flags: list[str]
    findings: list[ExtractionFinding] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "block_id": self.block_id,
            "sequence": self.sequence,
            "block_type": self.block_type,
            "text": self.text,
            "structured_content": self.structured_content,
            "content_sha256": self.content_sha256,
            "section": self.section,
            "source_locator": self.source_locator,
            "assets": self.assets,
            "provenance": self.provenance,
            "flags": self.flags,
        }

