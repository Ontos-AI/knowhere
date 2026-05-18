"""
VLM-based Atlas Classifier

Second-pass visual confirmation for atlas_candidate documents.
Renders the first 3 pages of a PDF as PNG images, then asks the vision
model to decide whether the document is an engineering atlas.

Architecture:
  - Page rendering via PyMuPDF runs in a spawned child process (consistent
    with the rest of the parsing pipeline).
  - VLM call is made in the *main process* after the child exits cleanly.
  - Fails gracefully: any error returns False (treat as non-atlas).
"""

import base64
import os
import tempfile
from typing import Optional

from app.services.document_parser.pymupdf_subprocess import run_in_child_process, worker
from loguru import logger
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

# ── Prompt ──────────────────────────────────────────────────────────────────
_ATLAS_JUDGE_PROMPT = """You are a document classification expert. Please observe the following PDF page screenshots and determine whether the document is an engineering atlas (drawing collection).

[Typical Characteristics of an Engineering Atlas]
- Page content is primarily technical drawings (e.g., architectural floor plans, structural details, pipeline installation diagrams, equipment layout plans).
- Usually contains a title block / info bar (including drawing name, drawing number, design institute, scale, date, etc.).
- Pages consist mainly of graphics, lines, annotations, and dimensions, with very little pure text.
- Page orientation is typically landscape (mostly A3 landscape).
- Common types: National standard design atlases (e.g., 09 series, 22 series), construction drawings, installation detail drawings.

[Judgment Criteria]
- If this IS an engineering atlas, reply ONLY with: yes
- If this IS NOT an engineering atlas (e.g., normal report, academic paper, presentation slides), reply ONLY with: no

You must reply ONLY with "yes" or "no", do not say anything else."""

# ── Child-process renderer ───────────────────────────────────────────────────


@worker
def _render_pages_worker(
    queue, pdf_path: str, page_indices: list, dpi: int, out_dir: str
) -> None:
    """Child process: render given PDF pages to PNG files in out_dir."""
    import pymupdf

    mat = pymupdf.Matrix(dpi / 72, dpi / 72)
    rendered: list[str] = []
    try:
        doc = pymupdf.open(pdf_path)
        for idx in page_indices:
            if idx >= doc.page_count:
                break
            page = doc[idx]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out_path = os.path.join(out_dir, f"atlas_preview_p{idx + 1}.png")
            pix.save(out_path)
            rendered.append(out_path)
            pix = None
            page = None
        doc.close()
    except Exception as exc:
        queue.put({"ok": False, "error": str(exc), "rendered": []})
        return
    queue.put({"ok": True, "rendered": rendered})


def _render_preview_pages(
    pdf_path: str,
    page_indices: list[int],
    out_dir: str,
    dpi: int = 120,
) -> list[str]:
    """Render pages to PNG files. Returns list of file paths."""
    result = run_in_child_process(
        _render_pages_worker, pdf_path, page_indices, dpi, out_dir, timeout=30
    )
    if not result.get("ok"):
        raise RuntimeError(f"Page render failed: {result.get('error')}")
    return result["rendered"]


def _png_to_data_url(path: str) -> Optional[str]:
    """Base64-encode a PNG file as a data URL."""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return f"data:image/png;base64,{data}"
    except Exception as exc:
        logger.warning(f"[atlas_classifier] Failed to encode {path}: {exc}")
        return None


def _call_vlm(image_data_urls: list[str]) -> bool:
    """Call VLM with preview images. Returns True if atlas, False otherwise."""
    from shared.core.config import settings
    from shared.utils.OpenAICompatibleClientSync import get_openai_client

    model = settings.IMAGE_MODEL or "qwen-vl-plus"
    client = get_openai_client(model=model)

    content: list[ChatCompletionContentPartParam] = [
        ChatCompletionContentPartTextParam(
            type="text",
            text=_ATLAS_JUDGE_PROMPT,
        )
    ]
    for url in image_data_urls:
        content.append(
            ChatCompletionContentPartImageParam(
                type="image_url",
                image_url={"url": url},
            )
        )

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": content}
    ]
    resp: str = client.chat_completion(
        messages=messages,
        model=model,
        temperature=0.0,
        max_tokens=8,
    )
    answer = resp.strip().lower().strip(".")
    logger.info(f"[atlas_classifier] VLM answer: {repr(resp)}")
    return answer.startswith("yes") or answer == "1" or answer == "true"


# ── Public API ───────────────────────────────────────────────────────────────


def classify_atlas_with_vlm(pdf_path: str, n_pages: int = 3) -> bool:
    """
    Render the first `n_pages` pages of `pdf_path` and ask the VLM whether
    the document is an engineering atlas.

    Returns:
        True  → confirmed atlas
        False → not an atlas (or error — fail-safe default)
    """
    page_indices = list(range(n_pages))
    with tempfile.TemporaryDirectory(prefix="atlas_clf_") as tmp_dir:
        try:
            png_paths = _render_preview_pages(pdf_path, page_indices, tmp_dir)
            if not png_paths:
                logger.warning(
                    "[atlas_classifier] No pages rendered, defaulting to non-atlas"
                )
                return False

            data_urls = [u for p in png_paths if (u := _png_to_data_url(p)) is not None]
            if not data_urls:
                logger.warning(
                    "[atlas_classifier] No images encoded, defaulting to non-atlas"
                )
                return False

            logger.info(
                f"[atlas_classifier] Sending {len(data_urls)} page(s) to VLM for atlas check"
            )
            is_atlas = _call_vlm(data_urls)
            logger.info(f"[atlas_classifier] VLM result: is_atlas={is_atlas}")
            return is_atlas

        except Exception as exc:
            logger.warning(
                f"[atlas_classifier] VLM atlas check failed for {pdf_path!r}, "
                f"defaulting to non-atlas. Error: {exc}"
            )
            return False
