#!/usr/bin/env python3
# ruff: noqa: E402
"""Temporary offline eval: TOC start-page confirm, Jev Choice vs current model.

Does not change production code. Both arms get the same user text:
the production confirm prompt plus the same ``--- Page N ---`` page
blocks. Jev only changes the output to per-page Choice(true/false).

Jev cannot take screenshots, so this script feeds page text (already in
the Stage-0 cache) to both arms. The prompt string is imported and not
rewritten.

Usage (from apps/worker):

  TYPESAFE_API_KEY=... uv run python scripts/page_memory/eval_jev_toc_anchor_confirm.py \\
      --debug-dir ~/.knowhere/_debug_parse/EN_medical.pdf/page_memory
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
WORKER_ROOT = ROOT / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(ROOT / "packages" / "shared-python"))

from dotenv import load_dotenv

load_dotenv(WORKER_ROOT / ".env")

from loguru import logger

from app.services.document_agent.pdf_text import page_content_map
from app.services.document_agent.tools.extract_toc_with_boundaries import (
    BOUNDARY_STEP_PAGES,
    TOC_ANCHOR_CONFIRM_MAX_TOKENS,
    _CONFIRM_PROMPT,
    _iter_chunks,
    _parse_confirm_items,
)
from app.services.document_agent.tools.find_toc_anchor_pages import (
    MAX_ANCHOR_CANDIDATES,
    _filter_recurring_elements,
    _scan_toc_from_page_texts,
)
from shared.core.config import settings
from shared.services.ai.openai_compatible_client_sync import get_openai_client

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"


def _load_env_keys() -> None:
    if not os.environ.get("TYPESAFE_API_KEY") and os.environ.get("JET_KEY"):
        os.environ["TYPESAFE_API_KEY"] = os.environ["JET_KEY"]


def _load_page_texts(debug_dir: Path) -> dict[int, str]:
    cache_path = debug_dir / "_doc_agent" / "page_full_text_cache.json"
    if not cache_path.exists():
        raise FileNotFoundError(
            f"{cache_path} missing; run debug_pm_stage0_bootstrap.py first"
        )
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    return page_content_map(raw)


def _candidate_pages(page_texts: dict[int, str]) -> list[int]:
    page_count = max(page_texts) if page_texts else 0
    matches = _scan_toc_from_page_texts(page_texts, page_count=page_count)
    raw_pages = {int(match["page"]) for match in matches}
    surviving = (
        _filter_recurring_elements(matches, page_count) if matches else set()
    )
    if len(surviving) > MAX_ANCHOR_CANDIDATES:
        surviving = set(sorted(surviving)[:MAX_ANCHOR_CANDIDATES])
    pages = sorted(surviving)
    logger.info(
        "keyword scan: {} raw hits → {} candidates {}",
        len(raw_pages),
        len(pages),
        pages,
    )
    return pages


def _page_block(page: int, text: str) -> str:
    return f"\n--- Page {page} ---\n{text}"


def _user_text(pages: list[int], page_texts: dict[int, str]) -> str:
    parts = [_CONFIRM_PROMPT]
    for page in pages:
        parts.append(_page_block(page, page_texts.get(page, "")))
    return "".join(parts)


def _call_baseline(
    pages: list[int],
    page_texts: dict[int, str],
    *,
    model: str | None,
) -> dict[str, Any]:
    user_text = _user_text(pages, page_texts)
    resolved = model or os.environ.get("IMAGE_MODEL") or getattr(settings, "IMAGE_MODEL", None)
    if not resolved:
        raise SystemExit("IMAGE_MODEL is required for the baseline arm")
    client = get_openai_client(model=resolved)
    started = time.perf_counter()
    raw, usage = client.chat_completion_with_usage(
        messages=[{"role": "user", "content": user_text}],
        model=resolved,
        temperature=0.1,
        max_tokens=TOC_ANCHOR_CONFIRM_MAX_TOKENS,
        response_format={"type": "json_object"},
        usage_task="jev_eval.toc_anchor_confirm.baseline",
    )
    items = _parse_confirm_items(raw)
    by_page: dict[int, bool] = {}
    reasons: dict[int, str] = {}
    for item in items:
        if "page" not in item:
            continue
        page = int(item["page"])
        by_page[page] = bool(item.get("is_toc_start"))
        reasons[page] = str(item.get("reason") or "")
    return {
        "ok": True,
        "model": resolved or model,
        "latency_s": time.perf_counter() - started,
        "raw": raw,
        "by_page": by_page,
        "reasons": reasons,
        "usage": usage if isinstance(usage, dict) else getattr(usage, "__dict__", usage),
        "user_text": user_text,
    }


def _jev_questions(pages: list[int]) -> dict[str, Any]:
    # Output adapter only: same true/false field the production JSON asks for.
    # Task rules stay in `_CONFIRM_PROMPT` inside `state`.
    return {
        f"page_{page}": {
            "type": "choice",
            "instructions": f"is_toc_start for page {page}",
            "criteria": {"true": None, "false": None},
        }
        for page in pages
    }


def _call_jev(
    pages: list[int],
    user_text: str,
    *,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    payload = {
        "state": user_text,
        "model": model,
        "questions": _jev_questions(pages),
    }
    started = time.perf_counter()
    try:
        from typesafe_sdk import Choice, TypeSafeClient

        questions = {
            key: Choice(
                instructions=spec["instructions"],
                criteria=spec["criteria"],
            )
            for key, spec in payload["questions"].items()
        }
        with TypeSafeClient(api_key=api_key, timeout=60.0) as client:
            response = client.system_one(
                state=user_text,
                questions=questions,
                model=model,
            )
        answers = {
            key: {
                "choice": getattr(answer, "choice", None),
                "confidence": getattr(answer, "confidence", None),
                "probabilities": getattr(answer, "probabilities", None),
            }
            for key, answer in response.choices.items()
        }
        usage = getattr(response, "usage", None)
        return {
            "ok": True,
            "model": getattr(response, "model", model),
            "latency_s": time.perf_counter() - started,
            "answers": answers,
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            },
        }
    except ImportError:
        import httpx

        response = httpx.post(
            TYPESAFE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60.0,
        )
        response.raise_for_status()
        body = response.json()
        raw_answers = body.get("answers") or {}
        answers = {}
        for key, answer in raw_answers.items():
            if not isinstance(answer, dict):
                continue
            answers[key] = {
                "choice": answer.get("choice"),
                "confidence": answer.get("confidence"),
                "probabilities": answer.get("probabilities"),
            }
        return {
            "ok": True,
            "model": body.get("model", model),
            "latency_s": time.perf_counter() - started,
            "answers": answers,
            "usage": body.get("usage") or {},
        }


def _jev_bool(choice: Any) -> bool | None:
    if choice is None:
        return None
    text = str(choice).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _eval_one(debug_dir: Path, *, baseline_model: str | None, jev_model: str) -> dict[str, Any]:
    page_texts = _load_page_texts(debug_dir)
    pages = _candidate_pages(page_texts)
    report: dict[str, Any] = {
        "debug_dir": str(debug_dir),
        "candidate_pages": pages,
        "chunks": [],
        "pages": [],
    }
    if not pages:
        report["error"] = "no TOC keyword candidates"
        return report

    api_key = os.environ.get("TYPESAFE_API_KEY") or ""
    if not api_key:
        raise SystemExit(
            "TYPESAFE_API_KEY (or JET_KEY) is required for the Jev arm"
        )

    for chunk in _iter_chunks(pages, BOUNDARY_STEP_PAGES):
        logger.info("chunk pages={}", chunk)
        baseline = _call_baseline(chunk, page_texts, model=baseline_model)
        jev = _call_jev(
            chunk,
            baseline["user_text"],
            api_key=api_key,
            model=jev_model,
        )
        report["chunks"].append(
            {
                "pages": chunk,
                "baseline": {
                    key: baseline[key]
                    for key in ("ok", "model", "latency_s", "by_page", "reasons", "usage")
                },
                "jev": {
                    key: jev[key]
                    for key in ("ok", "model", "latency_s", "answers", "usage")
                    if key in jev
                },
            }
        )
        for page in chunk:
            jev_choice = (jev.get("answers") or {}).get(f"page_{page}") or {}
            report["pages"].append(
                {
                    "page": page,
                    "baseline_is_toc_start": baseline["by_page"].get(page),
                    "baseline_reason": baseline["reasons"].get(page, ""),
                    "jev_is_toc_start": _jev_bool(jev_choice.get("choice")),
                    "jev_choice": jev_choice.get("choice"),
                    "jev_confidence": jev_choice.get("confidence"),
                    "jev_probabilities": jev_choice.get("probabilities"),
                    "agree": baseline["by_page"].get(page)
                    == _jev_bool(jev_choice.get("choice"))
                    if page in baseline["by_page"]
                    and _jev_bool(jev_choice.get("choice")) is not None
                    else None,
                }
            )
    return report


def _summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for report in reports for row in report.get("pages", [])]
    comparable = [row for row in scored if row.get("agree") is not None]
    agree = sum(1 for row in comparable if row["agree"])
    return {
        "docs": len(reports),
        "candidate_pages": len(scored),
        "comparable": len(comparable),
        "agree": agree,
        "agree_rate": (agree / len(comparable)) if comparable else None,
        "disagreements": [row for row in comparable if not row["agree"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temp eval: TOC confirm prompt + page text, Jev Choice vs current model"
    )
    parser.add_argument(
        "--debug-dir",
        action="append",
        default=[],
        help="page_memory debug dir that already has Stage-0 page_full_text_cache.json",
    )
    parser.add_argument(
        "--baseline-model",
        default=None,
        help="Same channel as production confirm (IMAGE_MODEL) unless overridden",
    )
    parser.add_argument("--jev-model", default=TYPESAFE_MODEL)
    args = parser.parse_args()
    _load_env_keys()

    debug_dirs = [Path(path).expanduser().resolve() for path in args.debug_dir]
    if not debug_dirs:
        parser.error("pass at least one --debug-dir")

    reports = []
    for debug_dir in debug_dirs:
        logger.info("eval {}", debug_dir)
        report = _eval_one(
            debug_dir,
            baseline_model=args.baseline_model,
            jev_model=args.jev_model,
        )
        reports.append(report)
        out_path = debug_dir / "_jev_eval" / "toc_anchor_confirm.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("wrote {}", out_path)

    summary = _summarize(reports)
    summary_path = debug_dirs[0] / "_jev_eval" / "toc_anchor_confirm_summary.json"
    if len(debug_dirs) > 1:
        summary_path = debug_dirs[0].parent.parent / "_jev_eval_toc_anchor_confirm_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({"summary": summary, "reports": reports}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in summary["disagreements"]:
        print(
            f"disagree page={row['page']} baseline={row['baseline_is_toc_start']} "
            f"jev={row['jev_is_toc_start']} conf={row['jev_confidence']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
