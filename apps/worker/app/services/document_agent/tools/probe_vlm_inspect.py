"""Selective VLM inspection for ambiguous sampled PDF pages."""

from __future__ import annotations

import base64
import os
import tempfile
from typing import Any

from app.services.document_parser.formats.pdf.pymupdf_subprocess import run_in_child_process, worker
from loguru import logger
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

DEFAULT_QUESTION = (
    "Inspect these PDF page screenshots. For each page, decide whether it is a "
    "table-heavy page, image-heavy page, table of contents, blank/sparse page, "
    "landscape page, single-image page, or normal page. Return compact JSON with "
    "items: [{page, judgement, confidence, note}]."
)


@worker
def _render_vlm_pages_worker(
    queue,
    pdf_path: str,
    page_indices: list[int],
    dpi: int,
    out_dir: str,
) -> None:
    import pymupdf

    doc = pymupdf.open(pdf_path)
    rendered: list[dict[str, Any]] = []
    try:
        mat = pymupdf.Matrix(dpi / 72, dpi / 72)
        for idx in page_indices:
            if idx < 0 or idx >= doc.page_count:
                continue
            page = doc[idx]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out_path = os.path.join(out_dir, f"vlm_probe_p{idx + 1}.png")
            pix.save(out_path)
            rendered.append({"page_index": idx, "page_number": idx + 1, "path": out_path})
            pix = None
            page = None
    finally:
        doc.close()
    queue.put({"ok": True, "rendered": rendered})


def _png_to_data_url(path: str) -> str | None:
    try:
        with open(path, "rb") as file:
            data = base64.b64encode(file.read()).decode("utf-8")
        return f"data:image/png;base64,{data}"
    except Exception as exc:
        logger.warning(f"[document_agent.vlm_inspect] failed to encode {path}: {exc}")
        return None


def _call_vlm(
    *,
    image_items: list[dict[str, Any]],
    question: str,
    model: str | None = None,
    max_tokens: int = 900,
) -> tuple[str, dict[str, int]]:
    from shared.core.config import settings
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    effective_model = model or settings.IMAGE_MODEL or "qwen3.5-flash"
    client = get_openai_client(model=effective_model)
    content: list[ChatCompletionContentPartParam] = [
        ChatCompletionContentPartTextParam(
            type="text",
            text=question,
        )
    ]

    for item in image_items:
        url = item.get("data_url")
        if not url:
            continue
        content.append(
            ChatCompletionContentPartTextParam(
                type="text",
                text=f"Page {item['page_number']}:",
            )
        )
        content.append(
            ChatCompletionContentPartImageParam(
                type="image_url",
                image_url={"url": url},
            )
        )

    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": content}]
    return client.chat_completion_with_usage(
        messages=messages,
        model=effective_model,
        temperature=0.0,
        max_tokens=max_tokens,
    )


def vlm_inspect_pages(
    pdf_path: str,
    *,
    page_indices: list[int],
    question: str = DEFAULT_QUESTION,
    dpi: int = 120,
    model: str | None = None,
    max_tokens: int = 900,
    timeout: int = 60,
) -> dict[str, Any]:
    """Render selected 0-based pages and ask the configured VLM to inspect them."""
    if not page_indices:
        return {"observations": [], "raw_response": "", "usage": {}}

    with tempfile.TemporaryDirectory(prefix="doc_agent_vlm_") as tmp_dir:
        result = run_in_child_process(
            _render_vlm_pages_worker,
            pdf_path,
            sorted(set(page_indices)),
            dpi,
            tmp_dir,
            timeout=timeout,
        )
        image_items = []
        for item in result.get("rendered", []) or []:
            data_url = _png_to_data_url(item["path"])
            if data_url is not None:
                image_items.append({**item, "data_url": data_url})

        if not image_items:
            return {"observations": [], "raw_response": "", "usage": {}}

        response, usage = _call_vlm(
            image_items=image_items,
            question=question,
            model=model,
            max_tokens=max_tokens,
        )
        return {
            "observations": [
                {
                    "page": item["page_number"],
                    "page_index": item["page_index"],
                    "vlm_judgement": response,
                    "confidence": None,
                }
                for item in image_items
            ],
            "raw_response": response,
            "usage": usage,
        }
