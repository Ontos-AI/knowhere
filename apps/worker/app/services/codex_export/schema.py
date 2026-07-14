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


@dataclass
class DocumentTreeNode:
    node_id: str
    parent_node_id: str | None
    title: str | None
    level: int
    title_block_id: str | None
    start_sequence: int | None
    end_sequence: int | None
    start_page_number: int | None
    end_page_number: int | None
    child_node_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "parent_node_id": self.parent_node_id,
            "title": self.title,
            "level": self.level,
            "title_block_id": self.title_block_id,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "start_page_number": self.start_page_number,
            "end_page_number": self.end_page_number,
            "child_node_ids": self.child_node_ids,
        }


@dataclass
class DocumentTree:
    document_id: str
    nodes: list[DocumentTreeNode]
    findings: list[ExtractionFinding] = field(default_factory=list, repr=False)
    schema_version: str = "codex-document-tree/1.0"
    tree_origin: str = "mineru_title_levels"
    navigation_only: bool = True
    root_node_id: str = "sec_root"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "tree_origin": self.tree_origin,
            "navigation_only": self.navigation_only,
            "root_node_id": self.root_node_id,
            "nodes": [node.to_dict() for node in self.nodes],
        }
