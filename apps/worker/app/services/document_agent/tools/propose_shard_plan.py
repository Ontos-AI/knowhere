"""Shard planning for the Phase 1 split agent."""

from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256
from typing import Any

from app.services.document_agent.manifest import (
    GlobalSignals,
    ShardManifest,
    ShardSignal,
    SpecialPage,
)
from app.services.document_agent.tools.llm_json import extract_json_object
from loguru import logger

PROMPT = """You are planning PDF shards for a downstream parser.
Return ONLY valid json.

Goal:
- Cover every page from 1 to page_count exactly once.
- Prefer shards as close to max_pages_per_shard pages as possible without exceeding it.
- Do not cut through obvious table-heavy ranges, continuous image/landscape blocks, or likely TOC pages.
- Align cuts near safer normal/sparse pages when possible.

JSON schema:
{
  "cuts": [
    {"start": 1, "end": 199, "predominant_kind": "text_dense", "rationale": "short reason"}
  ],
  "global_notes": "short summary"
}

Allowed predominant_kind values: text_dense, table_heavy, image_heavy, mixed, landscape_block, toc, sparse.
"""

ALLOWED_PREDOMINANT = {
    "text_dense",
    "table_heavy",
    "image_heavy",
    "mixed",
    "landscape_block",
    "toc",
    "sparse",
}


def _special_by_page(classifications: dict[str, Any]) -> dict[int, dict[str, Any]]:
    by_page = {}
    for item in classifications.get("pages", []) or []:
        if not isinstance(item, dict):
            continue
        page = int(item.get("page") or 0)
        if page > 0:
            by_page[page] = item
    return by_page


