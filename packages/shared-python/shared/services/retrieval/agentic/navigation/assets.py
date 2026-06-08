from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy import func as sa_func
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agentic.core.budget import BudgetExceeded
from shared.services.retrieval.hydration.assets import build_retrieval_asset_url_map
from shared.services.retrieval.llm_adapter import LLMFn
from shared.utils.token_estimate import estimate_tokens


def build_connected_owner_map(text_chunks: list[dict[str, Any]]) -> dict[str, str]:
    owner_map: dict[str, str] = {}
    for chunk in text_chunks:
        if (chunk.get("chunk_type") or "text") != "text":
            continue
        section_path = chunk.get("section_path") or ""
        if not section_path:
            continue
        metadata = chunk.get("chunk_metadata") or {}
        if not isinstance(metadata, dict):
            continue
        for conn in metadata.get("connect_to") or []:
            if not isinstance(conn, dict):
                continue
            target_id = str(conn.get("target") or "").strip()
            if target_id and target_id not in owner_map:
                owner_map[target_id] = section_path
    return owner_map


async def _load_scope_sections(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_paths: list[str],
) -> list[tuple[str, str]]:
    section_stmt = (
        select(DocumentSection.section_id, DocumentSection.section_path)
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
    )
    if scope_paths:
        scope_filters = []
        for scope in scope_paths:
            scope_filters.append(DocumentSection.section_path == scope)
            scope_filters.append(DocumentSection.section_path.like(f"{scope} / %"))
        section_stmt = section_stmt.where(or_(*scope_filters))
    rows = (await db.execute(section_stmt)).all()
    return [(section_id, section_path or "") for section_id, section_path in rows]


async def count_assets_under_scope(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_paths: list[str],
) -> tuple[int, int]:
    section_rows = await _load_scope_sections(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        scope_paths=scope_paths,
    )
    all_section_ids = [section_id for section_id, _section_path in section_rows]

    if not all_section_ids:
        return 0, 0

    count_stmt = (
        select(
            DocumentChunk.chunk_type,
            sa_func.count(DocumentChunk.id),
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.section_id.in_(all_section_ids))
        .where(DocumentChunk.chunk_type.in_(["image", "table"]))
        .group_by(DocumentChunk.chunk_type)
    )
    count_result = await db.execute(count_stmt)

    total_images = 0
    total_tables = 0
    for chunk_type, count in count_result.all():
        if chunk_type == "image":
            total_images = count
        elif chunk_type == "table":
            total_tables = count
    return total_images, total_tables


async def resolve_root_asset_owners(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    chunks: list[dict[str, Any]],
) -> dict[str, str]:
    root_asset_ids = [
        str(chunk.get("chunk_id") or "")
        for chunk in chunks
        if not chunk.get("owner_section_path")
        and (chunk.get("section_path") or "") == "Root"
        and (chunk.get("chunk_type") or "").lower() in ("image", "table")
        and chunk.get("chunk_id")
    ]
    if not root_asset_ids:
        return {}

    root_asset_set = set(root_asset_ids)
    text_stmt = (
        select(
            DocumentChunk.chunk_metadata,
            DocumentSection.section_path,
        )
        .outerjoin(
            DocumentSection,
            DocumentSection.section_id == DocumentChunk.section_id,
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.chunk_type == "text")
    )
    result = await db.execute(text_stmt)

    owner_map: dict[str, str] = {}
    for metadata, section_path in result.all():
        if not isinstance(metadata, dict) or not section_path:
            continue
        for conn in metadata.get("connect_to") or []:
            if not isinstance(conn, dict):
                continue
            target_id = str(conn.get("target") or "").strip()
            if target_id in root_asset_set and target_id not in owner_map:
                owner_map[target_id] = section_path

    if owner_map:
        logger.info(
            f"  resolve_root_asset_owners: resolved {len(owner_map)}/{len(root_asset_ids)} "
            f"Root assets to their owner sections"
        )
    return owner_map


