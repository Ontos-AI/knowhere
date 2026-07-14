from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.codex_export.block_normalizer import normalize_content_list_v2
from app.services.codex_export.jsonl import write_blocks_jsonl
from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactBundle,
    MinerUArtifactManifest,
)


def _bundle(tmp_path: Path, pages: list[list[dict]], suffix: str) -> MinerUArtifactBundle:
    output_root = tmp_path / "mineru"
    output_root.mkdir()
    content_list_v2_path = output_root / "report_content_list_v2.json"
    content_list_v2_path.write_text(
        json.dumps(pages, ensure_ascii=False), encoding="utf-8"
    )
    placeholders = {}
    for name in ("report.md", "report_middle.json", "report_content_list.json"):
        path = output_root / name
        path.write_text("{}" if name.endswith(".json") else "# Report", encoding="utf-8")
        placeholders[name] = path
    images_dir = output_root / "images"
    images_dir.mkdir()
    source_hash = hashlib.sha256(b"synthetic source").hexdigest()
    manifest = MinerUArtifactManifest(
        schema_version="knowhere-mineru-artifacts/1.0",
        status="completed",
        source={
            "filename": f"report{suffix}",
            "suffix": suffix,
            "sha256": source_hash,
            "size_bytes": len(b"synthetic source"),
        },
        parser={"name": "MinerU", "backend_effective": "office" if suffix == ".docx" else "pipeline"},
        execution={"mode": "local-direct-python"},
        document={"logical_page_count": len(pages)},
        artifacts={},
        warnings=(),
        raw={},
    )
    return MinerUArtifactBundle(
        manifest_path=output_root / "mineru_manifest.json",
        output_root=output_root,
        markdown_path=placeholders["report.md"],
        middle_json_path=placeholders["report_middle.json"],
        content_list_path=placeholders["report_content_list.json"],
        content_list_v2_path=content_list_v2_path,
        images_dir=images_dir,
        manifest=manifest,
    )


def _pdf_pages() -> list[list[dict]]:
    return [
        [
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "1. Scope"}],
                    "level": 1,
                },
                "bbox": [80, 90, 900, 140],
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "Limit "},
                        {"type": "equation_inline", "content": "≤ 10⁻⁶ "},
                        {
                            "type": "hyperlink",
                            "content": "",
                            "children": [{"type": "text", "content": "µA"}],
                        },
                    ]
                },
            },
            {
                "type": "page_header",
                "content": {
                    "page_header_content": [
                        {"type": "text", "content": "Synthetic header"}
                    ]
                },
            },
        ],
        [],
        [
            {
                "type": "title",
                "content": {
                    "title_content": [
                        {"type": "text", "content": "1.1 Details"}
                    ],
                    "level": 2,
                },
            },
            {
                "type": "table",
                "content": {
                    "image_source": {"path": "images/table-1.png"},
                    "table_caption": [
                        {"type": "text", "content": "Table 1 — Limits"}
                    ],
                    "table_footnote": [
                        {"type": "text", "content": "Verify against source."}
                    ],
                    "html": "<table><tr><td>Temperature</td><td>25 °C</td></tr></table>",
                    "table_type": "simple_table",
                    "table_nest_level": 1,
                },
                "bbox": [100, 200, 900, 700],
            },
            {
                "type": "image",
                "content": {
                    "image_source": {"path": "images/figure-1.png"},
                    "image_caption": [
                        {"type": "text", "content": "Figure 1"}
                    ],
                    "content": "Machine description for navigation",
                },
            },
            {
                "type": "future_widget",
                "content": {"value": "preserve me"},
            },
            {
                "type": "page_footer",
                "content": {
                    "page_footer_content": [
                        {"type": "text", "content": "Page 3"}
                    ]
                },
            },
        ],
    ]


def test_block_ids_and_content_hashes_are_stable_across_runs(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, _pdf_pages(), ".pdf")

    first = normalize_content_list_v2(
        artifact_bundle=bundle, document_id="doc_synthetic"
    )
    second = normalize_content_list_v2(
        artifact_bundle=bundle, document_id="doc_synthetic"
    )

    assert [block.block_id for block in first] == [block.block_id for block in second]
    assert [block.content_sha256 for block in first] == [
        block.content_sha256 for block in second
    ]


def test_page_indexes_preserve_empty_pages_and_use_one_based_numbers(
    tmp_path: Path,
) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    table = next(block for block in blocks if block.block_type == "table")

    assert table.source_locator["kind"] == "pdf_page"
    assert table.source_locator["page_index"] == 2
    assert table.source_locator["page_number"] == 3
    assert table.source_locator["bbox_normalized_1000"] == [100, 200, 900, 700]


