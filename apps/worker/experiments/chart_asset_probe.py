"""Experimental: VLM-driven chart/table region detection + cropping.

Goal of this experiment
-----------------------
Test whether a VLM can directly locate table/chart bounding boxes on a
*rendered page image* (the same way PAGE-TRACK renders pages), so we can
**crop** those regions out and later hand the crop to a dedicated table
model (e.g. tabular / table-transformer) instead of asking the VLM to
transcribe the whole table verbatim (error-prone + expensive output).

This script does NOT touch production code.  It only:

  1. Renders selected PDF pages to PNG at a fixed DPI (mirrors PAGE-TRACK,
     default 144 DPI) so the pixel<->point mapping is fully controlled.
  2. (VLM) Asks the model for table/chart regions as normalized [0,1000]
     boxes, maps them to pixels, crops, and draws an annotated overlay.
  3. (Baseline) Runs PyMuPDF's free, pixel-accurate detectors
     (``find_tables`` + vector ``drawings`` rects + embedded ``images``)
     so VLM output can be judged against a non-LLM reference.

Outputs (under --out):
    pages/page-N.png                full page render
    crops/page-N_vlm-K_<type>.png   VLM crops
    crops/page-N_base-K_<kind>.png  baseline crops
    asset_annotate/page_N.png       page with VLM(red) + baseline(green) boxes
    results.json                    all regions + metadata
    report.md                       human-readable summary

Run:
    cd apps/worker
    uv run python experiments/chart_asset_probe.py \
        --pdf "/path/to/doc.pdf" \
        --pages all --baseline
    # Uses $IMAGE_MODEL (default qwen3.6-flash) unless --model is set.
    # Alternate: --model qwen3-vl-32b-instruct (open-weights, local-deployable).

VLM model guidance (chart/table bbox grounding)
------------------------------------------------
* **Default (cloud):** ``qwen3.6-flash`` via ``$IMAGE_MODEL`` — cheapest,
  strong bbox quality on our probe PDFs.
* **Alternate (cloud or self-hosted):** ``qwen3-vl-32b-instruct`` — open
  weights, can be deployed locally (vLLM / SGLang / Ollama); DashScope
  China pricing (2026-04): input ¥2 / output ¥8 per 1M tokens (see
  https://help.aliyun.com/zh/model-studio/model-pricing ).
* Coordinates are requested in a normalized 0-1000 space to be robust
  to whatever internal resize the API performs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont


# ── coordinate convention ─────────────────────────────────────────────
# We ask the VLM for boxes in a normalized integer space [0, 1000] for
# BOTH axes, with origin at the top-left of the page image. This is robust
# to whatever internal resize the API performs.
NORM = 1000

_PROMPT = (
    "You are a precise document layout detector. The attached image is a "
    "single rendered page.\n\n"
    "Task: locate every TABLE and every FIGURE (bar/line/pie charts, "
    "plots, diagrams, schematic images). Do NOT transcribe their content. "
    "Only return their bounding boxes.\n\n"
    "Coordinate system: treat the page image as a {n}x{n} grid. The "
    "top-left corner is (0,0) and the bottom-right is ({n},{n}). For each "
    "region return [x1,y1,x2,y2] integers in that 0-{n} space, where "
    "(x1,y1) is the top-left and (x2,y2) the bottom-right of a box that "
    "TIGHTLY encloses the region INCLUDING its title/caption and axis "
    "labels but EXCLUDING surrounding body paragraphs.\n\n"
    "Return STRICT JSON only, no prose:\n"
    '{{"regions":[{{"type":"table|chart|figure","bbox":[x1,y1,x2,y2],'
    '"caption":"short caption text if visible else empty",'
    '"confidence":0.0}}]}}\n'
    "If there are no tables/charts, return {{\"regions\":[]}}."
).format(n=NORM)


@dataclass
class Region:
    source: str  # "vlm" | "baseline"
    page: int
    kind: str  # table | chart | figure | drawing | image
    bbox_px: list[int]  # [x1,y1,x2,y2] in rendered-image pixels
    caption: str = ""
    confidence: float = 0.0
    crop_path: str = ""


@dataclass
class PageResult:
    page: int
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float
    image_path: str
    vlm_regions: list[Region] = field(default_factory=list)
    baseline_regions: list[Region] = field(default_factory=list)
    vlm_error: str = ""


# ── rendering ─────────────────────────────────────────────────────────


def render_page(page: fitz.Page, dpi: int, out_path: Path) -> tuple[int, int]:
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    pix.save(str(out_path))
    return pix.width, pix.height


# ── VLM call ──────────────────────────────────────────────────────────


def call_vlm(image_path: Path, model: str) -> dict[str, Any]:
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    content_parts = [
        {"type": "text", "text": _PROMPT},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        },
    ]
    # For this standalone experiment we pass a direct Qwen key when available.
    # The production Ali token pool depends on local Redis; direct mode keeps
    # the probe runnable on a laptop without changing production behavior.
    client = get_openai_client(model=model, api_key=_direct_api_key_for_model(model))
    raw, usage = client.chat_completion_with_usage(
        messages=[{"role": "user", "content": content_parts}],
        model=model,
        temperature=0.0,
        max_tokens=1200,
        response_format={"type": "json_object"},
        usage_task="experiment.chart_asset_probe",
    )
    data = json.loads(raw)
    data["_usage"] = usage
    return data


def _direct_api_key_for_model(model: str) -> str | None:
    model_lower = model.lower()
    if "qwen" not in model_lower:
        return None
    single = os.environ.get("ALI_API_KEY", "").strip()
    if single:
        return single
    keys = os.environ.get("ALI_API_KEYS", "").strip()
    if not keys:
        return None
    for item in re.split(r"[,;\s]+", keys):
        if item.strip():
            return item.strip()
    return None


def norm_to_px(box: list[float], w: int, h: int) -> list[int]:
    x1, y1, x2, y2 = box
    px = [
        int(round(x1 / NORM * w)),
        int(round(y1 / NORM * h)),
        int(round(x2 / NORM * w)),
        int(round(y2 / NORM * h)),
    ]
    # normalize ordering + clamp
    x1, x2 = sorted((px[0], px[2]))
    y1, y2 = sorted((px[1], px[3]))
    x1 = max(0, min(x1, w))
    x2 = max(0, min(x2, w))
    y1 = max(0, min(y1, h))
    y2 = max(0, min(y2, h))
    return [x1, y1, x2, y2]


# ── PyMuPDF baseline (free, pixel-accurate) ───────────────────────────


def baseline_regions(page: fitz.Page, dpi: int, w: int, h: int) -> list[Region]:
    zoom = dpi / 72.0
    regions: list[Region] = []

    # 1) Native table finder
    try:
        tf = page.find_tables()
        for t in tf.tables:
            r = t.bbox  # (x0,y0,x1,y1) in points
            regions.append(
                Region(
                    source="baseline",
                    page=page.number + 1,
                    kind="table",
                    bbox_px=_pt_box_to_px(r, zoom, w, h),
                    confidence=1.0,
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [baseline] find_tables failed: {exc}", file=sys.stderr)

    # 2) Embedded raster images (charts often exported as images)
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                regions.append(
                    Region(
                        source="baseline",
                        page=page.number + 1,
                        kind="image",
                        bbox_px=_pt_box_to_px(bbox, zoom, w, h),
                        confidence=0.5,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        print(f"  [baseline] get_image_info failed: {exc}", file=sys.stderr)

    return regions


def _pt_box_to_px(box: Any, zoom: float, w: int, h: int) -> list[int]:
    x1, y1, x2, y2 = box
    px = [int(round(x1 * zoom)), int(round(y1 * zoom)),
          int(round(x2 * zoom)), int(round(y2 * zoom))]
    px[0] = max(0, min(px[0], w))
    px[2] = max(0, min(px[2], w))
    px[1] = max(0, min(px[1], h))
    px[3] = max(0, min(px[3], h))
    return px


# ── cropping + overlay ────────────────────────────────────────────────


def crop_region(img: Image.Image, region: Region, margin: int, out_path: Path) -> None:
    x1, y1, x2, y2 = region.bbox_px
    x1 = max(0, x1 - margin)
    y1 = max(0, y1 - margin)
    x2 = min(img.width, x2 + margin)
    y2 = min(img.height, y2 + margin)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return
    img.crop((x1, y1, x2, y2)).save(out_path)
    region.crop_path = str(out_path)


def draw_overlay(img: Image.Image, page_res: PageResult, out_path: Path) -> None:
    canvas = img.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()

    for r in page_res.baseline_regions:
        x1, y1, x2, y2 = r.bbox_px
        draw.rectangle([x1, y1, x2, y2], outline=(0, 170, 0), width=3)
        draw.text((x1 + 2, y1 + 2), f"base:{r.kind}", fill=(0, 120, 0), font=font)

    for i, r in enumerate(page_res.vlm_regions):
        x1, y1, x2, y2 = r.bbox_px
        draw.rectangle([x1, y1, x2, y2], outline=(220, 0, 0), width=3)
        label = f"vlm:{r.kind} {r.confidence:.2f}"
        draw.text((x1 + 2, max(0, y1 - 24)), label, fill=(200, 0, 0), font=font)

    canvas.save(out_path)


# ── page range parsing ────────────────────────────────────────────────


def parse_pages(spec: str, total: int) -> list[int]:
    if spec.strip().lower() == "all":
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="all", help="e.g. 'all', '1-10', '1,3,5'")
    ap.add_argument("--dpi", type=int, default=144, help="render DPI (PAGE-TRACK uses 144)")
    ap.add_argument(
        "--model",
        default=os.environ.get("IMAGE_MODEL", ""),
        help=(
            "VLM model for bbox detection. Defaults to $IMAGE_MODEL "
            "(qwen3.6-flash). Alternate: qwen3-vl-32b-instruct "
            "(open-weights, local-deployable; DashScope ¥2/¥8 per 1M in/out)."
        ),
    )
    ap.add_argument("--margin", type=int, default=8, help="crop padding px")
    ap.add_argument("--baseline", action="store_true", help="also run PyMuPDF baseline")
    ap.add_argument("--no-vlm", action="store_true", help="skip VLM (baseline only)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    if not args.no_vlm and not args.model:
        print("No --model and IMAGE_MODEL unset; pass --model or use --no-vlm",
              file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else (
        Path.home() / ".knowhere" / "_debug_parse" / pdf_path.stem / "asset_probe"
    )
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)
    (out_dir / "crops").mkdir(parents=True, exist_ok=True)
    (out_dir / "asset_annotate").mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    page_nums = parse_pages(args.pages, doc.page_count)
    print(f"PDF: {pdf_path.name} | {doc.page_count} pages | probing {len(page_nums)} "
          f"| dpi={args.dpi} | model={args.model or '(none)'} | baseline={args.baseline}")

    results: list[PageResult] = []
    total_tokens = 0

    for pno in page_nums:
        page = doc[pno - 1]
        img_path = out_dir / "pages" / f"page-{pno}.png"
        w, h = render_page(page, args.dpi, img_path)
        pr = PageResult(
            page=pno, width_px=w, height_px=h,
            width_pt=page.rect.width, height_pt=page.rect.height,
            image_path=str(img_path),
        )
        print(f"\n[page {pno}] {w}x{h}px")

        if not args.no_vlm:
            try:
                data = call_vlm(img_path, args.model)
                usage = data.pop("_usage", {})
                total_tokens += int(usage.get("total_tokens", 0) or 0)
                for k, reg in enumerate(data.get("regions", [])):
                    box = reg.get("bbox") or reg.get("bbox_norm")
                    if not box or len(box) != 4:
                        continue
                    region = Region(
                        source="vlm", page=pno,
                        kind=str(reg.get("type", "region")),
                        bbox_px=norm_to_px([float(v) for v in box], w, h),
                        caption=str(reg.get("caption", "")),
                        confidence=float(reg.get("confidence", 0.0) or 0.0),
                    )
                    pr.vlm_regions.append(region)
                print(f"  [vlm] {len(pr.vlm_regions)} region(s); "
                      f"tokens={usage.get('total_tokens', '?')}")
            except Exception as exc:  # noqa: BLE001
                pr.vlm_error = str(exc)
                print(f"  [vlm] ERROR: {exc}", file=sys.stderr)

        if args.baseline:
            pr.baseline_regions = baseline_regions(page, args.dpi, w, h)
            print(f"  [baseline] {len(pr.baseline_regions)} region(s)")

        # crops + overlay
        with Image.open(img_path) as im:
            for k, r in enumerate(pr.vlm_regions):
                crop_region(im, r, args.margin,
                            out_dir / "crops" / f"page-{pno}_vlm-{k}_{r.kind}.png")
            for k, r in enumerate(pr.baseline_regions):
                crop_region(im, r, args.margin,
                            out_dir / "crops" / f"page-{pno}_base-{k}_{r.kind}.png")
            if pr.vlm_regions or pr.baseline_regions:
                draw_overlay(im, pr, out_dir / "asset_annotate" / f"page_{pno}.png")
        results.append(pr)

    doc.close()

    # results.json
    (out_dir / "results.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # report.md
    _write_report(out_dir, pdf_path, args, results, total_tokens)
    print(f"\nDone. Output: {out_dir}")
    print(f"  - annotated overlays: {out_dir/'asset_annotate'}")
    print(f"  - crops:              {out_dir/'crops'}")
    print("  - results.json / report.md")
    return 0


def _write_report(out_dir: Path, pdf_path: Path, args: Any,
                  results: list[PageResult], total_tokens: int) -> None:
    n_vlm = sum(len(r.vlm_regions) for r in results)
    n_base = sum(len(r.baseline_regions) for r in results)
    lines = [
        f"# Chart/Table Asset Probe — {pdf_path.name}",
        "",
        f"- pages probed: **{len(results)}**",
        f"- dpi: **{args.dpi}**, model: **{args.model or '(no-vlm)'}**",
        f"- VLM regions: **{n_vlm}**, baseline regions: **{n_base}**",
        f"- total VLM tokens: **{total_tokens}**",
        "",
        "| page | px | vlm | base | vlm kinds | vlm error |",
        "|-----:|----|----:|-----:|-----------|-----------|",
    ]
    for r in results:
        kinds = ",".join(sorted({x.kind for x in r.vlm_regions})) or "-"
        err = (r.vlm_error[:40] + "…") if r.vlm_error else ""
        lines.append(
            f"| {r.page} | {r.width_px}x{r.height_px} | {len(r.vlm_regions)} "
            f"| {len(r.baseline_regions)} | {kinds} | {err} |"
        )
    lines += [
        "",
        "## How to read",
        "- `asset_annotate/page_N.png`: red = VLM boxes, green = PyMuPDF baseline.",
        "- Judge VLM by: does the red box tightly enclose the table/chart "
        "(incl. caption, excl. body text)? Compare against green baseline.",
        "- `crops/`: the actual extracted assets to feed a table model next.",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