async def asset_filter_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None,
    asset_type: str,
) -> list[dict[str, Any]]:
    t0 = time.monotonic()
    try:
        scope_list = (
            scope_path
            if isinstance(scope_path, list)
            else [scope_path]
            if scope_path
            else []
        )

        section_rows = await _load_scope_sections(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            scope_paths=scope_list,
        )
        section_ids = {row[0] for row in section_rows}

        if not section_ids:
            logger.info(f"  asset_filter_step: no sections found under scope={scope_path}")
            return []

        section_path_by_id = {
            section_id: section_path for section_id, section_path in section_rows
        }
        asset_rows = (
            await db.execute(
                select(
                    DocumentChunk.chunk_id,
                    DocumentChunk.chunk_type,
                    DocumentChunk.content,
                    DocumentChunk.file_path,
                    DocumentChunk.section_id,
                    DocumentChunk.source_chunk_path,
                    DocumentChunk.chunk_metadata,
                    DocumentChunk.sort_order,
                    DocumentChunk.job_result_id,
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentChunk.section_id.in_(list(section_ids)))
                .where(DocumentChunk.chunk_type == asset_type)
                .order_by(DocumentChunk.sort_order)
            )
        ).all()

        text_rows = (
            await db.execute(
                select(
                    DocumentChunk.section_id,
                    DocumentChunk.chunk_type,
                    DocumentChunk.chunk_metadata,
                    DocumentChunk.source_chunk_path,
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentChunk.section_id.in_(list(section_ids)))
                .where(DocumentChunk.chunk_type == "text")
            )
        ).all()
        text_row_dicts = [
            {
                "chunk_type": chunk_type,
                "chunk_metadata": metadata or {},
                "section_id": section_id,
                "section_path": section_path_by_id.get(section_id, ""),
                "source_chunk_path": source_chunk_path,
            }
            for section_id, chunk_type, metadata, source_chunk_path in text_rows
        ]
        owner_by_target_id = build_connected_owner_map(text_row_dicts)

        connected_target_ids: set[str] = set(owner_by_target_id.keys())
        if connected_target_ids:
            connected_rows = (
                await db.execute(
                    select(
                        DocumentChunk.chunk_id,
                        DocumentChunk.chunk_type,
                        DocumentChunk.content,
                        DocumentChunk.file_path,
                        DocumentChunk.section_id,
                        DocumentChunk.source_chunk_path,
                        DocumentChunk.chunk_metadata,
                        DocumentChunk.sort_order,
                        DocumentChunk.job_result_id,
                    )
                    .where(DocumentChunk.document_id == document_id)
                    .where(DocumentChunk.job_result_id == job_result_id)
                    .where(DocumentChunk.chunk_id.in_(list(connected_target_ids)))
                    .where(DocumentChunk.chunk_type == asset_type)
                    .order_by(DocumentChunk.sort_order)
                )
            ).all()
        else:
            connected_rows = []

        job_id = (
            await db.execute(select(JobResult.job_id).where(JobResult.id == job_result_id))
        ).scalar() or ""
        seen_ids: set[str] = set()
        chunks: list[dict[str, Any]] = []
        for row in list(asset_rows) + list(connected_rows):
            chunk_id = row[0]
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)

            owner_section_path = owner_by_target_id.get(chunk_id)
            if not owner_section_path:
                own_section_path = section_path_by_id.get(row[4])
                if own_section_path and own_section_path == "Root":
                    logger.warning(
                        "  asset_filter_step: rejecting root-level owner fallback "
                        f"chunk_id={chunk_id} section_path={own_section_path}"
                    )
                    own_section_path = None
                owner_section_path = own_section_path

            if not owner_section_path:
                logger.warning(
                    f"  asset_filter_step unresolved owner: chunk_id={chunk_id} "
                    f"file_path={row[3]} scope={scope_path or 'root'}"
                )
                continue

            chunks.append(
                {
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "chunk_type": row[1],
                    "content": row[2],
                    "file_path": row[3],
                    "section_id": row[4],
                    "section_path": owner_section_path,
                    "owner_section_path": owner_section_path,
                    "source_chunk_path": row[5],
                    "chunk_metadata": row[6] or {},
                    "sort_order": row[7],
                    "job_result_id": job_result_id,
                    "job_id": job_id,
                }
            )

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"  asset_filter_step scope={scope_path or 'root'} "
            f"type={asset_type}: {len(chunks)} chunks found, {latency}ms"
        )
        return chunks

    except Exception as exc:
        logger.error(f"  asset_filter_step failed: {exc}")
        return []


