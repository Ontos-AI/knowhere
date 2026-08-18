"""One-shot VLM coarse document classifier (PROFILE stage 0).

Input: ``ToolContext`` with bootstrap page features / stats on the blackboard.
Output: ``DocumentProfile`` plus a ``ToolResult`` for tracing.
Does not choose tools or drive shard planning.
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
from typing import Any, cast

from loguru import logger

from app.services.document_agent.coarse_profile.prompts import COARSE_PROFILE_INSTRUCTIONS
from app.services.document_agent.manifest import DocumentProfile, ToolContext, ToolResult
from app.services.document_agent.visual import render_pages
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
from shared.utils.token_estimate import estimate_tokens

PAGE_KIND_DEFINITIONS = {
    "normal": (
        "Page with enough extractable native text and no dominant table/image "
        "structure."
    ),
    "landscape": "Landscape-oriented page, often wide tables, drawings, slides, or diagrams.",
}

_COARSE_SAMPLE_CAP = 10
_COARSE_SEGMENT_QUOTAS = (2, 2, 2)  # front, middle, back
_BUDGET_STAGE = "coarse_profile"


def _feature_rows(ctx: ToolContext, pages: list[int]) -> list[dict[str, Any]]:
    labels_by_page = {label.page: label for label in ctx.blackboard.page_labels}
    selected = []
    for feature in ctx.blackboard.page_features:
        if feature.page not in pages:
            continue
        label = labels_by_page.get(feature.page)
        selected.append(
            {
                "page": feature.page,
                "kind": label.kind if label else None,
                "confidence": label.confidence if label else None,
                "raw_text_length": feature.raw_text_length,
                "text_density": feature.text_density,
                "orientation": feature.orientation,
                "width": feature.width,
                "height": feature.height,
                "is_blank_like": feature.is_blank_like,
            }
        )
    return selected


def _segment_sample(candidates: list[int], count: int) -> list[int]:
    if count <= 0 or not candidates:
        return []
    if len(candidates) <= count:
        return candidates
    if count == 1:
        return [candidates[len(candidates) // 2]]
    step = (len(candidates) - 1) / (count - 1)
    return [candidates[round(index * step)] for index in range(count)]


def _sample_pages(
    page_count: int,
    extrema_pages: list[int],
    *,
    random_extra: int = 0,
    rng: random.Random | None = None,
) -> list[int]:
    """Select representative pages for VLM profiling.

    Strategy (cap 10):
      1. Text extrema first (length/density min+max, ≤4 unique), unless the
         caller passes an empty list (e.g. all visible text lengths are 0).
      2. Fill remaining slots with 2/2/2 stratified samples from
         front/middle/back of the non-extrema pool.
      3. Optionally append ``random_extra`` uniform pages from the leftover
         pool (used when extrema were skipped because max text length is 0).
      4. Hard truncate to ``_COARSE_SAMPLE_CAP``.
    """
    if page_count <= 0:
        return []
    extrema = [page for page in extrema_pages if 1 <= page <= page_count]
    pool = [page for page in range(1, page_count + 1) if page not in set(extrema)]
    if not pool:
        return sorted(set(extrema))[:_COARSE_SAMPLE_CAP]
    third = max(len(pool) // 3, 1)
    front = pool[:third]
    middle = pool[third : third * 2]
    back = pool[third * 2 :]
    front_n, middle_n, back_n = _COARSE_SEGMENT_QUOTAS
    sampled = (
        _segment_sample(front, front_n)
        + _segment_sample(middle or pool, middle_n)
        + _segment_sample(back or pool, back_n)
    )
    ordered: list[int] = []
    for page in extrema + sampled:
        if page not in ordered:
            ordered.append(page)

    extra_n = max(int(random_extra), 0)
    if extra_n > 0:
        leftover = [
            page
            for page in range(1, page_count + 1)
            if page not in ordered
        ]
        if leftover:
            picker = rng if rng is not None else random.Random()
            for page in picker.sample(leftover, k=min(extra_n, len(leftover))):
                ordered.append(page)

    return ordered[:_COARSE_SAMPLE_CAP]


def _parse_margin_ratio(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= ratio <= 1.0:
        return ratio
    return None


def _parse_profile(raw: str) -> DocumentProfile:
    """Parse coarse-profile VLM JSON into ``DocumentProfile``."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("coarse profile output must be a JSON object")
    category = " ".join(str(data.get("category") or "unknown document").split()[:5])
    routing_category = PdfRoutingCategory.normalize(
        data.get("routing_category") or data.get("category")
    ).value
    raw_is_scanned = data.get("is_scanned")
    if isinstance(raw_is_scanned, bool):
        is_scanned = raw_is_scanned
    elif isinstance(raw_is_scanned, str):
        is_scanned = raw_is_scanned.strip().lower() in {"true", "yes", "1", "scanned"}
    else:
        is_scanned = bool(raw_is_scanned)
    header_y = _parse_margin_ratio(data.get("header_y"))
    footer_y = _parse_margin_ratio(data.get("footer_y"))
    if header_y is not None and footer_y is not None and header_y >= footer_y:
        header_y = None
        footer_y = None
    return DocumentProfile(
        is_scanned=is_scanned,
        category=category or "unknown document",
        routing_category=routing_category,
        language=str(data.get("language") or "unknown"),
        rationale=str(data.get("rationale") or ""),
        header_y=header_y,
        footer_y=footer_y,
    )


