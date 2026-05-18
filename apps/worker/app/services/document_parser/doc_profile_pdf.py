# pyright: reportAttributeAccessIssue=false, reportOperatorIssue=false
from __future__ import annotations

import math
from typing import Any

from app.services.document_parser.doc_profile_model import (
    DocProfile,
    publish_profile_result,
)
from app.services.document_parser.pymupdf_subprocess import run_in_child_process, worker
from loguru import logger

# Thresholds
SCAN_TEXT_THRESHOLD = 50
SCAN_IMAGE_COVERAGE_MIN = 0.6
SCAN_PAGE_RATIO = 0.7

ATLAS_TEXT_THRESHOLD = 200
ATLAS_CANDIDATE_IMAGE_COVERAGE_MIN = 0.30
ATLAS_MIN_LANDSCAPE_RATIO = 0.5  # ≥50% of sampled pages must be landscape
ATLAS_MIN_PAGES = 2  # single-page scans (resumes, posters) are not atlases

FAST_TEXT_THRESHOLD = 500
MIN_FAST_TEXT_DENSITY_FLOOR = 120
SAFE_FAST_MAX_PAGE_COUNT = 80
HARD_STANDARD_PAGE_COUNT = 150

MULTI_COL_GAP_RATIO = 0.15
MULTI_COL_MIN_BLOCKS = 4

DEGRADED_SKINNY_ASPECT = 50
DEGRADED_SKINNY_MAX_H = 30
DEGRADED_SKINNY_MIN_PER_PAGE = 50
DEGRADED_PAGE_RATIO = 0.5

SIGNIFICANT_IMAGE_AREA_RATIO = 0.12
MEDIUM_IMAGE_AREA_RATIO = 0.03
LARGE_IMAGE_PAGE_RATIO = 0.25
SIGNIFICANT_IMAGE_MIN_DIM = 400
SIGNIFICANT_IMAGE_MIN_PIXELS = 250_000

PROFILE_MAX_NEW_XREFS_PER_PAGE = 30

TABLE_DRAWING_LINE_THRESHOLD = 12
TABLE_DRAWING_STRONG_THRESHOLD = 18
TABLE_DRAWING_RECT_THRESHOLD = 2

SAFE_FAST_MAX_COMPLEX_PAGE_RATIO = 0.05
SAFE_FAST_MAX_IMAGE_COVERAGE_ON_PAGE = 0.08
SAFE_FAST_MAX_AVG_IMAGE_COVERAGE = 0.03
SAFE_FAST_MAX_TEXT_STD = 600.0
HARD_COMPLEX_PAGE_RATIO = 0.2
HARD_SIGNIFICANT_IMAGE_PAGES = 3
HARD_LARGE_IMAGE_PAGE_RATIO = 0.15


def _clamp(value: float, min_value: float = 0.0, max_value: float = 1.0) -> float:
    return max(min_value, min(max_value, value))


def _stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _count_detected_tables(page: Any) -> int:
    try:
        finder = page.find_tables()
    except Exception:
        return 0

    if not finder:
        return 0

    tables = getattr(finder, "tables", finder)
    try:
        return len(tables)
    except TypeError:
        return 1 if tables else 0


def _is_stroked_drawing(drawing: dict[str, Any]) -> bool:
    stroke_width = drawing.get("width")
    return drawing.get("color") is not None or (
        stroke_width is not None and stroke_width > 0
    )


def _estimate_fast_benefit(profile: DocProfile) -> float:
    if profile.page_count <= 2:
        page_factor = 0.35
    elif profile.page_count <= 10:
        page_factor = 0.7
    elif profile.page_count <= SAFE_FAST_MAX_PAGE_COUNT:
        page_factor = 1.0
    elif profile.page_count <= HARD_STANDARD_PAGE_COUNT:
        page_factor = 0.8
    else:
        page_factor = 0.45

    density_factor = _clamp(profile.avg_text_density / 1200.0)
    stability_factor = _clamp(
        1.0
        - (profile.complex_page_ratio * 1.5)
        - (profile.large_image_page_ratio * 1.2)
        - (profile.table_signal_strength * 0.8)
    )
    return _clamp(
        (0.35 * page_factor) + (0.40 * density_factor) + (0.25 * stability_factor)
    )