async def search_assets_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None,
    asset_type: str,
    query: str,
    llm_fn: LLMFn,
    vlm_fn: LLMFn | None = None,
) -> dict[str, Any]:
    """LLM-filtered asset search.

    For **tables**: uses text LLM with summary descriptions (unchanged).
    For **images**: generates presigned S3 URLs via
    ``build_retrieval_asset_url_map`` and sends them to the VLM
    (``vlm_fn``) for visual relevance judgment.
    """
    t0 = time.monotonic()

    all_assets = await asset_filter_step(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        scope_path=scope_path,
        asset_type=asset_type,
    )
    if not all_assets:
        logger.info(f"  search_assets_step: no {asset_type} assets under scope={scope_path}")
        return {
            "status": "empty",
            "matched_assets": [],
            "verdicts": [],
            "candidate_count": 0,
        }

    # Build lookup by chunk_id
    asset_by_id: dict[str, dict[str, Any]] = {}
    for asset in all_assets:
        chunk_id = str(asset.get("chunk_id") or "")
        if chunk_id:
            asset_by_id[chunk_id] = asset

    if not asset_by_id:
        return {
            "status": "empty",
            "matched_assets": [],
            "verdicts": [],
            "candidate_count": 0,
        }

    status_detail = ""
    status = "empty"

    # ── Route by asset type ──────────────────────────────────────────
    if asset_type == "image":
        if vlm_fn is None:
            logger.info("  search_assets_step: VLM unavailable for image search")
            selected_ids = await _search_assets_via_text_llm(
                query=query,
                asset_type=asset_type,
                assets=list(asset_by_id.values()),
                llm_fn=llm_fn,
            )
            status = "fallback_matched" if selected_ids else "fallback_empty"
            status_detail = "vlm_unavailable_text_fallback"
        else:
            selected_ids, vlm_error = await _search_images_via_vlm(
                query=query,
                assets=list(asset_by_id.values()),
                vlm_fn=vlm_fn,
            )
            if vlm_error:
                logger.info(
                    "  search_assets_step: VLM image search fell back to text "
                    f"filter, reason={vlm_error}"
                )
                selected_ids = await _search_assets_via_text_llm(
                    query=query,
                    asset_type=asset_type,
                    assets=list(asset_by_id.values()),
                    llm_fn=llm_fn,
                )
                status = "fallback_matched" if selected_ids else "fallback_empty"
                status_detail = "vlm_failed_text_fallback"
            else:
                status = "matched" if selected_ids else "empty"
    else:
        selected_ids = await _search_assets_via_text_llm(
            query=query,
            asset_type=asset_type,
            assets=list(asset_by_id.values()),
            llm_fn=llm_fn,
        )
        status = "matched" if selected_ids else "empty"

    selected_id_set = {str(cid) for cid in selected_ids}
    matched_assets = [asset_by_id[cid] for cid in selected_ids if cid in asset_by_id]
    verdicts = [
        _asset_verdict(
            asset,
            relevant=str(asset.get("chunk_id") or "") in selected_id_set,
            reason=(
                _selected_reason(status)
                if str(asset.get("chunk_id") or "") in selected_id_set
                else _not_selected_reason(status)
            ),
        )
        for asset in asset_by_id.values()
    ]

    latency = int((time.monotonic() - t0) * 1000)
    logger.info(
        f"  search_assets_step query=\"{query}\" type={asset_type}: "
        f"{len(matched_assets)}/{len(all_assets)} assets matched, {latency}ms"
    )
    return {
        "status": status,
        "status_detail": status_detail,
        "matched_assets": matched_assets,
        "verdicts": verdicts,
        "candidate_count": len(asset_by_id),
        "latency_ms": latency,
    }


