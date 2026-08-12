"""KnowWhere-style 3-channel hybrid retrieval (path BM25 + content BM25 + term).

Ported from Ontos-AI/knowhere:
  packages/shared-python/shared/services/retrieval/search/{scoring,lexical_ranker,channels}.py

Reference: https://github.com/Ontos-AI/knowhere
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

RRF_K = 60
CHANNEL_WEIGHT_PATH = 1.0
CHANNEL_WEIGHT_CONTENT = 2.0
CHANNEL_WEIGHT_TERM = 1.5
INTERNAL_RECALL_K_MULTIPLIER = 2


def tokenize_for_retrieval(text: str, *, dedupe: bool = True) -> List[str]:
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", str(text or "").lower())
    if not dedupe:
        return [t for t in tokens if t]
    seen: set[str] = set()
    out: List[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def tokenize_query_for_ranker(query: str) -> List[str]:
    return tokenize_for_retrieval(query, dedupe=True)


def _space_join_tokens(text: str) -> str:
    return " ".join(tokenize_for_retrieval(text, dedupe=False))


def build_content_search_text(content: str, *, section_summary: Optional[str] = None) -> str:
    parts = [str(content or "").strip()]
    if section_summary and str(section_summary).strip():
        parts.append(str(section_summary).strip())
    raw = " ".join(p for p in parts if p)
    return _space_join_tokens(raw) if raw else ""


def build_path_search_text(
    *,
    source_file_name: Optional[str] = None,
    section_path: Optional[str] = None,
    section_title: Optional[str] = None,
) -> str:
    parts = [
        str(v).strip()
        for v in (source_file_name, section_path, section_title)
        if v and str(v).strip()
    ]
    if not parts:
        return ""
    return _space_join_tokens(" ".join(parts))


def build_term_search_text(content: str, *, path_text: Optional[str] = None) -> str:
    combined = f"{str(content or '').strip()} {str(path_text or '').strip()}".strip()
    return combined


def _get_search_tokens(row: dict[str, Any], *, search_field: str) -> List[str]:
    return [token for token in str(row.get(search_field) or "").split() if token]


def _rank_rows_by_token_overlap(
    rows: List[dict[str, Any]],
    query_tokens: List[str],
    *,
    search_field: str,
) -> List[dict[str, Any]]:
    ranked_rows: List[dict[str, Any]] = []
    query_token_set = set(query_tokens)
    for row in rows:
        tokens = _get_search_tokens(row, search_field=search_field)
        overlap = len(query_token_set.intersection(tokens))
        if overlap <= 0:
            continue
        ranked_rows.append(dict(row, score=float(overlap)))
    ranked_rows.sort(key=lambda row: row["score"], reverse=True)
    return ranked_rows


def rank_rows_by_bm25(
    rows: List[dict[str, Any]],
    query_tokens: List[str],
    *,
    search_field: str,
) -> List[dict[str, Any]]:
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return _rank_rows_by_token_overlap(rows, query_tokens, search_field=search_field)

    corpus: List[List[str]] = []
    ranked_rows: List[dict[str, Any]] = []
    for row in rows:
        tokens = _get_search_tokens(row, search_field=search_field)
        if not tokens:
            continue
        corpus.append(tokens)
        ranked_rows.append(row)

    if not corpus or not query_tokens:
        return []

    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query_tokens)
    for index, row in enumerate(ranked_rows):
        row = dict(row)
        row["score"] = float(scores[index])
        ranked_rows[index] = row
    ranked_rows.sort(key=lambda row: row["score"], reverse=True)
    return ranked_rows


def rank_rows_by_term_channel(rows: List[dict[str, Any]], query: str) -> List[dict[str, Any]]:
    query_lower = query.lower().strip()
    query_tokens = tokenize_query_for_ranker(query)
    if not query_lower or not query_tokens:
        return []

    scored: List[dict[str, Any]] = []
    for row in rows:
        haystack = (row.get("term_search_text") or "").lower()
        if not haystack:
            continue
        if query_lower in haystack:
            scored.append(dict(row, score=100.0))
            continue
        hit_count = sum(1 for unit in query_tokens if unit in haystack)
        if hit_count > 0:
            scored.append(dict(row, score=float(hit_count)))
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def merge_channels_rrf(
    channels: List[List[dict[str, Any]]],
    weights: List[float],
    top_k: int,
    k: int = RRF_K,
) -> List[dict[str, Any]]:
    score_dict: Dict[str, float] = {}
    row_by_chunk_id: Dict[str, dict[str, Any]] = {}

    for channel_idx, channel_rows in enumerate(channels):
        weight = weights[channel_idx] if channel_idx < len(weights) else 1.0
        for rank, row in enumerate(channel_rows):
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            rrf_score = weight / (k + rank + 1)
            score_dict[chunk_id] = score_dict.get(chunk_id, 0.0) + rrf_score
            if chunk_id not in row_by_chunk_id:
                row_by_chunk_id[chunk_id] = row

    ranked = sorted(score_dict.items(), key=lambda x: x[1], reverse=True)
    results: List[dict[str, Any]] = []
    for chunk_id, fused_score in ranked[:top_k]:
        row = dict(row_by_chunk_id[chunk_id])
        row["score"] = round(fused_score, 6)
        results.append(row)
    return results


def normalize_row_scores(
    rows: List[dict[str, Any]],
    *,
    source_field: str = "score",
    target_field: str = "score",
    default: float = 0.5,
) -> None:
    if not rows:
        return
    values = [float(row.get(source_field, 0.0) or 0.0) for row in rows]
    min_score = min(values)
    max_score = max(values)
    if max_score <= 0.0 and min_score <= 0.0:
        for row in rows:
            row[target_field] = 0.0
        return
    if max_score == min_score:
        for row in rows:
            row[target_field] = default
        return
    denominator = max_score - min_score
    for row in rows:
        raw_score = float(row.get(source_field, 0.0) or 0.0)
        row[target_field] = round((raw_score - min_score) / denominator, 6)


def _channel_weights() -> Tuple[float, float, float]:
    path_w = float(os.environ.get("NAV_DISCOVERY_CHANNEL_WEIGHT_PATH", str(CHANNEL_WEIGHT_PATH)).strip() or CHANNEL_WEIGHT_PATH)
    content_w = float(
        os.environ.get("NAV_DISCOVERY_CHANNEL_WEIGHT_CONTENT", str(CHANNEL_WEIGHT_CONTENT)).strip()
        or CHANNEL_WEIGHT_CONTENT
    )
    term_w = float(os.environ.get("NAV_DISCOVERY_CHANNEL_WEIGHT_TERM", str(CHANNEL_WEIGHT_TERM)).strip() or CHANNEL_WEIGHT_TERM)
    return path_w, content_w, term_w


def hybrid_search_rows(
    rows: Sequence[dict[str, Any]],
    query: str,
    *,
    top_k: int = 10,
    internal_recall_k: Optional[int] = None,
) -> List[dict[str, Any]]:
    """Run KnowWhere path/content/term channels + weighted RRF over in-memory rows."""
    if not rows:
        return []
    query_tokens = tokenize_query_for_ranker(query)
    if not query_tokens:
        return []

    recall_k = internal_recall_k
    if recall_k is None:
        mult = int(os.environ.get("NAV_DISCOVERY_RECALL_MULT", str(INTERNAL_RECALL_K_MULTIPLIER)).strip() or INTERNAL_RECALL_K_MULTIPLIER)
        recall_k = max(top_k, top_k * max(1, mult))

    rrf_k = int(os.environ.get("NAV_DISCOVERY_RRF_K", str(RRF_K)).strip() or RRF_K)
    path_w, content_w, term_w = _channel_weights()

    path_rows = rank_rows_by_bm25(list(rows), query_tokens, search_field="path_search_text")[:recall_k]
    content_rows = rank_rows_by_bm25(list(rows), query_tokens, search_field="content_search_text")[:recall_k]
    term_rows = rank_rows_by_term_channel(list(rows), query)[:recall_k]

    fused = merge_channels_rrf(
        [path_rows, content_rows, term_rows],
        [path_w, content_w, term_w],
        top_k,
        k=rrf_k,
    )
    normalize_row_scores(fused, target_field="discovery_score")
    return fused



def map_channel_weights() -> Tuple[float, float, float]:
    """Channel weights for map scoring (prefer NAV_MAP_* env, fall back to legacy names)."""
    path_w = float(
        os.environ.get(
            "NAV_MAP_CHANNEL_WEIGHT_PATH",
            os.environ.get("NAV_DISCOVERY_CHANNEL_WEIGHT_PATH", str(CHANNEL_WEIGHT_PATH)),
        ).strip()
        or CHANNEL_WEIGHT_PATH
    )
    content_w = float(
        os.environ.get(
            "NAV_MAP_CHANNEL_WEIGHT_CONTENT",
            os.environ.get("NAV_DISCOVERY_CHANNEL_WEIGHT_CONTENT", str(CHANNEL_WEIGHT_CONTENT)),
        ).strip()
        or CHANNEL_WEIGHT_CONTENT
    )
    term_w = float(
        os.environ.get(
            "NAV_MAP_CHANNEL_WEIGHT_TERM",
            os.environ.get("NAV_DISCOVERY_CHANNEL_WEIGHT_TERM", str(CHANNEL_WEIGHT_TERM)),
        ).strip()
        or CHANNEL_WEIGHT_TERM
    )
    return path_w, content_w, term_w


def map_dense_enabled() -> bool:
    """Dense fuse is off until wired with Knowhere three-channel vector.

    Keep the dense code path; do not honor NAV_MAP_DENSE until both sides
    share one embedding backend (no silent BM25 fallback).
    """
    return False


_DENSE_ENCODER_CACHE: dict[str, Any] = {}
_TEXT_EMB_CACHE: dict[tuple[str, str, str, str], Any] = {}


def _dense_encoder():
    model_name = (
        os.environ.get("BODYRICH_EMBEDDING_MODEL", "").strip()
        or os.environ.get("EMBEDDING_MODEL", "").strip()
        or "text-embedding-v3"
    )
    cached = _DENSE_ENCODER_CACHE.get(model_name)
    if cached is not None:
        return cached, model_name
    from agent_delivery.code.embedding_backend import (  # type: ignore
        get_dense_encoder,
        resolve_embedding_model,
    )

    resolved = resolve_embedding_model(model_name)
    enc = get_dense_encoder(resolved)
    _DENSE_ENCODER_CACHE[model_name] = enc
    return enc, resolved


def score_dense_channel(
    texts: Sequence[str],
    query: str,
    *,
    unit_ids: Optional[Sequence[str]] = None,
    doc_id: Optional[str] = None,
    channel: str = "content",
    namespace: Optional[str] = None,
) -> Optional[List[float]]:
    """Path/content dense cosine scores aligned with texts.

    Returns None when dense is disabled or unavailable (caller keeps BM25-only
    channel scores). When enabled, returns one cosine score per text.

    Unit path/content vectors are query-independent and persisted under
    cache/.../map_units/{model}/{namespace}/{doc_id}/{channel}.npz when
    doc_id+unit_ids are provided.
    """
    if not map_dense_enabled():
        return None
    if not texts:
        return []
    # Deterministic offline hook for unit tests (no remote encoder).
    mock = os.environ.get("NAV_MAP_DENSE_MOCK", "").strip().lower()
    if mock in {"1", "true", "yes", "on"}:
        q = (query or "").strip().lower()
        q_toks = set(q.split()) if q else set()
        out: List[float] = []
        for text in texts:
            t = (text or "").strip().lower()
            if not t or not q_toks:
                out.append(0.0)
                continue
            t_toks = set(t.split())
            overlap = len(q_toks & t_toks)
            out.append(float(overlap) / float(max(1, len(q_toks))))
        return out
    try:
        from agent_delivery.code.embedding_backend import (  # type: ignore
            encode_labeled_texts_normalized,
            encode_query_normalized,
            encode_texts_normalized,
        )
        import numpy as np

        model, model_name = _dense_encoder()
        qv = encode_query_normalized(model, query)
        batch = int(os.environ.get("BODYRICH_EMBEDDING_BATCH_SIZE", "10") or "10")
        batch = max(1, min(batch, 10))
        ids = [str(u) for u in (unit_ids or [])]
        ns = (
            (namespace or "").strip()
            or os.environ.get("NAV_MAP_UNIT_CACHE_NS", "").strip()
            or "default"
        )
        if doc_id and ids and len(ids) == len(texts):
            mem_key = (model_name, ns, str(doc_id), str(channel), ",".join(ids))
            mat = _TEXT_EMB_CACHE.get(mem_key)
            if mat is None:
                mat = encode_labeled_texts_normalized(
                    model,
                    doc_id=str(doc_id),
                    channel=str(channel),
                    unit_ids=ids,
                    texts=texts,
                    batch_size=batch,
                    namespace=ns,
                )
                _TEXT_EMB_CACHE[mem_key] = mat
        else:
            mat = encode_texts_normalized(
                model,
                texts,
                batch_size=batch,
                namespace=f"map_channel:{channel}",
            )
        if int(getattr(mat, "shape", [0, 0])[1]) != int(qv.shape[0]):
            return None
        # Cosine = L2-normalized dot product. Sanitize before matmul so zero/NaN
        # rows cannot trigger Accelerate RuntimeWarnings or pollute scores.
        from agent_delivery.code.embedding_backend import l2_normalize_rows  # type: ignore

        mat = l2_normalize_rows(mat, label=f"dense:{channel}")
        qv = l2_normalize_rows(qv, label="dense:query")
        # Use np.dot (not @): on macOS Accelerate, mat@qv can emit spurious
        # divide/overflow/invalid RuntimeWarnings even when all values are finite
        # unit vectors and the cosine scores are correct.
        sims = np.nan_to_num(
            np.asarray(np.dot(mat, qv), dtype=np.float64),
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        )
        return [float(x) for x in sims.tolist()]
    except Exception:
        return None


def _normalize_score_list(values: List[float]) -> List[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi <= 0.0 and lo <= 0.0:
        return [0.0 for _ in values]
    if hi == lo:
        return [1.0 for _ in values]
    denom = hi - lo
    return [(v - lo) / denom for v in values]


def fuse_channel_bm25_dense(
    bm25_by_id: Dict[str, float],
    dense_by_id: Optional[Dict[str, float]],
    unit_ids: Sequence[str],
) -> Dict[str, float]:
    """Within-channel fuse: BM25 alone, or mean of min-max-normalized BM25+dense."""
    if not dense_by_id:
        return {uid: float(bm25_by_id.get(uid, 0.0) or 0.0) for uid in unit_ids}
    bm25_vals = [float(bm25_by_id.get(uid, 0.0) or 0.0) for uid in unit_ids]
    dense_vals = [float(dense_by_id.get(uid, 0.0) or 0.0) for uid in unit_ids]
    bm25_n = _normalize_score_list(bm25_vals)
    dense_n = _normalize_score_list(dense_vals)
    dense_w = float(os.environ.get("NAV_MAP_CHANNEL_DENSE_WEIGHT", "0.5").strip() or "0.5")
    dense_w = min(1.0, max(0.0, dense_w))
    bm25_w = 1.0 - dense_w
    return {
        uid: bm25_w * bm25_n[i] + dense_w * dense_n[i]
        for i, uid in enumerate(unit_ids)
    }


def _rank_ids_by_score(score_by_id: Dict[str, float]) -> List[str]:
    ranked = sorted(
        ((sid, float(score)) for sid, score in score_by_id.items() if float(score) > 0.0),
        key=lambda item: (-item[1], item[0]),
    )
    return [sid for sid, _ in ranked]


def score_rows_hybrid_all(
    rows: Sequence[dict[str, Any]],
    query: str,
    *,
    path_texts: Optional[Dict[str, str]] = None,
    content_texts: Optional[Dict[str, str]] = None,
    doc_id: Optional[str] = None,
    namespace: Optional[str] = None,
    dense_scores_by_channel: Optional[
        Dict[str, Optional[Dict[str, float]]]
    ] = None,
) -> List[dict[str, Any]]:
    """Score every row with path/content/term; optional within-channel dense fuse.

    Unlike hybrid_search_rows, this returns a score for every input row (0 if no hit).
    Dense is applied only inside path and content channels when score_dense_channel
    returns values; term stays lexical. ``dense_scores_by_channel`` lets callers
    load cached vectors in partitions while keeping BM25, normalization, channel
    ranking, and RRF global over this complete ``rows`` pool.
    """
    if not rows:
        return []
    query_tokens = tokenize_query_for_ranker(query)
    unit_ids = [str(row.get("chunk_id") or "").strip() for row in rows]
    unit_ids = [uid for uid in unit_ids if uid]
    if not unit_ids:
        return [dict(row, score=0.0) for row in rows]

    row_by_id = {str(row.get("chunk_id") or ""): dict(row) for row in rows if row.get("chunk_id")}
    path_w, content_w, term_w = map_channel_weights()
    rrf_k = int(
        os.environ.get(
            "NAV_MAP_RRF_K",
            os.environ.get("NAV_DISCOVERY_RRF_K", str(RRF_K)),
        ).strip()
        or RRF_K
    )

    if query_tokens:
        path_ranked = rank_rows_by_bm25(list(rows), query_tokens, search_field="path_search_text")
        content_ranked = rank_rows_by_bm25(
            list(rows), query_tokens, search_field="content_search_text"
        )
    else:
        path_ranked, content_ranked = [], []
    term_ranked = rank_rows_by_term_channel(list(rows), query) if query else []

    path_bm25 = {
        str(r.get("chunk_id") or ""): float(r.get("score") or 0.0) for r in path_ranked
    }
    content_bm25 = {
        str(r.get("chunk_id") or ""): float(r.get("score") or 0.0) for r in content_ranked
    }
    term_bm25 = {
        str(r.get("chunk_id") or ""): float(r.get("score") or 0.0) for r in term_ranked
    }

    if dense_scores_by_channel is None:
        path_text_list = [
            str(
                (path_texts or {}).get(uid)
                or row_by_id.get(uid, {}).get("path_text")
                or ""
            )
            for uid in unit_ids
        ]
        content_text_list = [
            str(
                (content_texts or {}).get(uid)
                or row_by_id.get(uid, {}).get("content")
                or ""
            )
            for uid in unit_ids
        ]
        path_dense_scores = score_dense_channel(
            path_text_list,
            query,
            unit_ids=unit_ids,
            doc_id=doc_id,
            channel="path",
            namespace=namespace,
        )
        content_dense_scores = score_dense_channel(
            content_text_list,
            query,
            unit_ids=unit_ids,
            doc_id=doc_id,
            channel="content",
            namespace=namespace,
        )
        path_dense_by_id = (
            {
                uid: float(path_dense_scores[i])
                for i, uid in enumerate(unit_ids)
            }
            if path_dense_scores is not None
            and len(path_dense_scores) == len(unit_ids)
            else None
        )
        content_dense_by_id = (
            {
                uid: float(content_dense_scores[i])
                for i, uid in enumerate(unit_ids)
            }
            if content_dense_scores is not None
            and len(content_dense_scores) == len(unit_ids)
            else None
        )
    else:
        path_dense_by_id = dense_scores_by_channel.get("path")
        content_dense_by_id = dense_scores_by_channel.get("content")

    path_channel = fuse_channel_bm25_dense(path_bm25, path_dense_by_id, unit_ids)
    content_channel = fuse_channel_bm25_dense(content_bm25, content_dense_by_id, unit_ids)
    term_channel = {uid: float(term_bm25.get(uid, 0.0) or 0.0) for uid in unit_ids}

    # Convert channel scores to ranked lists for existing RRF merger.
    def _rows_from_scores(score_by_id: Dict[str, float]) -> List[dict[str, Any]]:
        out: List[dict[str, Any]] = []
        for uid in _rank_ids_by_score(score_by_id):
            row = dict(row_by_id[uid])
            row["score"] = float(score_by_id[uid])
            out.append(row)
        return out

    fused = merge_channels_rrf(
        [
            _rows_from_scores(path_channel),
            _rows_from_scores(content_channel),
            _rows_from_scores(term_channel),
        ],
        [path_w, content_w, term_w],
        top_k=len(unit_ids),
        k=rrf_k,
    )
    fused_by_id = {str(r.get("chunk_id") or ""): float(r.get("score") or 0.0) for r in fused}
    out_rows: List[dict[str, Any]] = []
    for uid in unit_ids:
        row = dict(row_by_id[uid])
        row["score"] = float(fused_by_id.get(uid, 0.0) or 0.0)
        row["path_channel_score"] = float(path_channel.get(uid, 0.0) or 0.0)
        row["content_channel_score"] = float(content_channel.get(uid, 0.0) or 0.0)
        row["term_channel_score"] = float(term_channel.get(uid, 0.0) or 0.0)
        out_rows.append(row)
    return out_rows