def _estimate_risk_score(profile: DocProfile) -> float:
    risk = 0.0
    if profile.scan_type != "electronic":
        risk += 0.35
    if profile.doc_category != "generic":
        risk += 0.20
    if profile.is_multi_column:
        risk += 0.20
    if profile.is_degraded_electronic:
        risk += 0.20
    if profile.has_tables:
        risk += 0.30

    risk += min(0.20, profile.large_image_page_ratio * 1.2)
    risk += min(0.20, profile.complex_page_ratio * 0.8)
    risk += min(0.15, profile.table_signal_strength * 0.2)
    risk += min(0.12, profile.pages_with_significant_images * 0.04)

    if profile.page_count > HARD_STANDARD_PAGE_COUNT:
        risk += 0.10

    return _clamp(risk)


def _classify_route(profile: DocProfile) -> tuple[str, str, float, float, list[str]]:
    hard_gate_reasons: list[str] = []

    if profile.scan_type != "electronic":
        hard_gate_reasons.append(f"scan_type={profile.scan_type}")
    if profile.doc_category != "generic":
        hard_gate_reasons.append(f"doc_category={profile.doc_category}")
    if profile.is_multi_column:
        hard_gate_reasons.append("multi_column")
    if profile.is_degraded_electronic:
        hard_gate_reasons.append("degraded_electronic")
    if profile.has_tables:
        hard_gate_reasons.append(
            f"table_signals={profile.table_signal_pages}p/{profile.table_signal_strength:.2f}"
        )
    if (
        profile.max_image_coverage_on_page >= LARGE_IMAGE_PAGE_RATIO
        or profile.pages_with_significant_images >= HARD_SIGNIFICANT_IMAGE_PAGES
        or profile.large_image_page_ratio >= HARD_LARGE_IMAGE_PAGE_RATIO
    ):
        hard_gate_reasons.append(
            "significant_images="
            f"{profile.pages_with_significant_images}p,max={profile.max_image_coverage_on_page:.1%}"
        )
    if profile.complex_page_ratio >= HARD_COMPLEX_PAGE_RATIO:
        hard_gate_reasons.append(f"complex_pages={profile.complex_page_ratio:.0%}")
    if profile.page_count > HARD_STANDARD_PAGE_COUNT:
        hard_gate_reasons.append(
            f"page_count={profile.page_count}>{HARD_STANDARD_PAGE_COUNT}"
        )

    benefit = _estimate_fast_benefit(profile)
    risk = _estimate_risk_score(profile)

    if hard_gate_reasons:
        return (
            "standard",
            "safe_standard",
            benefit,
            risk,
            [
                "decision=safe_standard: hard gate matched",
                "hard_gates=" + ",".join(hard_gate_reasons),
            ],
        )

    safe_fast_checks = [
        (
            profile.page_count <= SAFE_FAST_MAX_PAGE_COUNT,
            f"page_count={profile.page_count}<={SAFE_FAST_MAX_PAGE_COUNT}",
        ),
        (
            profile.avg_text_density >= MIN_FAST_TEXT_DENSITY_FLOOR,
            "text_density_floor="
            f"{profile.avg_text_density:.0f}>={MIN_FAST_TEXT_DENSITY_FLOOR}",
        ),
        (
            not profile.has_significant_images,
            f"has_significant_images={profile.has_significant_images}",
        ),
        (
            profile.max_image_coverage_on_page <= SAFE_FAST_MAX_IMAGE_COVERAGE_ON_PAGE,
            "max_image_coverage_on_page="
            f"{profile.max_image_coverage_on_page:.1%}<={SAFE_FAST_MAX_IMAGE_COVERAGE_ON_PAGE:.0%}",
        ),
        (
            profile.avg_image_coverage <= SAFE_FAST_MAX_AVG_IMAGE_COVERAGE,
            f"avg_image_coverage={profile.avg_image_coverage:.1%}<={SAFE_FAST_MAX_AVG_IMAGE_COVERAGE:.0%}",
        ),
        (
            profile.complex_page_ratio <= SAFE_FAST_MAX_COMPLEX_PAGE_RATIO,
            f"complex_page_ratio={profile.complex_page_ratio:.0%}<={SAFE_FAST_MAX_COMPLEX_PAGE_RATIO:.0%}",
        ),
        (
            profile.text_density_std <= SAFE_FAST_MAX_TEXT_STD,
            f"text_density_std={profile.text_density_std:.0f}<={SAFE_FAST_MAX_TEXT_STD:.0f}",
        ),
        (
            risk <= 0.35,
            f"estimated_risk_score={risk:.2f}<=0.35",
        ),
    ]

    failed_checks = [reason for passed, reason in safe_fast_checks if not passed]
    if not failed_checks:
        return (
            "fast",
            "safe_fast",
            benefit,
            risk,
            [
                "decision=safe_fast: low-complexity high-yield pdf",
                "safe_fast_checks_passed",
            ],
        )

    return (
        "standard",
        "gray_zone",
        benefit,
        risk,
        [
            "decision=gray_zone: conservative fallback to standard in phase1",
            "borderline=" + ",".join(failed_checks[:4]),
        ],
    )