def _asset_verdict(
    asset: dict[str, Any],
    *,
    relevant: bool,
    reason: str,
) -> dict[str, Any]:
    metadata = asset.get("chunk_metadata") or {}
    summary = metadata.get("summary", "")
    return {
        "chunk_id": asset.get("chunk_id", ""),
        "file_path": asset.get("file_path", ""),
        "section_path": asset.get("owner_section_path") or asset.get("section_path", ""),
        "summary": summary,
        "relevant": relevant,
        "reason": reason,
    }


def _selected_reason(status: str) -> str:
    if status.startswith("fallback_"):
        return "selected_by_text_fallback"
    return "selected_by_asset_inspector"


def _not_selected_reason(status: str) -> str:
    if status.startswith("fallback_"):
        return "not_selected_by_text_fallback"
    return "not_selected_by_asset_inspector"


async def _search_assets_via_text_llm(
    *,
    query: str,
    asset_type: str,
    assets: list[dict[str, Any]],
    llm_fn: LLMFn,
) -> list[str]:
    """Text-based LLM filtering for table assets."""
    candidates_for_llm, valid_ids, id_to_chunk_id = _project_assets_for_text_filter(
        query=query,
        asset_type=asset_type,
        assets=assets,
    )

    prompt = _format_asset_filter_prompt(query, asset_type, candidates_for_llm)
    try:
        response = await llm_fn(prompt)
        selected_ids = _parse_asset_filter_response(response, valid_ids)
        return [
            id_to_chunk_id[row_id]
            for row_id in selected_ids
            if row_id in id_to_chunk_id
        ]
    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.warning(f"  _search_assets_via_text_llm failed: {exc}")
        return []


def _project_assets_for_text_filter(
    *,
    query: str,
    asset_type: str,
    assets: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], set[str], dict[str, str]]:
    """Project assets into a prompt-sized text view.

    Stable row identifiers are shown to the model. Owner paths stay internal:
    reconciliation and hydration use the original asset rows, not prompt text.
    Descriptive text is reduced only when the complete prompt would exceed the
    navigation planning budget envelope.
    """
    projected: list[dict[str, str]] = []
    valid_ids: set[str] = set()
    id_to_chunk_id: dict[str, str] = {}
    for index, asset in enumerate(assets, start=1):
        chunk_id = str(asset.get("chunk_id") or "")
        if not chunk_id:
            continue
        row_id = f"I{index}" if asset_type == "image" else f"T{index}"
        metadata = asset.get("chunk_metadata") or {}
        summary = str(metadata.get("summary") or "").strip()
        file_path = str(asset.get("file_path") or "")
        content = str(asset.get("content") or "").strip()
        description = summary or (content if asset_type == "table" else "")
        projected.append({
            "id": row_id,
            "file": file_path,
            "desc": description,
        })
        valid_ids.add(row_id)
        id_to_chunk_id[row_id] = chunk_id

    if not projected:
        return projected, valid_ids, id_to_chunk_id

    prompt = _format_asset_filter_prompt(query, asset_type, projected)
    prompt_budget = _asset_filter_prompt_budget()
    if estimate_tokens(prompt) <= prompt_budget:
        return projected, valid_ids, id_to_chunk_id

    structural_prompt = _format_asset_filter_prompt(
        query,
        asset_type,
        [
            {
                "id": item["id"],
                "file": item["file"],
                "desc": "",
            }
            for item in projected
        ],
    )
    structural_tokens = estimate_tokens(structural_prompt)
    desc_budget = max(prompt_budget - structural_tokens, len(projected))
    per_item_desc_tokens = max(desc_budget // len(projected), 1)
    compacted = [
        {
            "id": item["id"],
            "file": item["file"],
            "desc": _fit_text_to_token_budget(item["desc"], per_item_desc_tokens),
        }
        for item in projected
    ]
    return compacted, valid_ids, id_to_chunk_id


def _asset_filter_prompt_budget() -> int:
    from shared.services.retrieval.agentic.core.runtime import build_config_from_env

    config = build_config_from_env()
    planning_capacity = int(
        max(config.token_budget_total - config.bootstrap_budget, 0)
        * config.planning_ratio
    )
    return max(planning_capacity, 1)


def _fit_text_to_token_budget(text: str, token_budget: int) -> str:
    text = text.strip()
    if not text or estimate_tokens(text) <= token_budget:
        return text
    words = text.split()
    if len(words) > 1:
        kept: list[str] = []
        for word in words:
            candidate = " ".join([*kept, word])
            if estimate_tokens(candidate) > token_budget:
                break
            kept.append(word)
        return " ".join(kept).strip()

    lo = 0
    hi = len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].strip()
        if estimate_tokens(candidate) <= token_budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


