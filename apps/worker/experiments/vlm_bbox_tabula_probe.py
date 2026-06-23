"""Experimental: VLM bbox + Tabula PDF table extraction.

This probe tests the next step after ``chart_asset_probe.py``:

1. Read only VLM-detected ``kind=table`` regions from ``results.json``.
2. Convert rendered-image pixel boxes back to PDF point coordinates.
3. Pass each area to Tabula (`tabula-py`) against the original PDF text layer.
4. Export candidate DataFrames as HTML/CSV for manual inspection.

Important: Tabula does not read PNG crops. It needs a text-based PDF, so the
VLM box is used only as a precise `area=[top,left,bottom,right]` constraint.

Run:
    cd apps/worker
    uv run --with tabula-py python experiments/vlm_bbox_tabula_probe.py \
      --pdf "/path/to/doc.pdf" \
      --results "/path/to/asset_probe/results.json"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class TabulaCandidate:
    page: int
    region_index: int
    mode: str
    caption: str
    bbox_px: list[int]
    area_pt: list[float]
    ok: bool
    rows: int = 0
    cols: int = 0
    html_path: str = ""
    csv_path: str = ""
    error: str = ""


def _px_box_to_tabula_area(
    bbox_px: list[int],
    *,
    width_px: int,
    height_px: int,
    width_pt: float,
    height_pt: float,
    margin_pt: float,
) -> list[float]:
    """Convert [x1,y1,x2,y2] px box into Tabula [top,left,bottom,right] points."""
    x1, y1, x2, y2 = bbox_px
    left = x1 / width_px * width_pt
    right = x2 / width_px * width_pt
    top = y1 / height_px * height_pt
    bottom = y2 / height_px * height_pt
    return [
        round(max(0.0, top - margin_pt), 2),
        round(max(0.0, left - margin_pt), 2),
        round(min(height_pt, bottom + margin_pt), 2),
        round(min(width_pt, right + margin_pt), 2),
    ]


def _load_vlm_table_regions(results_path: Path) -> list[dict[str, Any]]:
    pages = json.loads(results_path.read_text(encoding="utf-8"))
    table_regions: list[dict[str, Any]] = []
    for page in pages:
        for region_index, region in enumerate(page.get("vlm_regions", [])):
            if str(region.get("kind", "")).lower() != "table":
                continue
            table_regions.append({
                "page": int(page["page"]),
                "region_index": region_index,
                "caption": str(region.get("caption", "")),
                "bbox_px": list(region["bbox_px"]),
                "width_px": int(page["width_px"]),
                "height_px": int(page["height_px"]),
                "width_pt": float(page["width_pt"]),
                "height_pt": float(page["height_pt"]),
            })
    return table_regions


def _is_meaningful_frame(df: pd.DataFrame) -> bool:
    if df.empty or df.shape[1] == 0:
        return False
    non_empty = df.fillna("").astype(str).map(lambda s: bool(s.strip()))
    return bool(non_empty.values.sum() >= 2)


def _safe_name(page: int, region_index: int, mode: str, candidate_index: int) -> str:
    return f"page-{page}_vlm-{region_index}_{mode}-{candidate_index}"


def _has_working_java() -> bool:
    if shutil.which("java") is None:
        return False
    try:
        result = subprocess.run(
            ["java", "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    combined = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 0 and "Unable to locate a Java Runtime" not in combined


def _extract_one(
    *,
    tabula: Any,
    pdf_path: Path,
    out_dir: Path,
    region: dict[str, Any],
    mode: str,
    area_pt: list[float],
) -> list[TabulaCandidate]:
    lattice = mode == "lattice"
    stream = mode == "stream"
    try:
        frames = tabula.read_pdf(
            str(pdf_path),
            pages=region["page"],
            area=area_pt,
            guess=False,
            lattice=lattice,
            stream=stream,
            multiple_tables=True,
            pandas_options={"header": None},
        )
    except Exception as exc:  # noqa: BLE001
        return [
            TabulaCandidate(
                page=region["page"],
                region_index=region["region_index"],
                mode=mode,
                caption=region["caption"],
                bbox_px=region["bbox_px"],
                area_pt=area_pt,
                ok=False,
                error=str(exc),
            )
        ]

    candidates: list[TabulaCandidate] = []
    if not frames:
        return [
            TabulaCandidate(
                page=region["page"],
                region_index=region["region_index"],
                mode=mode,
                caption=region["caption"],
                bbox_px=region["bbox_px"],
                area_pt=area_pt,
                ok=False,
                error="tabula returned no tables",
            )
        ]

    for candidate_index, df in enumerate(frames):
        name = _safe_name(region["page"], region["region_index"], mode, candidate_index)
        candidate = TabulaCandidate(
            page=region["page"],
            region_index=region["region_index"],
            mode=mode,
            caption=region["caption"],
            bbox_px=region["bbox_px"],
            area_pt=area_pt,
            ok=_is_meaningful_frame(df),
            rows=int(df.shape[0]),
            cols=int(df.shape[1]),
        )
        if candidate.ok:
            html_path = out_dir / "html" / f"{name}.html"
            csv_path = out_dir / "csv" / f"{name}.csv"
            df.to_html(html_path, index=False, header=False, na_rep="")
            df.to_csv(csv_path, index=False, header=False)
            candidate.html_path = str(html_path)
            candidate.csv_path = str(csv_path)
        else:
            candidate.error = "empty or near-empty dataframe"
        candidates.append(candidate)
    return candidates


def _write_report(out_dir: Path, candidates: list[TabulaCandidate]) -> None:
    ok_count = sum(1 for item in candidates if item.ok)
    lines = [
        "# VLM BBox + Tabula Probe",
        "",
        f"- candidates: **{len(candidates)}**",
        f"- successful non-empty tables: **{ok_count}**",
        "",
        "| page | vlm | mode | ok | shape | caption | error |",
        "|-----:|----:|------|----|-------|---------|-------|",
    ]
    for item in candidates:
        error = item.error.replace("\n", " ")[:80]
        lines.append(
            f"| {item.page} | {item.region_index} | {item.mode} | "
            f"{'yes' if item.ok else 'no'} | {item.rows}x{item.cols} | "
            f"{item.caption} | {error} |"
        )
    lines.append("")
    lines.append("Only VLM `kind=table` regions were used; baseline regions were ignored.")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--margin-pt", type=float, default=2.0)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    results_path = Path(args.results)
    out_dir = (
        Path(args.out)
        if args.out
        else results_path.parent / "tabula_from_vlm_bbox"
    )
    (out_dir / "html").mkdir(parents=True, exist_ok=True)
    (out_dir / "csv").mkdir(parents=True, exist_ok=True)

    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    if not results_path.exists():
        raise FileNotFoundError(results_path)
    if not _has_working_java():
        raise RuntimeError(
            "Java runtime is required by tabula-py but was not found. "
            "Install a JRE (e.g. OpenJDK/Temurin) and rerun this probe."
        )

    try:
        import tabula
    except ImportError as exc:
        raise RuntimeError(
            "tabula-py is required. Run with: uv run --with tabula-py python ..."
        ) from exc

    regions = _load_vlm_table_regions(results_path)
    print(f"Loaded {len(regions)} VLM table regions from {results_path}")

    candidates: list[TabulaCandidate] = []
    for region in regions:
        area_pt = _px_box_to_tabula_area(
            region["bbox_px"],
            width_px=region["width_px"],
            height_px=region["height_px"],
            width_pt=region["width_pt"],
            height_pt=region["height_pt"],
            margin_pt=args.margin_pt,
        )
        for mode in ("lattice", "stream"):
            extracted = _extract_one(
                tabula=tabula,
                pdf_path=pdf_path,
                out_dir=out_dir,
                region=region,
                mode=mode,
                area_pt=area_pt,
            )
            candidates.extend(extracted)
            for item in extracted:
                print(
                    f"page={item.page} vlm={item.region_index} "
                    f"mode={item.mode} ok={item.ok} shape={item.rows}x{item.cols}"
                )

    (out_dir / "results.json").write_text(
        json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_report(out_dir, candidates)
    print(f"Done. Output: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
