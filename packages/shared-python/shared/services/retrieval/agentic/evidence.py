from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import RetrievalHitStat
from shared.services.retrieval.agentic.budget import BudgetLedger
from shared.services.retrieval.agentic.types import DocTreeNode
from shared.services.retrieval.hydration.assets import build_retrieval_asset_url_map
from shared.services.retrieval.stats.service import compute_importance_score
from shared.utils.token_estimate import estimate_tokens


def with_context_prompt_projection(
    snapshot: dict[str, object],
    *,
    prompt_tokens: int,
) -> dict[str, object]:
    projected: dict[str, object] = dict(snapshot)
    context_raw = projected.get("context") or {}
    if not isinstance(context_raw, dict):
        return projected

    context = dict(context_raw)
    used = int(context.get("used", 0) or 0)
    reserved = int(context.get("reserved", 0) or 0)
    capacity = int(context.get("capacity", 0) or 0)
    projected_used = min(capacity, used + max(int(prompt_tokens), 0))
    projected_remaining = max(capacity - projected_used - reserved, 0)
    context.update(
        {
            "used_projected_before_answer": projected_used,
            "answer_prompt_estimate": max(int(prompt_tokens), 0),
            "remaining": projected_remaining,
            "used_pct": 100
            if capacity <= 0
            else min(100, int(round((projected_used + reserved) * 100 / capacity))),
        }
    )
    if projected_remaining <= 0:
        context["status"] = "EXHAUSTED"
    elif context["used_pct"] >= 80:
        context["status"] = "CRITICAL"
    elif context["used_pct"] >= 50:
        context["status"] = "TIGHT"
    else:
        context["status"] = "HEALTHY"
    projected["context"] = context
    return projected


def collect_media_chunks(node: DocTreeNode) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    for chunks in node.leaf_content.values():
        for chunk in chunks:
            chunk_type = (
                chunk.get("chunk_type") or chunk.get("type") or ""
            ).strip().lower()
            if chunk_type in ("image", "table"):
                media.append(chunk)
    for child in node.children.values():
        media.extend(collect_media_chunks(child))
    return media


def collect_media_chunks_all(
    doc_trees: dict[str, DocTreeNode],
) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    for tree in doc_trees.values():
        media.extend(collect_media_chunks(tree))
    return media


async def build_asset_url_map(
    media_chunks: list[dict[str, Any]],
) -> dict[str, str]:
    return await build_retrieval_asset_url_map(
        media_chunks,
        log_context="agentic evidence",
    )


def _collect_all_leaf_paths(node: DocTreeNode) -> set[str]:
    paths = set(node.leaf_content.keys())
    for child in node.children.values():
        paths.update(_collect_all_leaf_paths(child))
    return paths


def _collect_visible_paths(node: DocTreeNode) -> set[str]:
    paths = {item["path"] for item in node.outline_items if item.get("path")}
    for child in node.children.values():
        paths.update(_collect_visible_paths(child))
    return paths


def _find_closest_ancestor(path: str, target_paths: set[str]) -> str | None:
    parts = path.split(" / ")
    for i in range(len(parts) - 1, 0, -1):
        ancestor = " / ".join(parts[:i])
        if ancestor in target_paths:
            return ancestor
    return None


def reconcile_deferred_assets(
    tree: DocTreeNode,
    pending_assets: list[dict],
) -> None:
    final_paths = _collect_all_leaf_paths(tree)
    visible_paths = _collect_visible_paths(tree)
    all_target_paths = final_paths | visible_paths

    if not all_target_paths:
        return

    existing_ids = {
        str(row.get("chunk_id") or "")
        for row in tree.flatten_chunk_rows()
        if row.get("chunk_id")
    }

    placed = 0
    ancestor_placed = 0
    for asset in pending_assets:
        chunk_id = str(asset.get("chunk_id") or "")
        if chunk_id and chunk_id in existing_ids:
            continue

        owner_path = asset.get("owner_section_path") or asset.get("section_path")
        if not owner_path:
            continue

        target_path = owner_path if owner_path in all_target_paths else None
        if target_path is None:
            target_path = _find_closest_ancestor(owner_path, all_target_paths)
            if target_path:
                ancestor_placed += 1

        if target_path is None:
            continue

        tree.add_leaf_chunks(target_path, [asset])
        if chunk_id:
            existing_ids.add(chunk_id)
        placed += 1

    if placed:
        tree.reparent_leaf_content()
        logger.info(
            f"  deferred asset reconcile: {placed}/{len(pending_assets)} "
            f"assets placed into {len(final_paths)} leaf + {len(visible_paths)} visible paths "
            f"(ancestor_fallback={ancestor_placed})"
        )