async def _search_images_via_vlm(
    *,
    query: str,
    assets: list[dict[str, Any]],
    vlm_fn: LLMFn,
) -> tuple[list[str], str | None]:
    """VLM-based image search with presigned S3 URLs.

    Generates presigned URLs for each image asset, builds a multimodal
    prompt with image_url blocks, and asks the VLM to select relevant ones.
    """
    url_map = await build_retrieval_asset_url_map(
        assets, log_context="search_images_vlm",
    )

    # Only include images that have valid URLs.
    candidates: list[tuple[str, str, str]] = []  # (row_id, file_path, url)
    valid_ids: set[str] = set()
    id_to_chunk_id: dict[str, str] = {}
    for index, asset in enumerate(assets, start=1):
        chunk_id = str(asset.get("chunk_id") or "")
        url = url_map.get(chunk_id)
        if not url:
            continue
        row_id = f"I{index}"
        file_path = asset.get("file_path") or ""
        candidates.append((row_id, file_path, url))
        valid_ids.add(row_id)
        id_to_chunk_id[row_id] = chunk_id

    if not candidates:
        logger.info("  _search_images_via_vlm: no presigned URLs available, skipping")
        return [], "no_presigned_urls"

    messages = _format_vlm_image_filter_messages(query, candidates)
    try:
        response = await vlm_fn(messages)
        selected_ids = _parse_asset_filter_response(response, valid_ids)
        return [
            id_to_chunk_id[row_id]
            for row_id in selected_ids
            if row_id in id_to_chunk_id
        ], None
    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.warning(f"  _search_images_via_vlm failed: {exc}")
        return [], str(exc)


def _format_asset_filter_prompt(
    query: str,
    asset_type: str,
    candidates: list[dict[str, str]],
) -> str:
    """Build the text LLM prompt for table asset filtering."""
    type_label = "images" if asset_type == "image" else "tables"
    items_text = _format_asset_candidates_table(candidates)
    example_id = candidates[0]["id"] if candidates else ("I1" if asset_type == "image" else "T1")
    return (
        f"You are an asset relevance filter.\n\n"
        f"Original user query: {query}\n\n"
        f"Below are {len(candidates)} {type_label} from a document. "
        f"Select ONLY assets that directly satisfy the user's query.\n\n"
        f"Selection policy:\n"
        f"- Match the requested asset type and the requested subject. "
        f"Being an image/chart/table is not enough.\n"
        f"- Do not select assets only because they belong to the same broad "
        f"domain as the query.\n"
        f"- Do not broaden specific market, instrument, company, metric, or "
        f"entity terms. Neighboring topics are not matches unless the candidate "
        f"explicitly connects them to the requested subject.\n"
        f"- Treat words like \"all\" as all relevant assets, not all visible "
        f"candidates.\n"
        f"- If the file name, summary, or content signal does not "
        f"directly support relevance, leave it out.\n"
        f"- If uncertain, do not select the asset.\n\n"
        f"=== Candidate {type_label.title()} ===\n{items_text}\n=== End ===\n\n"
        f"Return ONLY a JSON array of matching row IDs, e.g.: "
        f'["{example_id}"]\n'
        f"If none are relevant, return an empty array: []\n"
        f"Do not include any explanation."
    )