@worker
def _profile_pdf_worker(queue, file_path: str) -> None:
    """Child process: analyze PDF features, return profile as dict."""
    import pymupdf

    profile = DocProfile(file_type="pdf")
    reasons: list[str] = []

    try:
        doc = pymupdf.open(file_path)
    except Exception as exc:
        profile.reasoning = f"Cannot open file: {exc}"
        publish_profile_result(queue, profile)
        return

    profile.page_count = doc.page_count

    if doc.page_count == 0:
        profile.reasoning = "Empty file (0 pages)"
        doc.close()
        del doc
        publish_profile_result(queue, profile)
        return

    if doc.page_count <= 50:
        sample_indices = list(range(doc.page_count))
    else:
        step = max(1, doc.page_count // 20)
        sample_indices = list(range(0, doc.page_count, step))[:20]
        sample_indices = sorted(
            set(
                sample_indices
                + [0, 1, 2]
                + [doc.page_count - 3, doc.page_count - 2, doc.page_count - 1]
            )
        )
        sample_indices = [idx for idx in sample_indices if 0 <= idx < doc.page_count]

    page_details = []
    text_lengths: list[float] = []
    total_text_len = 0
    total_image_coverage = 0.0
    scanned_pages = 0
    all_text_parts: list[str] = []
    has_any_fonts = False
    has_any_tables = False
    table_signal_pages = 0
    total_table_signal_strength = 0.0
    multi_col_pages = 0
    landscape_pages = 0
    degraded_pages = 0
    doc_page_sizes = []

    significant_image_count = 0
    pages_with_significant_images = 0
    large_image_pages = 0
    max_image_coverage_on_page = 0.0

    complex_pages = 0
    max_drawing_count = 0

    # Track xrefs already processed across pages to avoid redundant
    # get_image_rects() calls on shared/inherited image resources.
    # PDFs with shared xrefs (e.g. scanned docs) report ALL document
    # images on every page; without dedup this causes O(pages × images)
    # content-stream scans.
    seen_xrefs: set = set()

    for idx in sample_indices:
        page = doc[idx]
        page_width = page.rect.width
        page_height = page.rect.height
        page_area = page_width * page_height

        if page_width > page_height:
            landscape_pages += 1
        doc_page_sizes.append((page_width, page_height))

        text = page.get_text().strip()
        text_len = len(text)
        text_lengths.append(float(text_len))
        total_text_len += text_len

        if len("".join(all_text_parts)) < 500:
            all_text_parts.append(text[:200])

        images = page.get_images(full=True)
        img_total_area = 0.0
        page_significant_image_count = 0
        page_max_rect_ratio = 0.0
        page_medium_image_coverage = 0.0
        skinny_count = 0

        new_xref_count = 0
        for img in images:
            xref = img[0]
            img_w, img_h = img[2], img[3]
            if (
                img_h > 0
                and img_w / img_h > DEGRADED_SKINNY_ASPECT
                and img_h < DEGRADED_SKINNY_MAX_H
            ):
                skinny_count += 1

            # ── xref dedup: skip images already analyzed on earlier pages ──
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            new_xref_count += 1
            # Cap expensive get_image_rects calls per page
            if new_xref_count > PROFILE_MAX_NEW_XREFS_PER_PAGE:
                continue

            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []

            for rect in rects:
                rect_area = rect.width * rect.height
                img_total_area += rect_area

                area_ratio = rect_area / page_area if page_area > 0 else 0.0
                page_max_rect_ratio = max(page_max_rect_ratio, area_ratio)

                is_significant = (
                    area_ratio >= SIGNIFICANT_IMAGE_AREA_RATIO
                    or (
                        area_ratio >= 0.05
                        and (
                            max(img_w, img_h) >= SIGNIFICANT_IMAGE_MIN_DIM
                            or (img_w * img_h) >= SIGNIFICANT_IMAGE_MIN_PIXELS
                        )
                    )
                    or (
                        area_ratio >= 0.02
                        and (img_w * img_h) >= (SIGNIFICANT_IMAGE_MIN_PIXELS * 2)
                    )
                )

                if is_significant:
                    page_significant_image_count += 1
                elif area_ratio >= MEDIUM_IMAGE_AREA_RATIO:
                    page_medium_image_coverage += area_ratio

        if skinny_count >= DEGRADED_SKINNY_MIN_PER_PAGE:
            degraded_pages += 1

        img_coverage = img_total_area / page_area if page_area > 0 else 0.0
        img_coverage = min(img_coverage, 1.0)
        total_image_coverage += img_coverage

        fonts = page.get_fonts()
        if fonts:
            has_any_fonts = True

        drawings = page.get_drawings()
        drawing_count = len(drawings)
        max_drawing_count = max(max_drawing_count, drawing_count)
        line_like_items = 0
        horizontal_line_items = 0
        vertical_line_items = 0
        rect_items = 0
        fill_only_rect_items = 0
        for drawing in drawings:
            is_stroked = _is_stroked_drawing(drawing)
            for item in drawing.get("items", []):
                if item[0] == "l":
                    if is_stroked:
                        line_like_items += 1
                        point_a = item[1]
                        point_b = item[2]
                        if abs(point_a.y - point_b.y) <= 2:
                            horizontal_line_items += 1
                        if abs(point_a.x - point_b.x) <= 2:
                            vertical_line_items += 1
                elif item[0] == "re":
                    if is_stroked:
                        rect_items += 1
                        line_like_items += 4
                        horizontal_line_items += 2
                        vertical_line_items += 2
                    else:
                        fill_only_rect_items += 1

        detected_table_count = _count_detected_tables(page)
        drawing_table_signal = line_like_items >= TABLE_DRAWING_LINE_THRESHOLD and (
            (horizontal_line_items >= 2 and vertical_line_items >= 2)
            or rect_items >= TABLE_DRAWING_RECT_THRESHOLD
        )
        # NOTE:
        # `page.find_tables()` produces too many false positives on Word / Writer
        # exported pure-text PDFs, where paragraph background boxes are inferred as
        # full-page tables. For Phase 1 fast-path routing, keep `find_tables()`
        # only as debug evidence and rely on explicit drawing-grid signals for
        # table hard gates.
        table_hit = drawing_table_signal
        page_table_strength = 0.0
        if drawing_table_signal:
            page_table_strength = min(
                1.0,
                line_like_items / float(TABLE_DRAWING_STRONG_THRESHOLD),
            )

        if table_hit:
            has_any_tables = True
            table_signal_pages += 1
        total_table_signal_strength += page_table_strength

        blocks = page.get_text("blocks")
        text_blocks = [
            block
            for block in blocks
            if block[6] == 0
            and (block[2] - block[0]) > 20
            and (block[3] - block[1]) > 10
        ]

        is_multi_col_page = False
        if len(text_blocks) >= MULTI_COL_MIN_BLOCKS:
            min_x_gap = page.rect.width * MULTI_COL_GAP_RATIO
            side_by_side_count = 0

            for i in range(len(text_blocks)):
                for j in range(i + 1, len(text_blocks)):
                    block_i = text_blocks[i]
                    block_j = text_blocks[j]
                    y_overlap = min(block_i[3], block_j[3]) - max(
                        block_i[1], block_j[1]
                    )
                    if y_overlap <= 0:
                        continue
                    x_gap = max(block_j[0] - block_i[2], block_i[0] - block_j[2])
                    if x_gap > min_x_gap:
                        side_by_side_count += 1
                        if side_by_side_count >= 3:
                            is_multi_col_page = True
                            break
                if is_multi_col_page:
                    break

        if is_multi_col_page:
            multi_col_pages += 1

        is_scan_page = (
            text_len < SCAN_TEXT_THRESHOLD and img_coverage > SCAN_IMAGE_COVERAGE_MIN
        )
        if is_scan_page:
            scanned_pages += 1

        page_has_significant_images = (
            page_significant_image_count > 0 or page_medium_image_coverage >= 0.18
        )
        if page_has_significant_images:
            pages_with_significant_images += 1
            significant_image_count += page_significant_image_count or 1

        page_has_large_image = (
            page_max_rect_ratio >= LARGE_IMAGE_PAGE_RATIO or img_coverage >= 0.35
        )
        if page_has_large_image:
            large_image_pages += 1

        max_image_coverage_on_page = max(
            max_image_coverage_on_page, page_max_rect_ratio
        )

        is_complex_page = (
            table_hit
            or page_has_large_image
            or is_multi_col_page
            or (page_has_significant_images and text_len < FAST_TEXT_THRESHOLD)
            or (drawing_count >= 25 and text_len < FAST_TEXT_THRESHOLD)
        )
        if is_complex_page:
            complex_pages += 1

        page_details.append(
            {
                "page": idx + 1,
                "text_len": text_len,
                "image_count": len(images),
                "img_coverage": round(img_coverage, 3),
                "font_count": len(fonts),
                "drawing_count": drawing_count,
                "line_like_items": line_like_items,
                "horizontal_line_items": horizontal_line_items,
                "vertical_line_items": vertical_line_items,
                "table_hit": table_hit,
                "detected_table_count": detected_table_count,
                "stroked_rect_count": rect_items,
                "fill_only_rect_count": fill_only_rect_items,
                "significant_image_count": page_significant_image_count,
                "max_image_coverage": round(page_max_rect_ratio, 3),
                "is_multi_col_page": is_multi_col_page,
                "is_scan_page": is_scan_page,
                "is_complex_page": is_complex_page,
                "text_block_count": len(text_blocks),
            }
        )

        del text_blocks
        del blocks
        del drawings
        del fonts
        del images
        del page

    doc.close()
    del doc

    n_sampled = len(sample_indices)
    profile.avg_text_density = total_text_len / n_sampled if n_sampled > 0 else 0.0
    profile.avg_image_coverage = (
        total_image_coverage / n_sampled if n_sampled > 0 else 0.0
    )
    profile.has_embedded_fonts = has_any_fonts
    profile.has_tables = has_any_tables
    profile.is_multi_column = multi_col_pages > (n_sampled * 0.3)
    profile.is_degraded_electronic = degraded_pages > (n_sampled * DEGRADED_PAGE_RATIO)
    profile.sample_text = " ".join(all_text_parts)[:500]
    profile.page_details = page_details

    profile.has_significant_images = pages_with_significant_images > 0
    profile.significant_image_count = significant_image_count
    profile.max_image_coverage_on_page = max_image_coverage_on_page
    profile.pages_with_significant_images = pages_with_significant_images
    profile.large_image_page_ratio = (
        large_image_pages / n_sampled if n_sampled > 0 else 0.0
    )

    profile.table_signal_pages = table_signal_pages
    profile.table_signal_strength = (
        total_table_signal_strength / n_sampled if n_sampled > 0 else 0.0
    )

    profile.complex_pages = complex_pages
    profile.complex_page_ratio = complex_pages / n_sampled if n_sampled > 0 else 0.0
    profile.max_drawing_count = max_drawing_count
    profile.min_text_density_page = min(text_lengths) if text_lengths else 0.0
    profile.text_density_std = _stddev(text_lengths)

    scan_ratio = scanned_pages / n_sampled if n_sampled > 0 else 0.0
    if scan_ratio >= SCAN_PAGE_RATIO:
        profile.scan_type = "scanned"
        reasons.append(
            f"scanned: {scanned_pages}/{n_sampled} sampled pages are scanned ({scan_ratio:.0%})"
        )
    elif scanned_pages > 0:
        profile.scan_type = "mixed"
        reasons.append(f"mixed: {scanned_pages}/{n_sampled} sampled pages are scanned")
    else:
        profile.scan_type = "electronic"
        reasons.append(
            f"electronic: sampled pages contain extractable text (avg={profile.avg_text_density:.0f})"
        )

    landscape_ratio = landscape_pages / n_sampled if n_sampled > 0 else 0.0

    # ── Linear atlas gate: VLM always makes the final call ──
    # Any document meeting all 4 conditions is sent for VLM visual confirmation.
    # We do NOT heuristically commit here — VLM decides in parse_service.
    is_atlas_candidate = (
        profile.avg_text_density
        < ATLAS_TEXT_THRESHOLD  # text-sparse (< 200 chars/page)
        and profile.avg_image_coverage
        > ATLAS_CANDIDATE_IMAGE_COVERAGE_MIN  # image-heavy (> 30%)
        and landscape_ratio >= ATLAS_MIN_LANDSCAPE_RATIO  # mostly landscape (>= 50%)
        and profile.page_count >= ATLAS_MIN_PAGES  # multi-page (>= 2)
    )
    if is_atlas_candidate:
        profile.doc_category = (
            "generic"  # provisional — VLM will promote to "atlas" if confirmed
        )
        profile.atlas_candidate = True
        reasons.append(
            f"atlas_candidate: text={profile.avg_text_density:.0f}<{ATLAS_TEXT_THRESHOLD}, "
            f"img={profile.avg_image_coverage:.1%}>{ATLAS_CANDIDATE_IMAGE_COVERAGE_MIN:.0%}, "
            f"landscape={landscape_ratio:.0%}>={ATLAS_MIN_LANDSCAPE_RATIO:.0%}, "
            f"pages={profile.page_count}>={ATLAS_MIN_PAGES} → VLM confirmation required"
        )
    else:
        profile.doc_category = "generic"

    if landscape_ratio >= 0.8 and profile.doc_category == "generic":
        slide_ratios = [1.333, 1.778, 1.600]
        tolerance = 0.05
        ref_page = doc_page_sizes[0] if doc_page_sizes else None
        if ref_page:
            page_ratio = ref_page[0] / ref_page[1] if ref_page[1] > 0 else 0.0
            is_slide_ratio = any(
                abs(page_ratio - ratio) < tolerance for ratio in slide_ratios
            )
            if is_slide_ratio:
                profile.doc_category = "ppt_converted"
                reasons.append(
                    f"ppt_converted: {landscape_pages}/{n_sampled} landscape, ratio={page_ratio:.2f}"
                )

    route, decision_band, benefit, risk, route_reasons = _classify_route(profile)
    profile.route = route
    profile.decision_band = decision_band
    profile.estimated_fast_benefit = benefit
    profile.estimated_risk_score = risk
    reasons.extend(route_reasons)

    profile.reasoning = " | ".join(reasons)
    publish_profile_result(queue, profile)


def profile_pdf(file_path: str) -> DocProfile:
    """Profile a PDF by running PyMuPDF analysis in a spawned child process."""
    result = run_in_child_process(_profile_pdf_worker, file_path, timeout=300)
    profile = DocProfile(**result["profile"])
    logger.info(
        f"[doc-profiler] route={profile.route} band={profile.decision_band} "
        f"category={profile.doc_category} scan={profile.scan_type} "
        f"pages={profile.page_count} text_density={profile.avg_text_density:.0f} "
        f"img_coverage={profile.avg_image_coverage:.1%} risk={profile.estimated_risk_score:.2f} "
        f"gain={profile.estimated_fast_benefit:.2f}"
    )
    return profile