async def render_evidence(
    db: AsyncSession,
    doc_trees: dict[str, DocTreeNode],
    doc_id_to_name: dict[str, str],
) -> str:
    del db

    from shared.services.retrieval.agentic.evidence_renderer import render_unified_doc_tree

    asset_url_map = await build_asset_url_map(collect_media_chunks_all(doc_trees))

    evidence_parts: list[str] = []
    for doc_id, doc_tree in doc_trees.items():
        if doc_tree.has_content():
            doc_name = doc_id_to_name.get(doc_id, doc_id)
            rendered = render_unified_doc_tree(
                doc_tree,
                doc_name,
                asset_lookup=asset_url_map,
            )
            if rendered.strip():
                evidence_parts.append(rendered)

    return "\n\n".join(evidence_parts) if evidence_parts else "(no evidence collected)"


def _iter_leaf_content(node: DocTreeNode):
    for path, chunks in node.leaf_content.items():
        yield path, chunks
    for child in node.children.values():
        yield from _iter_leaf_content(child)


def _collect_confidences(node: DocTreeNode) -> dict[str, float]:
    values = dict(node.confidence)
    for child in node.children.values():
        for path, score in _collect_confidences(child).items():
            values[path] = max(values.get(path, 0.0), score)
    return values


def _pop_leaf_path(node: DocTreeNode, path: str) -> bool:
    if path in node.leaf_content:
        node.leaf_content.pop(path)
        return True
    for child in node.children.values():
        if _pop_leaf_path(child, path):
            return True
    return False


def _estimate_chunks_tokens(chunks: list[dict[str, Any]]) -> int:
    text = "\n".join(str(chunk.get("content") or "") for chunk in chunks)
    return estimate_tokens(text)


async def _fetch_importance_norm_scores(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    chunk_ids: list[str],
) -> dict[str, float]:
    if not chunk_ids:
        return {}
    stmt = (
        select(
            RetrievalHitStat.chunk_id,
            RetrievalHitStat.hit_count,
            RetrievalHitStat.last_hit_at,
            RetrievalHitStat.created_at,
        )
        .where(RetrievalHitStat.user_id == user_id)
        .where(RetrievalHitStat.namespace == namespace)
        .where(RetrievalHitStat.hit_kind == "chunk")
        .where(RetrievalHitStat.chunk_id.in_(chunk_ids))
    )
    result = await db.execute(stmt)
    scores: dict[str, float] = {}
    for chunk_id, hit_count, last_hit_at, created_at in result.all():
        if chunk_id and last_hit_at and created_at:
            scores[str(chunk_id)] = compute_importance_score(
                hit_count,
                last_hit_at,
                created_at,
            )
    return scores


async def trim_evidence_to_budget(
    db: AsyncSession,
    *,
    doc_trees: dict[str, DocTreeNode],
    doc_id_to_name: dict[str, str],
    context_remaining: int,
    user_id: str,
    namespace: str,
    ledger: BudgetLedger | None,
    safety_margin: float = 0.9,
) -> str:
    full_text = await render_evidence(db, doc_trees, doc_id_to_name)
    target = int(max(context_remaining, 0) * safety_margin)
    if estimate_tokens(full_text) <= target:
        return full_text

    candidates: list[tuple[str, str, tuple[float, float, float], int]] = []
    for doc_id, tree in doc_trees.items():
        confidence = _collect_confidences(tree)
        for path, chunks in _iter_leaf_content(tree):
            chunk_ids = [
                str(chunk.get("chunk_id"))
                for chunk in chunks
                if chunk.get("chunk_id")
            ]
            importance = 0.0
            importance_scores = await _fetch_importance_norm_scores(
                db,
                user_id=user_id,
                namespace=namespace,
                chunk_ids=chunk_ids,
            )
            if importance_scores:
                importance = max(importance_scores.values())
            discovery_score = (
                float(chunks[0].get("discovery_score", 0.0) or 0.0)
                if chunks
                else 0.0
            )
            score = (
                float(confidence.get(path, 0.0) or 0.0),
                discovery_score,
                importance,
            )
            candidates.append((doc_id, path, score, _estimate_chunks_tokens(chunks)))

    current_estimate = estimate_tokens(full_text)
    removed: list[dict[str, Any]] = []
    for doc_id, path, score, token_estimate in sorted(
        candidates,
        key=lambda item: (item[2], -item[3]),
    ):
        if current_estimate <= target:
            break
        if _pop_leaf_path(doc_trees[doc_id], path):
            confidence_score, discovery_score, importance_score = score
            removed.append(
                {
                    "document_id": doc_id,
                    "document_name": doc_id_to_name.get(doc_id, doc_id),
                    "path": path,
                    "confidence_score": round(confidence_score, 4),
                    "discovery_score": round(discovery_score, 4),
                    "importance_score": round(importance_score, 4),
                    "token_estimate": token_estimate,
                }
            )
            current_estimate = max(current_estimate - token_estimate, 0)

    if ledger is not None:
        ledger.trimmed_paths.extend(removed)
    logger.info(
        f"  agentic.trim_evidence: removed={len(removed)} "
        f"est_tokens={current_estimate} target={target}"
    )
    return await render_evidence(db, doc_trees, doc_id_to_name)
