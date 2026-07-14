"""Export MinerU table HTML and explicitly best-effort CSV derivatives."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence

import pandas as pd
from bs4 import BeautifulSoup
from bs4.element import Tag

from app.services.codex_export.schema import (
    DocumentBlock,
    ExtractionFinding,
    deterministic_id,
)


@dataclass(frozen=True)
class TableExportResult:
    block_id: str
    table_id: str
    html_path: Path
    metadata_path: Path
    csv_paths: tuple[Path, ...]
    csv_fidelity: str
    image_path: Path | None
    warnings: tuple[str, ...] = ()
    findings: tuple[ExtractionFinding, ...] = field(default_factory=tuple)


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value).strip()
    if isinstance(value, dict):
        parts = []
        if "content" in value:
            parts.append(_flatten_text(value["content"]))
        if "children" in value:
            parts.append(_flatten_text(value["children"]))
        if parts:
            return " ".join(part for part in parts if part).strip()
        return " ".join(
            _flatten_text(value[key]) for key in sorted(value) if key != "type"
        ).strip()
    return str(value)


def _safe_source_image(package_root: Path, raw_path: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    portable = PurePosixPath(raw_path.replace("\\", "/"))
    if (
        portable.is_absolute()
        or PureWindowsPath(raw_path).is_absolute()
        or ".." in portable.parts
    ):
        return None
    raw_root = (package_root / "raw" / "mineru").resolve()
    candidate = (raw_root / Path(*portable.parts)).resolve()
    try:
        candidate.relative_to(raw_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _table_id(block: DocumentBlock) -> str:
    for asset in block.assets:
        if asset.get("asset_type") == "table" and asset.get("asset_id"):
            return str(asset["asset_id"])
    return deterministic_id("tbl", block.block_id)


def _write_csv(
    frame: pd.DataFrame,
    path: Path,
    *,
    explicit_header: bool,
) -> None:
    frame.to_csv(
        path,
        index=False,
        header=explicit_header,
        encoding="utf-8-sig",
        lineterminator="\n",
    )


def _table_finding(
    *,
    block: DocumentBlock,
    message: str,
) -> ExtractionFinding:
    page = block.source_locator.get("page_number")
    page_number = page if isinstance(page, int) else None
    return ExtractionFinding(
        finding_id=deterministic_id(
            "ext", block.document_id, block.block_id, "table_conversion", message
        ),
        severity="warning",
        category="table_conversion",
        message=message,
        document_id=block.document_id,
        block_id=block.block_id,
        page_number=page_number,
        native_verification_required=True,
    )


def _link_asset(
    block: DocumentBlock,
    *,
    asset_type: str,
    asset_id: str,
    relative_path: str,
) -> None:
    for asset in block.assets:
        if asset.get("asset_type") == asset_type and asset.get(
            "relative_path"
        ) == relative_path:
            return
    block.assets.append(
        {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "relative_path": relative_path,
        }
    )


def _export_table(block: DocumentBlock, package_root: Path) -> TableExportResult:
    tables_dir = package_root / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    table_id = _table_id(block)
    stem = f"T-{block.block_id}"
    html_path = tables_dir / f"{stem}.html"
    metadata_path = tables_dir / f"{stem}.metadata.json"
    html_value = block.structured_content.get("html", "")
    html = html_value if isinstance(html_value, str) else str(html_value)
    html_path.write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "html.parser")
    table_tags = [tag for tag in soup.find_all("table") if isinstance(tag, Tag)]
    top_level_tables = [tag for tag in table_tags if tag.find_parent("table") is None]
    has_rowspan = bool(soup.find_all(attrs={"rowspan": True}))
    has_colspan = bool(soup.find_all(attrs={"colspan": True}))
    has_nested_table = len(table_tags) > len(top_level_tables)
    warnings: list[str] = []
    if has_rowspan:
        warnings.append("rowspan detected; CSV cell expansion may be lossy")
    if has_colspan:
        warnings.append("colspan detected; CSV cell expansion may be lossy")
    if has_nested_table:
        warnings.append("nested table detected; CSV parts require native verification")
    if len(top_level_tables) > 1:
        warnings.append("multiple tables detected; CSV was split into numbered parts")

    frames: list[pd.DataFrame] = []
    if not table_tags:
        warnings.append("no parseable table element was found")
    else:
        try:
            frames = pd.read_html(StringIO(html), header=None)
        except (ImportError, ValueError, TypeError) as error:
            warnings.append(f"table parser failed: {type(error).__name__}")

    nonempty_frames = [
        (index, frame)
        for index, frame in enumerate(frames)
        if not frame.empty and len(frame.columns) > 0
    ]
    if frames and len(nonempty_frames) != len(frames):
        warnings.append("empty parsed table part was not exported")

    csv_paths: list[Path] = []
    numbered_parts = (
        len(nonempty_frames) > 1
        or has_nested_table
        or len(top_level_tables) > 1
    )
    explicit_headers = [tag.find("thead") is not None for tag in table_tags]
    for part_number, (source_index, frame) in enumerate(nonempty_frames, start=1):
        csv_name = (
            f"{stem}-part-{part_number:02d}.csv"
            if numbered_parts
            else f"{stem}.csv"
        )
        csv_path = tables_dir / csv_name
        explicit_header = (
            explicit_headers[source_index]
            if source_index < len(explicit_headers)
            else False
        )
        _write_csv(frame, csv_path, explicit_header=explicit_header)
        csv_paths.append(csv_path)

    complex_conversion = bool(
        has_rowspan
        or has_colspan
        or has_nested_table
        or len(top_level_tables) > 1
        or len(frames) > 1
    )
    if not csv_paths:
        csv_fidelity = "not_generated"
    elif complex_conversion:
        csv_fidelity = "lossy_complex"
    else:
        csv_fidelity = "best_effort_simple"

    image_path = None
    image_source = block.structured_content.get("image_source")
    raw_image_path = image_source.get("path") if isinstance(image_source, dict) else None
    source_image = _safe_source_image(package_root, raw_image_path)
    if source_image is not None:
        suffix = source_image.suffix.lower() or ".png"
        image_path = tables_dir / f"{stem}{suffix}"
        shutil.copy2(source_image, image_path)

    findings: list[ExtractionFinding] = []
    if csv_fidelity != "best_effort_simple":
        if csv_fidelity == "lossy_complex":
            message = (
                "CSV was generated from a structurally complex table and may be "
                "lossy; verify decision-relevant values on the native page."
            )
        else:
            message = (
                "CSV could not be generated; use the preserved HTML and native "
                "page for verification."
            )
        findings.append(_table_finding(block=block, message=message))

    caption = _flatten_text(block.structured_content.get("table_caption"))
    footnote = _flatten_text(block.structured_content.get("table_footnote"))
    metadata = {
        "schema_version": "codex-table-derivative/1.0",
        "document_id": block.document_id,
        "block_id": block.block_id,
        "table_id": table_id,
        "page_number": block.source_locator.get("page_number"),
        "caption": caption,
        "footnote": footnote,
        "html_path": html_path.relative_to(package_root).as_posix(),
        "csv_paths": [path.relative_to(package_root).as_posix() for path in csv_paths],
        "csv_fidelity": csv_fidelity,
        "native_verification_required": True,
        "warnings": warnings,
    }
    if image_path is not None:
        metadata["image_path"] = image_path.relative_to(package_root).as_posix()
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    html_relative = html_path.relative_to(package_root).as_posix()
    table_asset = next(
        (asset for asset in block.assets if asset.get("asset_type") == "table"),
        None,
    )
    if table_asset is None:
        _link_asset(
            block,
            asset_type="table",
            asset_id=table_id,
            relative_path=html_relative,
        )
    else:
        table_asset["relative_path"] = html_relative
    _link_asset(
        block,
        asset_type="table_metadata",
        asset_id=f"{table_id}_metadata",
        relative_path=metadata_path.relative_to(package_root).as_posix(),
    )
    for index, csv_path in enumerate(csv_paths, start=1):
        _link_asset(
            block,
            asset_type="table_csv",
            asset_id=f"{table_id}_csv_{index:02d}",
            relative_path=csv_path.relative_to(package_root).as_posix(),
        )
    if image_path is not None:
        _link_asset(
            block,
            asset_type="table_image",
            asset_id=f"{table_id}_image",
            relative_path=image_path.relative_to(package_root).as_posix(),
        )

    return TableExportResult(
        block_id=block.block_id,
        table_id=table_id,
        html_path=html_path,
        metadata_path=metadata_path,
        csv_paths=tuple(csv_paths),
        csv_fidelity=csv_fidelity,
        image_path=image_path,
        warnings=tuple(warnings),
        findings=tuple(findings),
    )


def export_tables(
    *,
    blocks: Sequence[DocumentBlock],
    package_root: Path,
) -> list[TableExportResult]:
    """Export each normalized table while preserving its original HTML."""
    root = package_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return [
        _export_table(block, root)
        for block in blocks
        if block.block_type == "table"
    ]
