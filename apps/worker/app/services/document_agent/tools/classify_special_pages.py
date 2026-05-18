"""Classify sampled pages into Phase 1 special page kinds."""

from __future__ import annotations

import json
from typing import Any

from app.services.document_agent.manifest import SpecialKind
from app.services.document_agent.tools.llm_json import extract_json_object
from loguru import logger

ALLOWED_KINDS: set[str] = {
    "toc",
    "blank",
    "sparse",
    "table_heavy",
    "image_heavy",
    "landscape",
    "single_image",
    "normal",
}

PROMPT = """You are classifying sampled PDF pages for a document split planner.
Return ONLY valid json.

Allowed special_kind values:
- toc: table of contents / contents pages
- blank: blank page
- sparse: very little useful content
- table_heavy: mostly tables or dense tabular rules
- image_heavy: many charts/photos/figures
- landscape: landscape page that should not be split through a landscape block
- single_image: one large screenshot/scanned image dominates the page
- normal: ordinary text page

Use structural features first, and use VLM observations when present. Be conservative:
only mark toc/table/image/landscape/single_image when there is clear evidence.

JSON schema:
{
  "pages": [
    {"page": 1, "special_kind": "normal", "confidence": 0.75, "note": "short reason"}
  ],
  "global_notes": "short summary"
}
"""


def _heuristic_kind(page: dict[str, Any]) -> tuple[SpecialKind, float, str]:
    text = str(page.get("text_preview") or "")
    text_len = int(page.get("text_length") or 0)
    image_coverage = float(page.get("image_coverage") or 0.0)
    table_count = int(page.get("table_count") or 0)
    drawings_count = int(page.get("drawings_count") or 0)
    orientation = str(page.get("orientation") or "")
    is_blank_like = bool(page.get("is_blank_like"))

    toc_markers = ["目录", "contents", "table of contents"]
    if any(marker.lower() in text.lower() for marker in toc_markers):
        return "toc", 0.82, "text preview contains TOC marker"
    if is_blank_like:
        return "blank", 0.9, "very low text/image/drawing signal"
    if image_coverage >= 0.72 and text_len < 250:
        return "single_image", 0.82, "one or more images dominate the page"
    if table_count > 0 or drawings_count >= 80:
        return "table_heavy", 0.72, "table detector or dense ruled drawings fired"
    if image_coverage >= 0.35:
        return "image_heavy", 0.72, "high image coverage"
    if orientation == "landscape":
        return "landscape", 0.75, "page is landscape"
    if text_len < 80:
        return "sparse", 0.68, "short text and no stronger special signal"
    return "normal", 0.65, "no special signal"


def heuristic_classify_special_pages(
    sampled_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    pages = []
    for page in sampled_pages:
        kind, confidence, note = _heuristic_kind(page)
        pages.append(
            {
                "page": int(page.get("page_number") or 0),
                "special_kind": kind,
                "confidence": confidence,
                "note": note,
            }
        )
    return {"pages": pages, "global_notes": "heuristic classification"}


def _normalize_llm_pages(
    data: dict[str, Any],
    sampled_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    by_page = {int(page.get("page_number") or 0): page for page in sampled_pages}
    pages = []
    for item in data.get("pages", []) or []:
        if not isinstance(item, dict):
            continue
        page_number = int(item.get("page") or item.get("page_number") or 0)
        if page_number not in by_page:
            continue
        kind = str(item.get("special_kind") or item.get("kind") or "normal")
        if kind not in ALLOWED_KINDS:
            kind = "normal"
        confidence = max(0.0, min(float(item.get("confidence") or 0.0), 1.0))
        if confidence <= 0:
            confidence = 0.5
        pages.append(
            {
                "page": page_number,
                "special_kind": kind,
                "confidence": confidence,
                "note": str(item.get("note") or "")[:300],
            }
        )

    seen = {item["page"] for item in pages}
    for fallback in heuristic_classify_special_pages(sampled_pages)["pages"]:
        if fallback["page"] not in seen:
            pages.append(fallback)
    pages.sort(key=lambda item: item["page"])
    return {
        "pages": pages,
        "global_notes": str(data.get("global_notes") or data.get("notes") or "")[:1000],
    }


def classify_special_pages(
    sampled_pages: list[dict[str, Any]],
    *,
    vlm_observations: list[dict[str, Any]] | None = None,
    model: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Classify special pages with LLM, falling back to deterministic heuristics."""
    if not use_llm:
        return heuristic_classify_special_pages(sampled_pages)

    try:
        from shared.core.config import settings
        from shared.utils.OpenAICompatibleClientSync import get_openai_client

        effective_model = model or settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL
        client = get_openai_client(model=effective_model)
        payload = {
            "sampled_pages": sampled_pages,
            "vlm_observations": vlm_observations or [],
        }
        response = client.chat_completion(
            messages=[
                {"role": "system", "content": PROMPT},
                {
                    "role": "user",
                    "content": "Classify these pages as json:\n"
                    + json.dumps(payload, ensure_ascii=False),
                },
            ],
            model=effective_model,
            temperature=0.0,
            max_tokens=1800,
            response_format={"type": "json_object"},
        )
        return _normalize_llm_pages(extract_json_object(response), sampled_pages)
    except Exception as exc:
        logger.warning(
            f"[document_agent.classify_special_pages] LLM classification failed, "
            f"using heuristics: {exc}"
        )
        return heuristic_classify_special_pages(sampled_pages)