def test_title_stack_assigns_deterministic_section_paths(tmp_path: Path) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    paragraph = next(block for block in blocks if block.block_type == "paragraph")
    table = next(block for block in blocks if block.block_type == "table")

    assert paragraph.section["path"] == ["1. Scope"]
    assert paragraph.section["heading_level"] == 1
    assert table.section["path"] == ["1. Scope", "1.1 Details"]
    assert table.section["heading_level"] == 2


def test_rich_spans_flatten_for_search_without_losing_structure(
    tmp_path: Path,
) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    paragraph = next(block for block in blocks if block.block_type == "paragraph")

    assert paragraph.text == "Limit ≤ 10⁻⁶ µA"
    spans = paragraph.structured_content["paragraph_content"]
    assert spans[1] == {"type": "equation_inline", "content": "≤ 10⁻⁶ "}
    assert spans[2]["children"][0]["content"] == "µA"


def test_table_and_image_references_preserve_provenance(tmp_path: Path) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    table = next(block for block in blocks if block.block_type == "table")
    image = next(block for block in blocks if block.block_type == "image")

    assert table.assets[0]["asset_type"] == "table"
    assert table.assets[0]["source_relative_path"] == "images/table-1.png"
    assert table.provenance["native_verification_required"] is True
    assert "Table 1 — Limits" in table.text
    assert "25 °C" in table.text
    assert image.assets[0]["source_relative_path"] == "images/figure-1.png"
    assert image.provenance["derivation"] == "machine_generated_visual_description"
    assert image.provenance["evidence_use"] == "navigation_only"


def test_unknown_blocks_are_preserved_and_emit_warning_findings(
    tmp_path: Path,
) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    unknown = next(block for block in blocks if block.block_type == "unknown")

    assert unknown.structured_content == {"value": "preserve me"}
    assert "unsupported_block_type:future_widget" in unknown.flags
    assert len(unknown.findings) == 1
    assert unknown.findings[0].severity == "warning"
    assert unknown.findings[0].category == "unsupported_block_type"


def test_headers_and_footers_are_preserved_but_excluded_from_section_body(
    tmp_path: Path,
) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    peripheral = [
        block
        for block in blocks
        if block.block_type in {"page_header", "page_footer"}
    ]

    assert len(peripheral) == 2
    assert all("excluded_from_section_body" in block.flags for block in peripheral)
    assert all(block.section["node_id"] == "sec_root" for block in peripheral)


def test_docx_locator_keeps_logical_and_normalized_pages_separate(
    tmp_path: Path,
) -> None:
    pages = [
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "Office paragraph"}
                    ]
                },
                "anchor": "word/bookmark-1",
            }
        ]
    ]
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, pages, ".docx"),
        document_id="doc_office",
    )

    locator = blocks[0].source_locator
    assert locator["kind"] == "office_logical_page"
    assert locator["page_number"] == 1
    assert locator["anchor"] == "word/bookmark-1"
    assert locator["normalized_pdf_page_number"] is None
    assert locator["normalized_pdf_mapping_status"] == "unmapped"


def test_blocks_jsonl_is_utf8_and_round_trips_records(tmp_path: Path) -> None:
    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, _pdf_pages(), ".pdf"),
        document_id="doc_synthetic",
    )
    output_path = tmp_path / "structured" / "blocks.jsonl"

    write_blocks_jsonl(blocks, output_path)

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [block.to_dict() for block in blocks]
    assert any("µA" in record["text"] for record in records)


@pytest.mark.parametrize(
    ("raw_type", "normalized_type"),
    [
        ("list", "list"),
        ("index", "index"),
        ("equation_interline", "interline_equation"),
        ("chart", "chart"),
        ("code", "code"),
        ("algorithm", "algorithm"),
        ("page_number", "page_number"),
        ("page_footnote", "page_footnote"),
        ("page_aside_text", "aside_text"),
    ],
)
def test_supported_mineru_v2_types_are_not_dropped(
    tmp_path: Path,
    raw_type: str,
    normalized_type: str,
) -> None:
    pages = [[{"type": raw_type, "content": {"value": "preserved"}}]]

    blocks = normalize_content_list_v2(
        artifact_bundle=_bundle(tmp_path, pages, ".pdf"),
        document_id="doc_types",
    )

    assert blocks[0].block_type == normalized_type
    assert blocks[0].text == "preserved"
