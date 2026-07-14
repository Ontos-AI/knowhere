"""Normalize MinerU content_list_v2 pages into deterministic block records."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from app.services.codex_export.schema import (
    BLOCK_SCHEMA_VERSION,
    DocumentBlock,
    ExtractionFinding,
    canonical_sha256,
    deterministic_id,
)
from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactBundle,
)


_TYPE_MAP = {
    "title": "title",
    "paragraph": "paragraph",
    "list": "list",
    "index": "index",
    "equation_interline": "interline_equation",
    "interline_equation": "interline_equation",
    "image": "image",
    "table": "table",
    "chart": "chart",
    "code": "code",
    "algorithm": "algorithm",
    "page_header": "page_header",
    "page_footer": "page_footer",
    "page_number": "page_number",
    "page_footnote": "page_footnote",
    "page_aside_text": "aside_text",
    "aside_text": "aside_text",
}
_SECTION_PERIPHERAL_TYPES = {"page_header", "page_footer", "page_number"}
_TEXT_KEYS = (
    "title_content",
    "paragraph_content",
    "page_header_content",
    "page_footer_content",
    "page_number_content",
    "page_footnote_content",
    "page_aside_text_content",
    "math_content",
    "code_caption",
    "code_content",
    "code_footnote",
    "algorithm_caption",
    "algorithm_content",
    "algorithm_footnote",
    "table_caption",
    "html",
    "table_footnote",
    "image_caption",
    "content",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
    "list_items",
    "item_content",
    "value",
    "children",
)
_NON_TEXT_KEYS = {
    "type",
    "level",
    "image_source",
    "math_type",
    "code_language",
    "list_type",
    "attribute",
    "table_type",
    "table_nest_level",
}


def _html_text(value: str) -> str:
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _flatten_value(value: Any, *, key: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _html_text(value) if key == "html" else value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(_flatten_value(item) for item in value)
    if not isinstance(value, dict):
        return str(value)

    parts = [
        _flatten_value(value[name], key=name)
        for name in _TEXT_KEYS
        if name in value
    ]
    if not parts:
        parts = [
            _flatten_value(value[name], key=name)
            for name in sorted(value)
            if name not in _NON_TEXT_KEYS
        ]
    return " ".join(part for part in parts if part)


def _flatten_text(content: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", _flatten_value(content)).strip()


def _section_record(stack: list[dict[str, Any]]) -> dict[str, Any]:
    if not stack:
        return {"node_id": "sec_root", "path": [], "heading_level": 0}
    active = stack[-1]
    return {
        "node_id": active["node_id"],
        "path": [item["title"] for item in stack],
        "heading_level": active["level"],
    }


def _source_locator(
    *,
    suffix: str,
    page_index: int,
    block_index: int,
    raw_block: dict[str, Any],
) -> dict[str, Any]:
    locator: dict[str, Any] = {
        "kind": "office_logical_page" if suffix == ".docx" else "pdf_page",
        "page_index": page_index,
        "page_number": page_index + 1,
        "block_index": block_index,
        "bbox_normalized_1000": None,
        "anchor": raw_block.get("anchor"),
    }
    bbox = raw_block.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        locator["bbox_normalized_1000"] = copy.deepcopy(bbox)
    if suffix == ".docx":
        locator["normalized_pdf_page_number"] = None
        locator["normalized_pdf_mapping_status"] = "unmapped"
    return locator


def _assets_for_block(
    *,
    block_type: str,
    block_id: str,
    content: dict[str, Any],
) -> list[dict[str, Any]]:
    image_source = content.get("image_source")
    source_path = image_source.get("path") if isinstance(image_source, dict) else None
    if block_type == "table":
        asset = {
            "asset_type": "table",
            "asset_id": deterministic_id("tbl", block_id),
            "relative_path": f"tables/T-{block_id}.html",
        }
        if isinstance(source_path, str) and source_path:
            asset["source_relative_path"] = source_path
        return [asset]
    if block_type in {"image", "chart"}:
        prefix = "img" if block_type == "image" else "cht"
        asset = {
            "asset_type": block_type,
            "asset_id": deterministic_id(prefix, block_id),
            "relative_path": source_path,
        }
        if isinstance(source_path, str) and source_path:
            asset["source_relative_path"] = source_path
        return [asset]
    return []


def _provenance(block_type: str, content: dict[str, Any]) -> dict[str, Any]:
    derivation = "parser_extracted"
    evidence_use = "source_derivative"
    native_verification_required = False
    if block_type == "table":
        evidence_use = "native_verification_required"
        native_verification_required = True
    elif block_type in {"image", "chart"} and bool(content.get("content")):
        derivation = "machine_generated_visual_description"
        evidence_use = "navigation_only"
        native_verification_required = True
    return {
        "parser": "MinerU",
        "source_artifact": "content_list_v2",
        "derivation": derivation,
        "evidence_use": evidence_use,
        "native_verification_required": native_verification_required,
        "extraction_method": "unknown",
    }


def _unknown_finding(
    *,
    document_id: str,
    block_id: str,
    page_number: int,
    raw_type: str,
) -> ExtractionFinding:
    message = f"Unsupported MinerU block type preserved as unknown: {raw_type}"
    return ExtractionFinding(
        finding_id=deterministic_id(
            "ext", document_id, block_id, "unsupported_block_type", raw_type
        ),
        severity="warning",
        category="unsupported_block_type",
        message=message,
        document_id=document_id,
        block_id=block_id,
        page_number=page_number,
        native_verification_required=True,
    )


def _read_pages(path: Path) -> list[list[Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("content_list_v2 must be valid JSON") from error
    if not isinstance(payload, list) or any(not isinstance(page, list) for page in payload):
        raise ValueError("content_list_v2 must contain one array per logical page")
    return payload


def normalize_content_list_v2(
    *,
    artifact_bundle: MinerUArtifactBundle,
    document_id: str,
) -> list[DocumentBlock]:
    """Normalize MinerU v2 pages without inventing or summarizing content."""
    pages = _read_pages(artifact_bundle.content_list_v2_path)
    source = artifact_bundle.manifest.source
    source_hash = str(source.get("sha256") or "")
    suffix = str(source.get("suffix") or "").lower()
    heading_stack: list[dict[str, Any]] = []
    blocks: list[DocumentBlock] = []
    sequence = 0

    for page_index, page in enumerate(pages):
        for block_index, raw_value in enumerate(page):
            raw_block = raw_value if isinstance(raw_value, dict) else {"value": raw_value}
            raw_type = str(raw_block.get("type") or "unknown")
            block_type = _TYPE_MAP.get(raw_type, "unknown")
            content_value = raw_block.get("content", {})
            content = (
                copy.deepcopy(content_value)
                if isinstance(content_value, dict)
                else {"value": copy.deepcopy(content_value)}
            )
            text = _flatten_text(content)
            block_id = deterministic_id(
                "blk", source_hash, page_index, block_index, block_type
            )
            flags: list[str] = []
            findings: list[ExtractionFinding] = []

            if block_type == "title":
                level_value = content.get("level", 1)
                level = level_value if isinstance(level_value, int) and level_value > 0 else 1
                while heading_stack and heading_stack[-1]["level"] >= level:
                    heading_stack.pop()
                title_path = [item["title"] for item in heading_stack] + [text]
                heading_stack.append(
                    {
                        "title": text,
                        "level": level,
                        "node_id": deterministic_id(
                            "sec", document_id, *title_path, block_id
                        ),
                    }
                )

            if block_type in _SECTION_PERIPHERAL_TYPES:
                section = _section_record([])
                flags.append("excluded_from_section_body")
            else:
                section = _section_record(heading_stack)

            if block_type == "unknown":
                flags.append(f"unsupported_block_type:{raw_type}")
                findings.append(
                    _unknown_finding(
                        document_id=document_id,
                        block_id=block_id,
                        page_number=page_index + 1,
                        raw_type=raw_type,
                    )
                )

            block = DocumentBlock(
                schema_version=BLOCK_SCHEMA_VERSION,
                document_id=document_id,
                block_id=block_id,
                sequence=sequence,
                block_type=block_type,
                text=text,
                structured_content=content,
                content_sha256=canonical_sha256(
                    {"text": text, "structured_content": content}
                ),
                section=section,
                source_locator=_source_locator(
                    suffix=suffix,
                    page_index=page_index,
                    block_index=block_index,
                    raw_block=raw_block,
                ),
                assets=_assets_for_block(
                    block_type=block_type,
                    block_id=block_id,
                    content=content,
                ),
                provenance=_provenance(block_type, content),
                flags=flags,
                findings=findings,
            )
            blocks.append(block)
            sequence += 1
    return blocks