class CoarseProfiler:
    """One-shot VLM classifier: pages + features → ``DocumentProfile``."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def classify(self) -> tuple[DocumentProfile, ToolResult]:
        start = time.monotonic()
        model = self.ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
        text_max = float(
            (
                ((self.ctx.blackboard.doc_stats or {}).get("raw_text_length") or {}).get(
                    "max"
                )
                or {}
            ).get("value")
            or 0.0
        )
        # All-visible-zero docs: extrema collapse to a meaningless page-1 tie.
        # Skip them and draw one uniform random page instead.
        if text_max > 0:
            extrema_pages = self.ctx.blackboard.extrema_pages
            random_extra = 0
        else:
            extrema_pages = []
            random_extra = 1
        pages = _sample_pages(
            self.ctx.blackboard.page_count,
            extrema_pages,
            random_extra=random_extra,
        )
        if not model:
            profile = DocumentProfile(
                is_scanned=False,
                category="unknown document",
                routing_category=PdfRoutingCategory.GENERIC.value,
                rationale="No VLM model configured.",
            )
            return profile, ToolResult(
                status="ok",
                payload={"source": "deterministic", "sampled_pages": pages},
                latency_ms=int((time.monotonic() - start) * 1000),
                warnings=["No VLM model configured; using conservative profile."],
                input_summary={"page_count": self.ctx.blackboard.page_count},
                output_summary={"profile": profile.to_dict()},
            )

        pngs = render_pages(
            self.ctx,
            pages,
            folder_name="coarse_profile_pages",
            prefix="coarse",
            timeout=180,
        )
        feature_summary = _feature_rows(self.ctx, pages)
        payload = {
            "page_count": self.ctx.blackboard.page_count,
            "page_kind_counts": self.ctx.blackboard.global_signals.get(
                "page_kind_counts",
                {},
            ),
            "page_kind_definitions": PAGE_KIND_DEFINITIONS,
            "doc_shape": self.ctx.blackboard.global_signals.get("doc_shape", {}),
            "doc_stats": self.ctx.blackboard.doc_stats,
            "extrema_samples": self.ctx.blackboard.global_signals.get(
                "extrema_samples",
                [],
            ),
            "sampled_page_features": feature_summary,
        }
        prompt_text = COARSE_PROFILE_INSTRUCTIONS + "\nPayload:\n" + json.dumps(
            payload,
            ensure_ascii=False,
        )
        prompt_tokens_est = estimate_tokens(prompt_text) + len(pngs) * 800
        if not self.ctx.budget.try_reserve(
            "visual", prompt_tokens_est, stage=_BUDGET_STAGE
        ):
            raise RuntimeError("Insufficient visual budget for coarse profiling.")

        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for item in pngs:
            try:
                with open(str(item["png_path"]), "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                content_parts.append(
                    {"type": "text", "text": f"\n--- Page {item['page']} ---"}
                )
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    }
                )
            except Exception as exc:
                logger.warning(
                    "[document_agent] coarse profile png attach failed: {}", exc
                )

        try:
            from shared.services.ai.llm_overrides import get_vision_client

            client, model = get_vision_client(requested_model=model)
            raw, usage = client.chat_completion_with_usage(
                messages=cast(Any, [{"role": "user", "content": content_parts}]),
                model=model,
                temperature=0.0,
                max_tokens=1800,
                response_format={"type": "json_object"},
                usage_task="document_agent.coarse_profile",
            )
            self.ctx.budget.commit(
                "visual",
                actual=usage.get("total_tokens", prompt_tokens_est),
                est=prompt_tokens_est,
                stage=_BUDGET_STAGE,
            )
            profile = _parse_profile(raw)
            return profile, ToolResult(
                status="ok",
                payload={"source": "llm", "sampled_pages": pages},
                latency_ms=int((time.monotonic() - start) * 1000),
                tokens_used=usage.get("total_tokens", 0),
                input_summary=payload,
                output_summary={"profile": profile.to_dict()},
                debug={
                    "prompt_text": prompt_text,
                    "sampled_pages": pages,
                    "sampled_pngs": pngs,
                    "raw_response": raw,
                },
            )
        except Exception:
            self.ctx.budget.refund(
                "visual", est=prompt_tokens_est, stage=_BUDGET_STAGE
            )
            raise
