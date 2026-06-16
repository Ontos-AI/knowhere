from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.visual import render_pages
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import PARSER_ROW_COLUMNS
from app.services.page_memory.normalizer import normalize_to_pdf

from shared.core.exceptions.domain_exceptions import ValidationException


_SUPPORTED_GRANULARITY = "whole_doc"
_UNSUPPORTED_GRANULARITY_REASON = "PAGE_MEMORY_GRANULARITY_NOT_IMPLEMENTED"


@dataclass(frozen=True)
class PageMemoryInput:
    file_path: str
    filename: str
    output_dir: str
    job_id: str | None = None
    internal_output_filename: str | None = None
    base_url: str = ""


def run(request: PageMemoryInput) -> tuple[str, pd.DataFrame]:
    """Run the page-memory track.

    PR3 intentionally implements only the whole_doc skeleton. Full page mode,
    tagging, and section mapping land in PR4.
    """
    full_output_dir = _resolve_output_dir(request)
    os.makedirs(full_output_dir, exist_ok=True)
    pdf_path, pdf_filename = normalize_to_pdf(
        file_path=request.file_path,
        filename=request.filename,
        output_dir=full_output_dir,
        base_url=request.base_url,
    )
    profile = profile_document(
        pdf_path,
        pdf_filename,
        job_id=request.job_id,
        output_dir=full_output_dir,
    )
    verdict = _decide_granularity(profile)
    if verdict != _SUPPORTED_GRANULARITY:
        _raise_unsupported_granularity(verdict)
    return full_output_dir, _build_whole_doc_dataframe(
        pdf_path=pdf_path,
        filename=request.filename,
        output_dir=full_output_dir,
        page_count=max(int(profile.page_count or 0), 0),
        verdict=verdict,
    )


def _resolve_output_dir(request: PageMemoryInput) -> str:
    output_name = request.internal_output_filename or request.filename
    return os.path.join(request.output_dir, Path(output_name).stem)


def _decide_granularity(profile: Any) -> str:
    page_count = int(getattr(profile, "page_count", 0) or 0)
    toc = getattr(profile, "toc", None)
    has_toc = bool(getattr(toc, "has_toc", False))
    if page_count > 200:
        return "shard_page"
    if page_count <= 6 and not has_toc:
        return "whole_doc"
    return "page"


def _raise_unsupported_granularity(verdict: str) -> None:
    raise ValidationException(
        user_message=(
            "page_memory is enabled, but this PR only supports whole-document "
            "page memory. Per-page and shard-page modes are intentionally gated "
            "until the page renderer, tagger, and section mapper land."
        ),
        violations=[
            {
                "field": "parse_track",
                "description": (
                    f"{_UNSUPPORTED_GRANULARITY_REASON}: "
                    f"granularity={verdict}; supported={_SUPPORTED_GRANULARITY}"
                ),
            }
        ],
        internal_message=(
            f"{_UNSUPPORTED_GRANULARITY_REASON}: granularity={verdict}; "
            f"supported={_SUPPORTED_GRANULARITY}"
        ),
    )


def _build_whole_doc_dataframe(
    *,
    pdf_path: str,
    filename: str,
    output_dir: str,
    page_count: int,
    verdict: str,
) -> pd.DataFrame:
    pages = list(range(1, page_count + 1)) if page_count > 0 else [1]
    page_texts = read_page_texts(pdf_path, pages)
    raw_text = "\n\n".join(page_texts.get(page, "") for page in pages).strip()
    summary = _build_summary(filename=filename, page_count=page_count, raw_text=raw_text)
    page_image_uris = _render_page_images(
        pdf_path=pdf_path,
        output_dir=output_dir,
        page_count=page_count,
        pages=pages,
    )
    content = f"[SUMMARY]\n{summary}\n\n[RAW]\n{raw_text}".strip()
    know_id = gen_str_codes(f"wholedoc::{filename}::{content}")
    row = {
        "content": content,
        "path": f"{filename}/Root",
        "type": "page",
        "length": len(content),
        "keywords": "",
        "summary": summary,
        "know_id": know_id,
        "tokens": "",
        "connectto": "",
        "addtime": get_str_time(),
        "page_nums": ",".join(str(page) for page in pages),
        "extra_metadata": {
            "granularity": "whole_doc",
            "strategy_used": "whole_doc" if verdict == "whole_doc" else "whole_doc_fallback",
            "source_verdict": verdict,
            "page_index": None,
            "page_image_uris": page_image_uris,
            "status": "clear",
        },
    }
    return pd.DataFrame([row], columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))


def _build_summary(*, filename: str, page_count: int, raw_text: str) -> str:
    prefix = f"{filename} whole-document memory ({page_count} pages)"
    preview = " ".join(raw_text.split())[:500]
    return f"{prefix}: {preview}" if preview else prefix


def _render_page_images(
    *,
    pdf_path: str,
    output_dir: str,
    page_count: int,
    pages: list[int],
) -> list[str]:
    if page_count <= 0:
        return []
    blackboard = AgentBlackboard()
    blackboard.page_count = page_count
    ctx = ToolContext(
        pdf_path=pdf_path,
        job_id="page_memory_render",
        blackboard=blackboard,
        budget=None,
        trace=None,
        output_dir=output_dir,
        settings={},
    )
    rendered = render_pages(ctx, pages, folder_name="pages", prefix="page", timeout=180)
    return [
        str(Path(item["png_path"]).relative_to(output_dir))
        for item in rendered
        if item.get("png_path")
    ]