def _kind_for_range(start: int, end: int, by_page: dict[int, dict[str, Any]]) -> str:
    kinds = [
        str(item.get("special_kind") or item.get("kind") or "normal")
        for page, item in by_page.items()
        if start <= page <= end
    ]
    if not kinds:
        return "text_dense"
    counts = Counter(kinds)
    special_total = sum(count for kind, count in counts.items() if kind != "normal")
    if special_total == 0:
        return "text_dense"
    top_kind, top_count = counts.most_common(1)[0]
    if top_kind in {"table_heavy"}:
        return "table_heavy"
    if top_kind in {"image_heavy", "single_image"}:
        return "image_heavy"
    if top_kind == "landscape":
        return "landscape_block"
    if top_kind == "toc":
        return "toc"
    if top_kind in {"blank", "sparse"} and top_count >= max(1, (end - start + 1) // 2):
        return "sparse"
    return "mixed"


def _fallback_cuts(
    page_count: int,
    *,
    max_pages_per_shard: int,
    classifications: dict[str, Any],
) -> list[dict[str, Any]]:
    by_page = _special_by_page(classifications)
    cuts = []
    start = 1
    while start <= page_count:
        target_end = min(start + max_pages_per_shard - 1, page_count)
        end = target_end
        if target_end < page_count:
            # Prefer a safe boundary close to the shard limit without exceeding it.
            target_kind = str(by_page.get(target_end, {}).get("special_kind") or "normal")
            if target_kind not in {"blank", "sparse", "normal"}:
                window_start = max(start, target_end - 5)
                candidates = []
                for page in range(window_start, target_end):
                    kind = str(by_page.get(page, {}).get("special_kind") or "normal")
                    priority = {"blank": 0, "sparse": 1, "normal": 2}.get(kind)
                    if priority is not None:
                        candidates.append((abs(page - target_end), priority, page))
                if candidates:
                    end = min(candidates)[2]
        cuts.append(
            {
                "start": start,
                "end": end,
                "predominant_kind": _kind_for_range(start, end, by_page),
                "rationale": "deterministic fallback cut near max shard size",
            }
        )
        start = end + 1
    return cuts


def _normalize_cuts(
    cuts: list[dict[str, Any]],
    *,
    page_count: int,
    max_pages_per_shard: int,
    classifications: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = []
    expected = 1
    for raw in cuts:
        if not isinstance(raw, dict):
            continue
        start = int(raw.get("start") or raw.get("page_start") or 0)
        end = int(raw.get("end") or raw.get("page_end") or 0)
        if start != expected or end < start or end > page_count:
            raise ValueError("LLM shard cuts are not contiguous")
        kind = str(raw.get("predominant_kind") or "mixed")
        if kind not in ALLOWED_PREDOMINANT:
            kind = "mixed"
        normalized.append(
            {
                "start": start,
                "end": end,
                "predominant_kind": kind,
                "rationale": str(raw.get("rationale") or "")[:500],
            }
        )
        expected = end + 1
    if expected != page_count + 1:
        raise ValueError("LLM shard cuts do not cover all pages")
    if not normalized:
        raise ValueError("empty LLM shard cuts")
    # Avoid accepting pathological single giant cuts except naturally small docs.
    if page_count > max_pages_per_shard * 2 and any(
        cut["end"] - cut["start"] + 1 > max_pages_per_shard * 2
        for cut in normalized
    ):
        raise ValueError("LLM shard cut exceeds hard tolerance")
    return normalized


def _build_global_signals(
    *,
    sampled_pages: list[dict[str, Any]],
    classifications: dict[str, Any],
) -> GlobalSignals:
    pages = classifications.get("pages", []) or []
    toc_pages = [
        int(page.get("page") or 0)
        for page in pages
        if str(page.get("special_kind") or page.get("kind")) == "toc"
    ]
    sample_size = len(sampled_pages)
    if sample_size <= 0:
        return GlobalSignals(
            has_toc=bool(toc_pages),
            toc_pages=toc_pages,
            landscape_ratio=0.0,
            table_page_ratio=0.0,
            image_page_ratio=0.0,
            sample_size=0,
            notes=str(classifications.get("global_notes") or ""),
        )
    landscape_count = sum(1 for page in sampled_pages if page.get("orientation") == "landscape")
    table_count = sum(
        1
        for page in pages
        if str(page.get("special_kind") or page.get("kind")) == "table_heavy"
    )
    image_count = sum(
        1
        for page in pages
        if str(page.get("special_kind") or page.get("kind")) in {"image_heavy", "single_image"}
    )
    return GlobalSignals(
        has_toc=bool(toc_pages),
        toc_pages=toc_pages,
        landscape_ratio=landscape_count / sample_size,
        table_page_ratio=table_count / sample_size,
        image_page_ratio=image_count / sample_size,
        sample_size=sample_size,
        notes=str(classifications.get("global_notes") or ""),
    )


def build_manifest_from_cuts(
    *,
    file_uri: str,
    job_id: str,
    page_count: int,
    sampled_pages: list[dict[str, Any]],
    classifications: dict[str, Any],
    cuts: list[dict[str, Any]],
    decision_log_ref: str = "local-debug",
) -> ShardManifest:
    by_page = _special_by_page(classifications)
    shards = []
    for cut in cuts:
        start = int(cut["start"])
        end = int(cut["end"])
        special_pages = []
        for page in range(start, end + 1):
            item = by_page.get(page)
            if not item:
                continue
            kind = str(item.get("special_kind") or item.get("kind") or "normal")
            if kind == "normal":
                continue
            special_pages.append(
                SpecialPage(
                    page=page,
                    kind=kind,  # type: ignore[arg-type]
                    confidence=float(item.get("confidence") or 0.0),
                    note=str(item.get("note") or ""),
                )
            )
        shards.append(
            ShardSignal(
                page_start=start,
                page_end=end,
                page_offset=start - 1,
                predominant_kind=cut["predominant_kind"],
                special_pages=special_pages,
                cut_rationale=str(cut.get("rationale") or ""),
            )
        )

    manifest = ShardManifest(
        job_id=job_id,
        file_uri=file_uri,
        file_sha=_hash_file(file_uri),
        page_count=page_count,
        shard_count=len(shards),
        shards=shards,
        global_signals=_build_global_signals(
            sampled_pages=sampled_pages,
            classifications=classifications,
        ),
        decision_log_ref=decision_log_ref,
    )
    manifest.validate()
    return manifest


def _hash_file(file_uri: str) -> str:
    digest = sha256()
    try:
        with open(file_uri, "rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return sha256(file_uri.encode("utf-8")).hexdigest()


def propose_shard_plan(
    *,
    file_uri: str,
    job_id: str,
    page_count: int,
    sampled_pages: list[dict[str, Any]],
    classifications: dict[str, Any],
    max_pages_per_shard: int = 199,
    model: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Produce a validated shard proposal and manifest."""
    max_pages_per_shard = max(1, int(max_pages_per_shard))

    cuts: list[dict[str, Any]]
    raw_response = ""
    if use_llm and page_count > max_pages_per_shard:
        try:
            from shared.core.config import settings
            from shared.utils.OpenAICompatibleClientSync import get_openai_client

            effective_model = model or settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL
            client = get_openai_client(model=effective_model)
            payload = {
                "page_count": page_count,
                "max_pages_per_shard": max_pages_per_shard,
                "sampled_pages": sampled_pages,
                "page_classifications": classifications,
            }
            raw_response = client.chat_completion(
                messages=[
                    {"role": "system", "content": PROMPT},
                    {
                        "role": "user",
                        "content": "Propose a shard plan as json:\n"
                        + json.dumps(payload, ensure_ascii=False),
                    },
                ],
                model=effective_model,
                temperature=0.0,
                max_tokens=2200,
                response_format={"type": "json_object"},
            )
            data = extract_json_object(raw_response)
            cuts = _normalize_cuts(
                list(data.get("cuts") or []),
                page_count=page_count,
                max_pages_per_shard=max_pages_per_shard,
                classifications=classifications,
            )
            if data.get("global_notes") and not classifications.get("global_notes"):
                classifications = {**classifications, "global_notes": data.get("global_notes")}
        except Exception as exc:
            logger.warning(
                f"[document_agent.propose_shard_plan] LLM planning failed, "
                f"using fallback: {exc}"
            )
            cuts = _fallback_cuts(
                page_count,
                max_pages_per_shard=max_pages_per_shard,
                classifications=classifications,
            )
    else:
        cuts = _fallback_cuts(
            page_count,
            max_pages_per_shard=max_pages_per_shard,
            classifications=classifications,
        )

    manifest = build_manifest_from_cuts(
        file_uri=file_uri,
        job_id=job_id,
        page_count=page_count,
        sampled_pages=sampled_pages,
        classifications=classifications,
        cuts=cuts,
    )
    return {
        "cuts": cuts,
        "manifest": manifest,
        "manifest_dict": manifest.to_dict(),
        "raw_response": raw_response,
    }
