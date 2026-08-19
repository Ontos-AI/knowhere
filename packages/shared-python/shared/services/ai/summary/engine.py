"""Unified summary engine (audit §4.1 / §4.2).

A single entrypoint both ingestion tracks call. It centralizes what used to be
duplicated across ``text/parser``, ``page_tagger``, ``node_assembler``, and
``image/parser``:

- prompt construction (via the shared ``build_prompt`` registry),
- the text-or-vision LLM call (with optional page/asset images),
- JSON parsing with one retry,
- budget reserve/commit/refund for visual calls,
- deterministic language locking for text input.

Two public functions:

- ``summarize(...) -> BodySummary | AssetSummary`` — dispatches by ``mode``.
- ``transcribe(...) -> str`` — the single OCR primitive (§4.2) that replaces the
  former ``ocr-image`` and ``page-memory-vlm-ocr`` paths.

The engine returns the typed contracts in ``model.py``; callers map those onto
their rows. Entity enrichment (§4.4) plugs into the same parse step
without changing call sites.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Literal, cast, overload

from loguru import logger

from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.ai import openai_compatible_client_sync as _client_mod
from shared.services.ai.prompt_service import _detect_text_language, build_prompt
from shared.services.ai.response_process_service import eval_response
from shared.services.ai.summary.model import (
    AssetSummary,
    BodySummary,
    Entity,
)
from shared.utils.token_estimate import estimate_tokens

SummaryMode = Literal["text", "page", "asset"]

_MAX_JSON_RETRIES = 1
_IMAGE_TOKEN_EST = 800


def _read_image_b64(image_path: str) -> str | None:
    try:
        with open(image_path, "rb") as handle:
            return base64.b64encode(handle.read()).decode()
    except Exception as exc:
        logger.warning("[summary] failed to read image {}: {}", image_path, exc)
        return None


def _parse_linesplit_asset(raw: str, title_hint: str) -> AssetSummary:
    """Parse a line-split asset response: title\\nsummary\\nentities."""
    lines = raw.strip().split("\n")
    title = lines[0].strip() if len(lines) > 0 else ""
    summary = lines[1].strip() if len(lines) > 1 else ""
    entities_str = lines[2].strip() if len(lines) > 2 else ""
    entities = _split_entities(entities_str)
    return AssetSummary(
        title=title or title_hint,
        summary=summary,
        entities=entities,
        kind="table",
    )


def _split_entities(value: Any) -> list[Entity]:
    """Parse an entities payload (§4.4) into typed ``Entity`` objects.

    Accepts the future structured form ``[{"text","type"}, ...]`` as well as the
    transitional free-form keyword string (``"a;b;c"``) so callers can switch to
    entity-aware prompts incrementally.
    """
    if isinstance(value, list):
        out: list[Entity] = []
        for item in value:
            if isinstance(item, dict) and str(item.get("text", "")).strip():
                out.append(Entity.from_dict(item))
            elif isinstance(item, str) and item.strip():
                out.append(Entity(text=item.strip()))
        return out
    if isinstance(value, str):
        normalized = value.replace("；", ";")
        return [
            Entity(text=part.strip())
            for part in normalized.split(";")
            if part.strip() and part.strip().lower() not in ("none", "null", "无", "empty")
        ]
    return []


def _call_llm(
    *,
    prompt: str,
    model: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    image_paths: list[str],
    usage_task: str,
    expect_json: bool,
    budget: Any | None,
    budget_pool: str,
    budget_stage: str | None,
    channel: Literal["text", "vision"] = "text",
) -> Any | None:
    """One text-or-vision call with budget accounting and a single JSON retry.

    Returns the parsed object (``expect_json``) or the raw string, or ``None`` on
    failure / exhausted budget. Budget is reserved before the call, committed on
    success, refunded on failure — matching the prior per-caller bookkeeping but
    in one place.

    ``channel`` selects BYOK text vs vision credentials when overrides are active.
    """
    from shared.services.ai.llm_overrides import resolve_text, resolve_vision

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        img_b64 = _read_image_b64(path)
        if img_b64 is not None:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                }
            )
    has_images = len(content_parts) > 1
    if image_paths and not has_images:
        # Every requested image failed to load; nothing to send.
        return None

    est = estimate_tokens(prompt) + _IMAGE_TOKEN_EST * max(1, len(image_paths))
    if budget is not None and not budget.try_reserve(
        budget_pool, est, stage=budget_stage
    ):
        logger.debug("[summary] budget exhausted for task {}", usage_task)
        return None

    api_kwargs: dict[str, Any] = {}
    if expect_json:
        api_kwargs["response_format"] = {"type": "json_object"}

    resolve = resolve_vision if channel == "vision" else resolve_text
    effective_model, api_key, api_url = resolve(model)
    if not effective_model:
        if budget is not None:
            budget.refund(budget_pool, est=est, stage=budget_stage)
        return None

    client = _client_mod.get_openai_client(
        model=effective_model,
        api_key=api_key,
        api_url=api_url,
    )
    for attempt in range(_MAX_JSON_RETRIES + 1):
        try:
            raw, usage = client.chat_completion_with_usage(
                messages=cast(Any, [{"role": "user", "content": content_parts}]),
                model=effective_model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                usage_task=usage_task,
                **api_kwargs,
            )
            if budget is not None:
                budget.commit(
                    budget_pool,
                    actual=usage.get("total_tokens", est),
                    est=est,
                    stage=budget_stage,
                )
                budget = None  # commit once even across the retry loop
            if not expect_json:
                if isinstance(raw, str) and raw.strip().lower() in ("null", "none"):
                    return None
                return raw
            parsed = eval_response(raw)
            if isinstance(parsed, (dict, list)):
                return parsed
            # eval_response fell back to a raw string → treat as a parse miss.
            if attempt < _MAX_JSON_RETRIES:
                logger.warning(
                    "[summary] JSON parse miss for {} (attempt {}), retrying",
                    usage_task,
                    attempt + 1,
                )
                continue
            return None
        except UnavailableException:
            if budget is not None:
                budget.refund(budget_pool, est=est, stage=budget_stage)
            if usage_task.startswith("page_memory."):
                raise
            return None
        except Exception as exc:
            logger.warning("[summary] LLM call failed for {}: {}", usage_task, exc)
            if budget is not None:
                budget.refund(budget_pool, est=est, stage=budget_stage)
            return None
    return None


@overload
def summarize(
    *,
    mode: Literal["asset"],
    text: str = ...,
    image_paths: list[str] | None = ...,
    summary_len: int = ...,
    max_keywords: int = ...,
    model: str | None = ...,
    usage_task: str | None = ...,
    budget: Any | None = ...,
    budget_pool: str = ...,
    budget_stage: str | None = ...,
    asset_title_hint: str = ...,
    prompt_task: str | None = ...,
    prompt_paras: dict[str, Any] | None = ...,
) -> AssetSummary:
    pass


@overload
def summarize(
    *,
    mode: Literal["text", "page"],
    text: str = ...,
    image_paths: list[str] | None = ...,
    summary_len: int = ...,
    max_keywords: int = ...,
    model: str | None = ...,
    usage_task: str | None = ...,
    budget: Any | None = ...,
    budget_pool: str = ...,
    budget_stage: str | None = ...,
    asset_title_hint: str = ...,
    prompt_task: str | None = ...,
    prompt_paras: dict[str, Any] | None = ...,
) -> BodySummary:
    pass


def summarize(
    *,
    mode: SummaryMode,
    text: str = "",
    image_paths: list[str] | None = None,
    summary_len: int = 200,
    max_keywords: int = 5,
    model: str | None = None,
    usage_task: str | None = None,
    # TODO(parse-budget-cleanup): no live caller passes a non-None budget after
    # PROFILE BudgetTracker removal. Drop budget/budget_pool/budget_stage once
    # remaining formats stop needing this duck-typed hook, or redirect any
    # future limit to token_tracking instead.
    budget: Any | None = None,
    budget_pool: str = "visual",
    budget_stage: str | None = None,
    asset_title_hint: str = "",
    prompt_task: str | None = None,
    prompt_paras: dict[str, Any] | None = None,
) -> BodySummary | AssetSummary:
    """Produce a summary for body content (text/page) or an asset.

    Parameters
    ----------
    mode:
        ``text`` / ``page`` → :class:`BodySummary`; ``asset`` → :class:`AssetSummary`.
    text:
        Raw text input (text mode) or optional context (page/asset modes).
    image_paths:
        Page or asset image(s). Required for ``page``/``asset`` modes that render
        from an image; ignored for plain ``text``.
    budget:
        Optional external reservation ledger. Visual calls reserve from
        ``budget_stage``.
    prompt_task / prompt_paras:
        Override the prompt used for the image-based page path. Lets a bounded
        node summary (``page-memory-node-summary`` with ``node_title`` /
        ``next_title``) reuse the same call mechanics. Ignored for text/asset.

    Returns the typed contract; callers map it onto their row. On any failure the
    engine returns an empty contract of the right type (no raw-text fallbacks —
    callers decide what to do with an empty result).
    """
    image_paths = [p for p in (image_paths or []) if p and os.path.exists(p)]

    if mode == "asset":
        return _summarize_asset(
            text=text,
            image_paths=image_paths,
            summary_len=summary_len,
            model=model,
            usage_task=usage_task or "summary.asset",
            budget=budget,
            budget_pool=budget_pool,
            budget_stage=budget_stage,
            asset_title_hint=asset_title_hint,
        )
    return _summarize_body(
        mode=mode,
        text=text,
        image_paths=image_paths,
        summary_len=summary_len,
        max_keywords=max_keywords,
        model=model,
        usage_task=usage_task or f"summary.{mode}",
        budget=budget,
        budget_pool=budget_pool,
        budget_stage=budget_stage,
        prompt_task=prompt_task,
        prompt_paras=prompt_paras,
    )


def _summarize_body(
    *,
    mode: SummaryMode,
    text: str,
    image_paths: list[str],
    summary_len: int,
    max_keywords: int,
    model: str | None,
    usage_task: str,
    budget: Any | None,
    budget_pool: str,
    budget_stage: str | None,
    prompt_task: str | None = None,
    prompt_paras: dict[str, Any] | None = None,
) -> BodySummary:
    kind = "page" if mode == "page" else "text"

    if image_paths:
        # Vision page tag (or a bounded node-summary override): image → {summary, ...}.
        task = prompt_task or "page-memory-vlm-tag"
        paras = prompt_paras or {"max_tokens": 600}
        prompt, temperature, top_p, max_tokens = build_prompt(task, "", "", paras=paras)
        resolved_model = model or os.environ.get("IMAGE_MODEL")
        if not resolved_model:
            return BodySummary(kind=kind)
        parsed = _call_llm(
            prompt=prompt,
            model=resolved_model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            image_paths=image_paths,
            usage_task=usage_task,
            expect_json=True,
            budget=budget,
            budget_pool=budget_pool,
            budget_stage=budget_stage,
            channel="vision",
        )
    else:
        # Text summary: the shared summary-full prompt with deterministic lang lock.
        if not text.strip():
            return BodySummary(kind=kind)
        detected_lang = _detect_text_language(text)
        prompt, temperature, top_p, max_tokens = build_prompt(
            "summary-full",
            text,
            "",
            paras={
                "max_tokens": summary_len,
                "kw_num": max_keywords,
                "lang": detected_lang,
            },
        )
        resolved_model = model or os.environ.get("NORMOL_MODEL", "deepseek-v4-flash")
        parsed = _call_llm(
            prompt=prompt,
            model=resolved_model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            image_paths=[],
            usage_task=usage_task,
            expect_json=True,
            budget=budget,
            budget_pool="plan",
            budget_stage=None,
            channel="text",
        )

    if not isinstance(parsed, dict):
        return BodySummary(kind=kind)
    entities = _split_entities(parsed.get("entities") or parsed.get("keywords"))
    return BodySummary(
        summary=str(parsed.get("summary", "")).strip(),
        entities=entities,
        kind=kind,
    )


def _summarize_asset(
    *,
    text: str,
    image_paths: list[str],
    summary_len: int,
    model: str | None,
    usage_task: str,
    budget: Any | None,
    budget_pool: str,
    budget_stage: str | None,
    asset_title_hint: str,
) -> AssetSummary:
    if not image_paths and not text.strip():
        return AssetSummary(title=asset_title_hint)

    if not image_paths:
        # Text-based asset (e.g. an HTML table): use line-split format to avoid
        # JSON parse failures with flash-tier models.
        detected_lang = _detect_text_language(text)
        prompt, temperature, top_p, max_tokens = build_prompt(
            "summary-asset-linesplit",
            text,
            "",
            paras={"max_tokens": summary_len, "kw_num": 5, "lang": detected_lang},
        )
        resolved_model = model or os.environ.get("NORMOL_MODEL", "deepseek-v4-flash")
        raw = _call_llm(
            prompt=prompt,
            model=resolved_model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            image_paths=[],
            usage_task=usage_task,
            expect_json=False,
            budget=budget,
            budget_pool="plan",
            budget_stage=None,
            channel="text",
        )
        if isinstance(raw, str) and raw.strip():
            return _parse_linesplit_asset(raw, asset_title_hint)
        return AssetSummary(title=asset_title_hint, kind="table")

    # Image-based asset: the shared per-type image prompt returns one strict JSON
    # object (title + summary + entities).
    prompt, temperature, top_p, max_tokens = build_prompt(
        "summary-images",
        text,
        "",
        paras={"max_tokens": summary_len},
    )
    resolved_model = model or os.environ.get("IMAGE_MODEL")
    if not resolved_model:
        return AssetSummary(title=asset_title_hint)
    parsed = _call_llm(
        prompt=prompt,
        model=resolved_model,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        image_paths=image_paths,
        usage_task=usage_task,
        expect_json=True,
        budget=budget,
        budget_pool=budget_pool,
        budget_stage=budget_stage,
        channel="vision",
    )
    if isinstance(parsed, dict):
        return AssetSummary(
            title=str(parsed.get("title", "")).strip() or asset_title_hint,
            summary=str(parsed.get("summary", "")).strip(),
            entities=_split_entities(parsed.get("entities") or parsed.get("keywords")),
            kind="figure",
        )
    return AssetSummary(title=asset_title_hint)


def transcribe(
    *,
    image_paths: list[str],
    model: str | None = None,
    max_tokens: int = 1500,
    usage_task: str = "summary.transcribe",
    budget: Any | None = None,
    budget_pool: str = "visual",
    budget_stage: str | None = None,
) -> str:
    """Single OCR primitive (§4.2): transcribe page/image text verbatim.

    Replaces the former ``ocr-image`` (image track) and ``page-memory-vlm-ocr``
    (page track) duplicates. Returns the transcribed text, or ``""`` when nothing
    is readable / the call fails.
    """
    image_paths = [p for p in image_paths if p and os.path.exists(p)]
    if not image_paths:
        return ""
    resolved_model = model or os.environ.get("IMAGE_MODEL")
    if not resolved_model:
        return ""
    prompt, temperature, top_p, _max_tokens = build_prompt(
        "transcribe", "", "", paras={"max_tokens": max_tokens}
    )
    parsed = _call_llm(
        prompt=prompt,
        model=resolved_model,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        image_paths=image_paths,
        usage_task=usage_task,
        expect_json=True,
        budget=budget,
        budget_pool=budget_pool,
        budget_stage=budget_stage,
        channel="vision",
    )
    if isinstance(parsed, dict):
        return str(parsed.get("text", "")).strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return ""