def _format_asset_candidates_table(candidates: list[dict[str, str]]) -> str:
    lines = [
        "| ID | File | Summary / content signal |",
        "|---|---|---|",
    ]
    for candidate in candidates:
        lines.append(
            "| "
            + " | ".join([
                _markdown_cell(candidate.get("id", "")),
                _markdown_cell(candidate.get("file", "")),
                _markdown_cell(candidate.get("desc", "")),
            ])
            + " |"
        )
    return "\n".join(lines)


def _markdown_cell(value: str) -> str:
    return (
        str(value or "")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("|", "\\|")
        .strip()
    )


def _format_vlm_image_filter_messages(
    query: str,
    candidates: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Build multimodal VLM messages with inline image URLs.

    Each candidate is (row_id, file_path, presigned_url).
    The VLM sees the actual images and decides relevance.
    """
    content_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"You are an image relevance filter.\n\n"
                f"Original user query: {query}\n\n"
                f"Below are {len(candidates)} images from a document. "
                f"Look at each image and select ONLY images that directly "
                f"satisfy the user's query.\n\n"
                f"Selection policy:\n"
                f"- Match both the requested visual type and requested subject.\n"
                f"- Do not select images only because they are charts or from "
                f"the same broad domain.\n"
                f"- Do not broaden specific market, instrument, company, metric, "
                f"or entity terms. Neighboring topics are not matches unless "
                f"the image explicitly connects them to the requested subject.\n"
                f"- Treat words like \"all\" as all relevant images, not all "
                f"visible candidates.\n"
                f"- If uncertain, do not select the image.\n\n"
            ),
        },
    ]

    for row_id, file_path, url in candidates:
        content_parts.append({
            "type": "text",
            "text": f'Image {row_id} file="{file_path}":',
        })
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": url},
        })

    content_parts.append({
        "type": "text",
        "text": (
            f"\n\nReturn ONLY a JSON array of matching image row IDs, e.g.: "
            f'["{candidates[0][0]}"]\n'
            f"If none are relevant, return an empty array: []\n"
            f"Do not include any explanation."
        ),
    })

    return [{"role": "user", "content": content_parts}]


def _parse_asset_filter_response(
    text: str,
    valid_ids: set[str],
) -> list[str]:
    """Parse LLM response for asset filter and keep valid row IDs."""
    import json
    import re

    text = text.strip()

    # Try direct JSON parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [str(item) for item in result if str(item) in valid_ids]
    except (ValueError, json.JSONDecodeError):
        pass

    # Try extracting from code fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            result = json.loads(fence_match.group(1).strip())
            if isinstance(result, list):
                return [str(item) for item in result if str(item) in valid_ids]
        except (ValueError, json.JSONDecodeError):
            pass

    # Try finding any JSON array
    bracket_match = re.search(r"\[.*?\]", text, re.DOTALL)
    if bracket_match:
        try:
            result = json.loads(bracket_match.group())
            if isinstance(result, list):
                return [str(item) for item in result if str(item) in valid_ids]
        except (ValueError, json.JSONDecodeError):
            pass

    return []
