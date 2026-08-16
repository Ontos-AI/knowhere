"""Generic inspect.pages tool: open physical pages, render, answer a question."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext, ToolResult


_DEFAULT_PAGE_CAP = 5


def inspect_pages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Open one or more physical PDF pages, render them, and answer ``question``.

    Hard limits are token/loop budgets (via ``BudgetTracker``), not a total page
    counter. ``inspect_page_cap`` only caps pages **per call** (batch size).
    """
    start = time.monotonic()
    raw_pages = args.get("pages") or []
    question = str(args.get("question") or "").strip()
    if not isinstance(raw_pages, list) or not raw_pages:
        return ToolResult(
            status="error",
            error="inspect.pages requires pages[]",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    if not question:
        return ToolResult(
            status="error",
            error="inspect.pages requires question",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    page_count = int(ctx.blackboard.page_count or 0)
    pages: list[int] = []
    for item in raw_pages:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= page <= page_count and page not in pages:
            pages.append(page)
    page_cap = int(ctx.settings.get("inspect_page_cap") or _DEFAULT_PAGE_CAP)
    pages = pages[: max(page_cap, 1)]
    if not pages:
        return ToolResult(
            status="error",
            error="no valid physical pages in range",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    from app.services.document_agent.visual import render_pages

    folder_name = str(args.get("folder_name") or "inspect_pages")
    prefix = str(args.get("prefix") or "inspect")
    rendered = render_pages(
        ctx,
        pages,
        folder_name=folder_name,
        prefix=prefix,
        timeout=120,
    )
    if not rendered:
        return ToolResult(
            status="error",
            error="render failed",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    if not model:
        return ToolResult(
            status="error",
            error="vlm_model missing",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    # Token budget (optional stage, e.g. calibration). No total page-count ledger.
    stage = args.get("visual_stage") or ctx.settings.get("inspect_visual_stage")
    stage_name = str(stage).strip() if stage else None
    est = 800 * len(rendered) + 800
    if stage_name and ctx.budget is not None:
        if not ctx.budget.try_reserve("visual", est, stage=stage_name):
            return ToolResult(
                status="error",
                error="calibration visual budget exhausted",
                latency_ms=int((time.monotonic() - start) * 1000),
            )

    prompt = (
        "Answer the question about the provided PDF page image(s). "
        'Return strict JSON object with keys: {"answer": string}. '
        "Include the word json in your reasoning.\n\n"
        f"Pages: {pages}\nQuestion: {question}\n"
    )
    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in rendered:
        with open(str(item["png_path"]), "rb") as image_file:
            img_b64 = base64.b64encode(image_file.read()).decode()
        content_parts.append({"type": "text", "text": f"\n--- Page {item['page']} ---"})
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )

    usage_task = str(args.get("usage_task") or "document_agent.inspect_pages")
    try:
        from shared.services.ai.llm_overrides import get_vision_client

        client, model = get_vision_client(requested_model=str(model))
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=model,
            temperature=0.0,
            max_tokens=800,
            response_format={"type": "json_object"},
            usage_task=usage_task,
        )
        payload = json.loads(raw) if raw else {}
        tokens_used = int((usage or {}).get("total_tokens") or 0)
        if stage_name and ctx.budget is not None:
            ctx.budget.commit(
                "visual",
                actual=tokens_used or est,
                est=est,
                stage=stage_name,
            )
    except Exception as exc:
        if stage_name and ctx.budget is not None:
            ctx.budget.refund("visual", est=est, stage=stage_name)
        logger.warning("[inspect.pages] VLM failed: {}", exc)
        return ToolResult(
            status="error",
            error=f"vlm failed: {exc}",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    return ToolResult(
        status="ok",
        payload={
            "pages": pages,
            "question": question,
            "answer": payload.get("answer"),
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        tokens_used=tokens_used,
        output_summary={"pages": pages, "answer": payload.get("answer")},
    )
