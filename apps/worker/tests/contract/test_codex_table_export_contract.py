from __future__ import annotations

import csv
import json
from pathlib import Path

from app.services.codex_export.schema import (
    BLOCK_SCHEMA_VERSION,
    DocumentBlock,
    canonical_sha256,
)
from app.services.codex_export.table_exporter import export_tables


def _table_block(
    html: str,
    *,
    block_id: str = "blk_table001",
    caption: str = "Table 1 — Synthetic limits",
    footnote: str = "Verify decision-relevant values against the native page.",
    image_path: str | None = None,
) -> DocumentBlock:
    content = {
        "table_caption": [{"type": "text", "content": caption}],
        "table_footnote": [{"type": "text", "content": footnote}],
        "html": html,
        "table_type": "simple_table",
        "table_nest_level": 1,
    }
    asset = {
        "asset_type": "table",
        "asset_id": "tbl_synthetic",
        "relative_path": f"tables/T-{block_id}.html",
    }
    if image_path:
        content["image_source"] = {"path": image_path}
        asset["source_relative_path"] = image_path
    return DocumentBlock(
        schema_version=BLOCK_SCHEMA_VERSION,
        document_id="doc_tables",
        block_id=block_id,
        sequence=3,
        block_type="table",
        text=caption,
        structured_content=content,
        content_sha256=canonical_sha256(content),
        section={"node_id": "sec_root", "path": [], "heading_level": 0},
        source_locator={
            "kind": "pdf_page",
            "page_index": 1,
            "page_number": 2,
            "block_index": 0,
        },
        assets=[asset],
        provenance={
            "parser": "MinerU",
            "source_artifact": "content_list_v2",
            "derivation": "parser_extracted",
            "evidence_use": "native_verification_required",
            "native_verification_required": True,
        },
        flags=[],
    )


def _read_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def test_simple_table_exports_html_and_one_headerless_csv(tmp_path: Path) -> None:
    block = _table_block(
        "<table><tr><td>Metric</td><td>Value</td></tr>"
        "<tr><td>Leakage</td><td>≤ 10 µA</td></tr></table>"
    )

    results = export_tables(blocks=[block], package_root=tmp_path)

    result = results[0]
    assert result.csv_fidelity == "best_effort_simple"
    assert result.html_path.read_text(encoding="utf-8").startswith("<table>")
    assert len(result.csv_paths) == 1
    assert _read_csv(result.csv_paths[0]) == [
        ["Metric", "Value"],
        ["Leakage", "≤ 10 µA"],
    ]
    assert all(not row[0].isdigit() for row in _read_csv(result.csv_paths[0]))


def test_rowspan_table_is_marked_lossy_and_emits_warning(tmp_path: Path) -> None:
    block = _table_block(
        "<table><tr><td rowspan='2'>A</td><td>1</td></tr>"
        "<tr><td>2</td></tr></table>"
    )

    result = export_tables(blocks=[block], package_root=tmp_path)[0]

    assert result.csv_fidelity == "lossy_complex"
    assert any("rowspan" in warning for warning in result.warnings)
    assert result.findings[0].category == "table_conversion"
    assert result.findings[0].native_verification_required is True


def test_colspan_table_is_marked_lossy(tmp_path: Path) -> None:
    block = _table_block(
        "<table><tr><td colspan='2'>Heading</td></tr>"
        "<tr><td>A</td><td>B</td></tr></table>"
    )

    result = export_tables(blocks=[block], package_root=tmp_path)[0]

    assert result.csv_fidelity == "lossy_complex"
    assert any("colspan" in warning for warning in result.warnings)


def test_nested_table_creates_numbered_part_csv_files(tmp_path: Path) -> None:
    block = _table_block(
        "<table><tr><td>Outer</td><td>"
        "<table><tr><td>Inner</td></tr></table>"
        "</td></tr></table>"
    )

    result = export_tables(blocks=[block], package_root=tmp_path)[0]

    assert result.csv_fidelity == "lossy_complex"
    assert len(result.csv_paths) >= 2
    assert [path.name for path in result.csv_paths] == [
        f"T-{block.block_id}-part-{index:02d}.csv"
        for index in range(1, len(result.csv_paths) + 1)
    ]


def test_multiple_top_level_tables_create_numbered_parts(tmp_path: Path) -> None:
    block = _table_block(
        "<table><tr><td>First</td></tr></table>"
        "<table><tr><td>Second</td></tr></table>"
    )

    result = export_tables(blocks=[block], package_root=tmp_path)[0]

    assert result.csv_fidelity == "lossy_complex"
    assert len(result.csv_paths) == 2
    assert any("multiple" in warning for warning in result.warnings)


def test_invalid_html_preserves_html_and_reports_no_csv(tmp_path: Path) -> None:
    invalid_html = "<div>This is not a table</div>"
    block = _table_block(invalid_html)

    result = export_tables(blocks=[block], package_root=tmp_path)[0]

    assert result.html_path.read_text(encoding="utf-8") == invalid_html
    assert result.csv_fidelity == "not_generated"
    assert result.csv_paths == ()
    assert result.findings


def test_caption_footnote_and_fidelity_are_retained_in_metadata(tmp_path: Path) -> None:
    block = _table_block("<table><tr><td>A</td></tr></table>")

    result = export_tables(blocks=[block], package_root=tmp_path)[0]
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))

    assert metadata["caption"] == "Table 1 — Synthetic limits"
    assert metadata["footnote"] == (
        "Verify decision-relevant values against the native page."
    )
    assert metadata["csv_fidelity"] == "best_effort_simple"
    assert metadata["native_verification_required"] is True
    assert "equivalent" not in json.dumps(metadata).lower()


def test_csv_paths_are_deterministic_and_formula_unicode_are_preserved(
    tmp_path: Path,
) -> None:
    html = (
        "<table><tr><td>Formula</td><td>Symbol</td></tr>"
        "<tr><td>=SUM(A1:A2)</td><td>± 0.5 °C · 10⁻⁶</td></tr></table>"
    )
    first = export_tables(
        blocks=[_table_block(html)], package_root=tmp_path / "first"
    )[0]
    second = export_tables(
        blocks=[_table_block(html)], package_root=tmp_path / "second"
    )[0]

    assert first.csv_paths[0].name == second.csv_paths[0].name
    first_csv = first.csv_paths[0].read_text(encoding="utf-8-sig")
    second_csv = second.csv_paths[0].read_text(encoding="utf-8-sig")
    assert first_csv == second_csv
    assert "=SUM(A1:A2)" in first_csv
    assert "± 0.5 °C · 10⁻⁶" in first_csv


def test_existing_mineru_table_image_is_copied_and_linked(tmp_path: Path) -> None:
    source_image = tmp_path / "raw" / "mineru" / "images" / "table-1.png"
    source_image.parent.mkdir(parents=True)
    source_image.write_bytes(b"synthetic-png")
    block = _table_block(
        "<table><tr><td>A</td></tr></table>",
        image_path="images/table-1.png",
    )

    result = export_tables(blocks=[block], package_root=tmp_path)[0]

    assert result.image_path is not None
    assert result.image_path.read_bytes() == b"synthetic-png"
    linked_paths = {asset["relative_path"] for asset in block.assets}
    assert result.html_path.relative_to(tmp_path).as_posix() in linked_paths
    assert result.metadata_path.relative_to(tmp_path).as_posix() in linked_paths
    assert result.image_path.relative_to(tmp_path).as_posix() in linked_paths

