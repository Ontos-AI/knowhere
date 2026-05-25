"""Generic VLM inspection tool for selected PDF pages."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, cast

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import has_page_features, register_tool
from app.services.document_agent.visual import render_pages
from shared.utils.token_estimate import estimate_tokens


@register_tool(
    name="inspect.pages",
    description="Render arbitrary PDF pages and ask the VLM a custom profiling question.",
    parameters={
        "type": "object",
        "properties": {
            "pages": {"type": "array", "items": {"type": "integer"}},
            "question": {"type": "string"},
        },
        "required": ["pages", "question"],
    },
    preconditions=(has_page_features,),
)
def inspect_pages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    pages = sorted(
        {
            int(page)
            for page in (args.get("pages") or [])
            if 1 <= int(page) <= ctx.blackboard.page_count
        }
    )[:10]
    if not pages:
        return ToolResult(
            status="error",
            error="inspect.pages requires at least one valid page",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    question = str(args.get("question") or "Describe the document structure visible on these pages.")
    pngs = render_pages(ctx, pages, folder_name="inspect_pages", prefix="inspect")
    model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    prompt = (
        "You are inspecting PDF page screenshots for a document profiling agent. "
        "Answer strict JSON with keys: observations, implications, recommended_next_action. "
        "observations must be an array of {page, summary, visual_kind}. "
        f"Question: {question}"
    )
    est = estimate_tokens(prompt) + len(pngs) * 800
    if not model:
        payload = {"pages": pages, "pngs": pngs, "note": "No VLM model configured."}
        ctx.blackboard.global_signals.setdefault("visual_inspections", []).append(payload)
        return ToolResult(
            status="ok",
            payload=payload,
            latency_ms=int((time.monotonic() - start) * 1000),
            warnings=["No VLM model configured; returned rendered page paths only."],
        )
    if not ctx.budget.try_reserve("visual", est):
        return ToolResult(
            status="error",
            error="insufficient visual budget",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in pngs:
        with open(str(item["png_path"]), "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content_parts.append({"type": "text", "text": f"\n--- Page {item['page']} ---"})
        content_parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
        )
    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=model,
            temperature=0.0,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        ctx.budget.commit("visual", actual=usage.get("total_tokens", est), est=est)
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        if isinstance(payload, dict):
            payload.setdefault("pages", pages)
        else:
            payload = {"result": payload, "pages": pages}
        ctx.blackboard.global_signals.setdefault("visual_inspections", []).append(payload)
        return ToolResult(
            status="ok",
            payload=payload,
            latency_ms=int((time.monotonic() - start) * 1000),
            tokens_used=usage.get("total_tokens", 0),
        )
    except Exception:
        ctx.budget.refund("visual", est=est)
        raise
