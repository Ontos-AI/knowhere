"""
Knowledge Graph Builder — KB-level knowledge graph assembler (v2.0).

Assembles a file-level knowledge_graph.json from parsed chunks + connect_builder edges.
Deployed to ~/.knowhere/{kb_id}/ and grows incrementally as more files are parsed.

Architecture:
  - files: per-file summaries (chunks_count, types, top_keywords, importance)
  - edges: cross-file relationships (aggregated from chunk-level connections)
  - chunk_stats.json: per-chunk usage tracking (hit_count, last_hit, decay)
  - Per-file chunks.json: full chunk data lives in subdirectories

Usage:
  # One-stop API (recommended)
  graph = build_and_deploy(chunks, kb_id="my_kb", parsed_output_dir=add_dir)

  # Manual: first build
  graph = build_knowledge_graph(all_chunks, connections, kb_id="my_kb")

  # Manual: incremental update
  graph = update_knowledge_graph(existing_graph, new_chunks, existing_chunks)
"""

import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.connect_builder.builder import (
    DEFAULT_CONFIG,
    _compute_keyword_score,
    _extract_file_key,
    _get_keywords,
    _normalize_keyword,
)
from loguru import logger

from shared.utils.chunk_refs import CHUNK_REF_PATTERN

# ─── Tree Construction ───────────────────────────────────────────────────────


def _build_tree_from_paths(paths: List[str]) -> Dict[str, Any]:
    """
    Rebuild hierarchical tree from chunk path list.

    Args:
        paths: List of chunk paths, e.g. ["Default_Root/report.pdf/Section 1/1.1", ...]

    Returns:
        Nested dict tree rooted at Default_Root.
    """
    root: Dict[str, Any] = {}
    for path in paths:
        if not path:
            continue
        nodes = [n.strip() for n in path.split("/") if n.strip()]
        current = root
        for node in nodes:
            if node not in current:
                current[node] = {}
            current = current[node]
    return root


def _merge_tree(base: Dict[str, Any], addition: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep-merge two tree dicts. Addition is merged INTO base (in-place).

    Args:
        base: Existing tree.
        addition: New tree to merge in.

    Returns:
        The merged base dict (same reference).
    """
    for key, value in addition.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_tree(base[key], value)
        else:
            base[key] = value
    return base


# ─── Node Extraction ─────────────────────────────────────────────────────────


def _chunks_to_nodes(
    chunks: List[Dict[str, Any]],
    content_preview_len: int = 200,
) -> List[Dict[str, Any]]:
    """
    Extract node metadata from chunks for the knowledge graph.

    Args:
        chunks: List of normalized chunk dicts.
        content_preview_len: Max characters for content_preview.

    Returns:
        List of node dicts with: id, type, path, summary, keywords, content_preview.
    """
    nodes = []
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        if not chunk_id:
            continue

        content = chunk.get("content") or chunk.get("text", "")
        metadata = chunk.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        # Extract keywords from metadata or top-level
        keywords = metadata.get("keywords", [])
        if not keywords:
            keywords = chunk.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(";") if k.strip()]

        node = {
            "id": chunk_id,
            "type": chunk.get("type", "text"),
            "path": chunk.get("path", ""),
            "summary": metadata.get("summary") or chunk.get("summary", ""),
            "keywords": keywords,
            "content_preview": content[:content_preview_len] if content else "",
        }
        nodes.append(node)

    return nodes


# ─── Edge Extraction ─────────────────────────────────────────────────────────


def _connections_to_edges(
    connections: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Convert connect_builder output to deduplicated edge list.
    connect_builder produces bidirectional entries (A→B and B→A);
    we deduplicate to keep only one edge per pair.

    Args:
        connections: Output from build_connections(), mapping chunk_id → list of connections.

    Returns:
        List of edge dicts: {source, target, relation, score, shared_keywords}.
    """
    seen_pairs: set = set()
    edges = []

    for source_id, conn_list in connections.items():
        for conn in conn_list:
            target_id = conn.get("target", "")
            pair_key = tuple(sorted([source_id, target_id]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            edges.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "relation": conn.get("relation", "related"),
                    "score": conn.get("score", 0.0),
                    "shared_keywords": conn.get("keywords", []),
                }
            )

    return edges


def _merge_related_connections_into_chunks(
    chunks: List[Dict[str, Any]],
    connections: Dict[str, List[Dict[str, Any]]],
) -> None:
    """Backfill related connections into chunk metadata without touching embeds."""
    if not chunks or not connections:
        return

    chunk_map = {
        str(chunk.get("chunk_id") or chunk.get("know_id", "")): chunk
        for chunk in chunks
        if chunk.get("chunk_id") or chunk.get("know_id")
    }

    for chunk_id, conn_list in connections.items():
        chunk = chunk_map.get(str(chunk_id))
        if not chunk:
            continue
        metadata = chunk.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            chunk["metadata"] = metadata
        existing = metadata.get("connect_to", [])
        if not isinstance(existing, list):
            existing = []

        merged = []
        seen = set()
        for item in existing:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("target") or ""),
                str(item.get("relation") or "related"),
                str(item.get("ref") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

        for conn in conn_list:
            if not isinstance(conn, dict):
                continue
            if conn.get("relation", "related") != "related":
                continue
            key = (
                str(conn.get("target") or ""),
                "related",
                "",
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "target": conn.get("target", ""),
                    "relation": "related",
                    "score": conn.get("score", 0.0),
                    "keywords": conn.get("keywords", []),
                }
            )

        metadata["connect_to"] = merged


def _save_chunks_by_source_file(kb_dir: str, chunks: List[Dict[str, Any]]) -> None:
    """Persist grouped chunks.json files after metadata backfill."""
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        source_file = chunk.get("_source_file")
        if not source_file:
            continue
        cleaned = dict(chunk)
        cleaned.pop("_source_file", None)
        grouped[str(source_file)].append(cleaned)

    for source_file, source_chunks in grouped.items():
        output_path = os.path.join(kb_dir, source_file, "chunks.json")
        _save_chunks(source_chunks, output_path)


# ─── Incremental Matching ────────────────────────────────────────────────────


def _incremental_connections(
    new_chunks: List[Dict[str, Any]],
    existing_chunks: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Match ONLY new_chunks ↔ existing_chunks (skip existing ↔ existing).
    Reuses connect_builder scoring functions.

    Complexity: O(new × existing) instead of O(all²).

    Args:
        new_chunks: Newly parsed file's chunks.
        existing_chunks: All previously known chunks.
        config: Optional config overrides (same keys as connect_builder.DEFAULT_CONFIG).

    Returns:
        Dict mapping chunk_id → list of connection dicts (same format as build_connections).
    """
    from difflib import SequenceMatcher

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    min_overlap = cfg["min_keyword_overlap"]
    kw_weight = cfg["keyword_score_weight"]
    max_conns = cfg["max_connections_per_chunk"]
    min_score = cfg["min_score_threshold"]
    cross_only = cfg["cross_file_only"]
    max_content_overlap = cfg.get("max_content_overlap", 0.8)

    # Pre-compute keyword sets for new chunks
    new_data: Dict[str, Tuple[str, set, str]] = {}  # id → (file_key, kw_set, content)
    for chunk in new_chunks:
        cid = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        if not cid:
            continue
        file_key = _extract_file_key(chunk.get("path", ""))
        kws = _get_keywords(chunk)
        normalized = {_normalize_keyword(k) for k in kws if k}
        normalized.discard("")
        content = chunk.get("content") or chunk.get("text", "")
        new_data[cid] = (file_key, normalized, content)

    # Pre-compute keyword sets for existing chunks
    existing_data: Dict[str, Tuple[str, set, str]] = {}
    for chunk in existing_chunks:
        cid = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        if not cid:
            continue
        file_key = _extract_file_key(chunk.get("path", ""))
        kws = _get_keywords(chunk)
        normalized = {_normalize_keyword(k) for k in kws if k}
        normalized.discard("")
        content = chunk.get("content") or chunk.get("text", "")
        existing_data[cid] = (file_key, normalized, content)

    # Build keyword index for existing chunks only
    existing_kw_index: Dict[str, List[str]] = defaultdict(list)  # kw → [chunk_id]
    for cid, (_, kw_set, _) in existing_data.items():
        for kw in kw_set:
            existing_kw_index[kw].append(cid)

    connections: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    # For each new chunk, find candidates in existing chunks
    for new_id, (new_file, new_kws, new_content) in new_data.items():
        if not new_kws:
            continue

        candidate_shared: Dict[str, set] = defaultdict(set)
        for kw in new_kws:
            for existing_id in existing_kw_index.get(kw, []):
                if cross_only:
                    existing_file = existing_data[existing_id][0]
                    if existing_file == new_file:
                        continue
                candidate_shared[existing_id].add(kw)

        # Score and filter
        scored: List[Tuple[str, float, set]] = []
        for existing_id, shared_kws in candidate_shared.items():
            if len(shared_kws) < min_overlap:
                continue

            existing_kws = existing_data[existing_id][1]
            score = _compute_keyword_score(
                shared_kws=shared_kws,
                kws_a=new_kws,
                kws_b=existing_kws,
                weight=kw_weight,
            )
            if score >= min_score:
                # Near-duplicate filter
                if max_content_overlap < 1.0:
                    existing_content = existing_data[existing_id][2]
                    if new_content and existing_content:
                        ratio = SequenceMatcher(
                            None, new_content, existing_content
                        ).ratio()
                        if ratio >= max_content_overlap:
                            continue
                scored.append((existing_id, score, shared_kws))

        scored.sort(key=lambda x: x[1], reverse=True)
        for existing_id, score, shared_kws in scored[:max_conns]:
            conn = {
                "target": existing_id,
                "relation": "related",
                "score": round(score, 4),
                "keywords": sorted(shared_kws),
            }
            connections[new_id].append(conn)
            # Bidirectional: also add reverse edge
            connections[existing_id].append(
                {
                    "target": new_id,
                    "relation": "related",
                    "score": round(score, 4),
                    "keywords": sorted(shared_kws),
                }
            )

    total = sum(len(v) for v in connections.values())
    logger.info(
        f"🔗 Incremental connections: {total} new edges "
        f"between {len(new_data)} new chunks and {len(existing_data)} existing chunks"
    )

    return dict(connections)


# ─── File-Level Aggregation (v2.0) ───────────────────────────────────────────

# Token filtering — same logic as text_utils._is_meaningful_token
_CN_EN_NUM_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?")
_CHUNK_MARKER_RE = re.compile(
    rf"{CHUNK_REF_PATTERN}|image-\d+|table-\d+",
    re.IGNORECASE,
)


def _is_meaningful_token(token: str) -> bool:
    """Check if a token is worth keeping (same logic as text_utils)."""
    if not _CN_EN_NUM_RE.search(token):
        return False
    if len(token) == 1:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return False
    return True


def _extract_tokens_from_content(content: str) -> List[str]:
    """Extract meaningful tokens from content using jieba (regex fallback)."""
    content = _CHUNK_MARKER_RE.sub("", content)
    # Strip HTML tags and entities (table chunks contain raw HTML)
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"&\w+;", " ", content)
    try:
        import jieba

        if hasattr(jieba, "lcut"):
            raw = jieba.lcut(content)
        else:
            raw = list(jieba.cut(content))
    except ImportError:
        raw = re.split(r"[\s,;，；。！？、\-/]+", content)
    return [t for t in raw if _is_meaningful_token(t)]


def _get_chunk_keywords(chunk: Dict[str, Any]) -> List[str]:
    """Get keywords for a chunk; falls back to tokens from content if empty."""
    keywords = _get_keywords(chunk)
    meaningful = [k for k in keywords if _is_meaningful_token(k)]
    if meaningful:
        return meaningful
    content = chunk.get("content") or chunk.get("text", "")
    if not content:
        return []
    tokens = _extract_tokens_from_content(content)
    seen = set()
    unique = []
    for t in tokens:
        normalized = _normalize_keyword(t)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _compute_tfidf_top_keywords(
    file_chunks: Dict[str, List[Dict[str, Any]]],
    top_k: int = 6,
) -> Dict[str, List[str]]:
    """
    TF-IDF top keywords per file.
    TF = chunks in file containing keyword. IDF = log(total_files / files_with_keyword).
    """
    total_files = len(file_chunks)
    if total_files == 0:
        return {}

    file_kw_tf: Dict[str, Dict[str, int]] = {}
    doc_freq: Dict[str, int] = defaultdict(int)

    for fk, chunks in file_chunks.items():
        kw_count: Dict[str, int] = defaultdict(int)
        file_kw_set: set = set()
        for chunk in chunks:
            for kw in _get_chunk_keywords(chunk):
                normalized = _normalize_keyword(kw)
                if normalized:
                    kw_count[normalized] += 1
                    file_kw_set.add(normalized)
        file_kw_tf[fk] = dict(kw_count)
        for kw in file_kw_set:
            doc_freq[kw] += 1

    result: Dict[str, List[str]] = {}
    for fk, kw_count in file_kw_tf.items():
        scored = []
        for kw, tf in kw_count.items():
            if total_files == 1:
                score = tf  # Single-file KB: pure frequency
            else:
                idf = (
                    math.log(total_files / doc_freq[kw])
                    if doc_freq[kw] < total_files
                    else 0.1
                )
                score = tf * idf
            scored.append((score, tf, kw))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        result[fk] = [kw for _, _, kw in scored[:top_k]]

    return result


def _compute_file_importance(
    chunk_ids: List[str],
    chunk_stats: Dict[str, Dict[str, Any]],
    half_life_days: float = 30.0,
    alpha: float = 0.7,
    beta: float = 0.3,
) -> float:
    """importance = α × usage_heat + β × freshness"""
    if not chunk_ids:
        return 0.0
    total_relevance = 0.0
    earliest_created = None
    for cid in chunk_ids:
        stat = chunk_stats.get(cid, {})
        hc = stat.get("hit_count", 0)
        lh = stat.get("last_hit")
        ca = stat.get("created_at")
        if hc > 0 and lh:
            total_relevance += relevance_score(hc, lh, half_life_days)
        if ca and (earliest_created is None or ca < earliest_created):
            earliest_created = ca
    usage_heat = total_relevance / len(chunk_ids)
    freshness = (
        relevance_score(1, earliest_created, half_life_days)
        if earliest_created
        else 1.0
    )
    return round(alpha * usage_heat + beta * freshness, 4)


def _aggregate_file_level_edges(
    chunk_edges: List[Dict[str, Any]],
    chunk_to_file: Dict[str, str],
    chunk_paths: Optional[Dict[str, str]] = None,
    max_top_connections: int = 10,
) -> List[Dict[str, Any]]:
    """
    Aggregate chunk-level edges into file-level edges.
    Shows top_connections with readable chunk names instead of raw keywords.
    """
    if chunk_paths is None:
        chunk_paths = {}

    pair_data: Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]] = {}
    for edge in chunk_edges:
        src_id = str(edge.get("source", "") or "")
        tgt_id = str(edge.get("target", "") or "")
        sf = chunk_to_file.get(src_id, "")
        tf = chunk_to_file.get(tgt_id, "")
        if not sf or not tf or sf == tf:
            continue
        src_path = chunk_paths.get(src_id) or src_id
        tgt_path = chunk_paths.get(tgt_id) or tgt_id
        src_name = src_path.rsplit("/", 1)[-1] if "/" in src_path else src_path
        tgt_name = tgt_path.rsplit("/", 1)[-1] if "/" in tgt_path else tgt_path

        if sf <= tf:
            pk: Tuple[str, str] = (sf, tf)
            connection = {
                "source_chunk": src_name,
                "source_id": src_id,
                "target_chunk": tgt_name,
                "target_id": tgt_id,
                "relation": edge.get("relation", "related"),
                "score": edge.get("score", 0),
            }
        else:
            pk = (tf, sf)
            connection = {
                "source_chunk": tgt_name,
                "source_id": tgt_id,
                "target_chunk": src_name,
                "target_id": src_id,
                "relation": edge.get("relation", "related"),
                "score": edge.get("score", 0),
            }

        if pk not in pair_data:
            pair_data[pk] = {"connections": []}
        pair_data[pk]["connections"].append(connection)

    file_edges = []
    for (f1, f2), data in pair_data.items():
        conns = data["connections"]
        # Sort by score desc, take top N
        conns.sort(key=lambda x: x["score"], reverse=True)
        scores = [c["score"] for c in conns]
        file_edges.append(
            {
                "source": f1,
                "target": f2,
                "connection_count": len(conns),
                "avg_score": round(sum(scores) / len(scores), 4) if scores else 0,
                "top_connections": conns[:max_top_connections],
            }
        )
    file_edges.sort(key=lambda x: x["connection_count"], reverse=True)
    return file_edges


# ─── Main API ────────────────────────────────────────────────────────────────


def _get_source_file(chunk: Dict[str, Any]) -> str:
    """
    Get the source document file for a chunk.
    Uses `_source_file` tag (injected by build_and_deploy / _load_all_chunks_from_kb)
    for correct grouping of images/tables with their parent document.
    Falls back to `_extract_file_key` for backwards compatibility.
    """
    sf = chunk.get("_source_file")
    if sf:
        return sf
    return _extract_file_key(chunk.get("path", ""))


def build_knowledge_graph(
    all_chunks: List[Dict[str, Any]],
    connections: Dict[str, List[Dict[str, Any]]],
    kb_id: str = "",
    chunk_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    file_summaries: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build a file-level knowledge graph (v2.0)."""
    if chunk_stats is None:
        chunk_stats = {}

    file_chunks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    chunk_to_file: Dict[str, str] = {}
    chunk_paths: Dict[str, str] = {}
    for chunk in all_chunks:
        cid = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        fk = _get_source_file(chunk)
        if fk:
            file_chunks[fk].append(chunk)
            if cid:
                chunk_to_file[cid] = fk
                chunk_paths[cid] = chunk.get("path", "")

    file_keywords = _compute_tfidf_top_keywords(file_chunks)
    chunk_edges = _connections_to_edges(connections)
    file_edges = _aggregate_file_level_edges(chunk_edges, chunk_to_file, chunk_paths)

    files_dict = {}
    for fk, chunks in file_chunks.items():
        types_count: Dict[str, int] = defaultdict(int)
        cids = []
        for c in chunks:
            types_count[c.get("type", "text")] += 1
            cid = str(c.get("chunk_id") or c.get("know_id", ""))
            if cid:
                cids.append(cid)
        files_dict[fk] = {
            "chunks_count": len(chunks),
            "types": dict(types_count),
            "top_keywords": file_keywords.get(fk, []),
            "top_summary": (file_summaries or {}).get(fk, ""),
            "importance": _compute_file_importance(cids, chunk_stats),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    total_chunks = sum(f["chunks_count"] for f in files_dict.values())
    graph = {
        "version": "2.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kb_id": kb_id,
        "stats": {
            "total_files": len(files_dict),
            "total_chunks": total_chunks,
            "total_cross_file_edges": len(file_edges),
        },
        "files": files_dict,
        "edges": file_edges,
    }
    logger.info(
        f"📊 Knowledge graph built: "
        f"{graph['stats']['total_files']} files, "
        f"{graph['stats']['total_chunks']} chunks, "
        f"{graph['stats']['total_cross_file_edges']} edges"
    )
    return graph


def update_knowledge_graph(
    existing_graph: Dict[str, Any],
    new_chunks: List[Dict[str, Any]],
    existing_chunks: List[Dict[str, Any]],
    kb_id: str = "",
    connect_config: Optional[Dict[str, Any]] = None,
    chunk_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    file_summaries: Optional[Dict[str, str]] = None,
    new_connections: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Incrementally update a file-level knowledge graph with new chunks."""
    if chunk_stats is None:
        chunk_stats = {}

    all_combined = existing_chunks + new_chunks
    file_chunks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    chunk_to_file: Dict[str, str] = {}
    chunk_paths: Dict[str, str] = {}
    for chunk in all_combined:
        cid = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        fk = _get_source_file(chunk)
        if fk:
            file_chunks[fk].append(chunk)
            if cid:
                chunk_to_file[cid] = fk
                chunk_paths[cid] = chunk.get("path", "")

    file_keywords = _compute_tfidf_top_keywords(file_chunks)

    if new_connections is None:
        new_connections = _incremental_connections(
            new_chunks=new_chunks,
            existing_chunks=existing_chunks,
            config=connect_config,
        )
    new_chunk_edges = _connections_to_edges(new_connections)
    existing_file_edges = existing_graph.get("edges", [])
    new_file_edges = _aggregate_file_level_edges(
        new_chunk_edges, chunk_to_file, chunk_paths
    )

    # Merge file edges
    merged_map: Dict[Tuple[str, str], Dict] = {}
    for edge in existing_file_edges + new_file_edges:
        pk = tuple(sorted([edge["source"], edge["target"]]))
        if pk not in merged_map:
            merged_map[pk] = edge
        else:
            old = merged_map[pk]
            # Merge connections, dedup by chunk pair
            all_conns = old.get("top_connections", []) + edge.get("top_connections", [])
            seen = set()
            deduped = []
            for c in all_conns:
                pair = (c.get("source_chunk", ""), c.get("target_chunk", ""))
                if pair not in seen:
                    seen.add(pair)
                    deduped.append(c)
            deduped.sort(key=lambda x: x.get("score", 0), reverse=True)
            tc = old["connection_count"] + edge["connection_count"]
            scores = [c.get("score", 0) for c in deduped]
            avg = sum(scores) / len(scores) if scores else 0
            merged_map[pk] = {
                "source": pk[0],
                "target": pk[1],
                "connection_count": tc,
                "avg_score": round(avg, 4),
                "top_connections": deduped[:10],
            }
    all_file_edges = sorted(
        merged_map.values(), key=lambda x: x["connection_count"], reverse=True
    )

    existing_files = existing_graph.get("files", {})
    files_dict = {}
    new_file_count = 0
    for fk, chunks in file_chunks.items():
        types_count: Dict[str, int] = defaultdict(int)
        cids = []
        for c in chunks:
            types_count[c.get("type", "text")] += 1
            cid = str(c.get("chunk_id") or c.get("know_id", ""))
            if cid:
                cids.append(cid)
        created_at = existing_files.get(fk, {}).get(
            "created_at", datetime.now(timezone.utc).isoformat()
        )
        if fk not in existing_files:
            new_file_count += 1
        files_dict[fk] = {
            "chunks_count": len(chunks),
            "types": dict(types_count),
            "top_keywords": file_keywords.get(fk, []),
            "top_summary": (file_summaries or {}).get(fk, "") or existing_files.get(fk, {}).get("top_summary", ""),
            "importance": _compute_file_importance(cids, chunk_stats),
            "created_at": created_at,
        }

    total_chunks = sum(f["chunks_count"] for f in files_dict.values())
    graph = {
        "version": "2.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kb_id": kb_id or existing_graph.get("kb_id", ""),
        "stats": {
            "total_files": len(files_dict),
            "total_chunks": total_chunks,
            "total_cross_file_edges": len(all_file_edges),
        },
        "files": files_dict,
        "edges": all_file_edges,
    }
    logger.info(
        f"📊 Knowledge graph updated: "
        f"+{new_file_count} files → "
        f"total {graph['stats']['total_files']} files, "
        f"{graph['stats']['total_chunks']} chunks, "
        f"{graph['stats']['total_cross_file_edges']} edges"
    )
    return graph


# ─── Configuration ────────────────────────────────────────────────────────────

KNOWHERE_HOME = os.path.expanduser(os.environ.get("KNOWHERE_HOME", "~/.knowhere"))


def _get_kb_dir(kb_id: str) -> str:
    """Get the knowledge base directory path."""
    return os.path.join(KNOWHERE_HOME, kb_id)


def _get_kg_path(kb_id: str) -> str:
    """Get the knowledge_graph.json path for a KB."""
    return os.path.join(_get_kb_dir(kb_id), "knowledge_graph.json")


def _get_stats_path(kb_id: str) -> str:
    """Get the chunk_stats.json path for a KB."""
    return os.path.join(_get_kb_dir(kb_id), "chunk_stats.json")


def _empty_knowledge_graph(kb_id: str) -> Dict[str, Any]:
    """Build an empty v2 knowledge graph for a KB with no local chunks."""
    return {
        "version": "2.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kb_id": kb_id,
        "stats": {
            "total_files": 0,
            "total_chunks": 0,
            "total_cross_file_edges": 0,
        },
        "files": {},
        "edges": [],
    }


# ─── Chunk Usage Tracking ─────────────────────────────────────────────────────


def load_chunk_stats(kb_id: str) -> Dict[str, Dict[str, Any]]:
    """Load chunk usage stats from chunk_stats.json."""
    path = _get_stats_path(kb_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def record_chunk_hits(
    kb_id: str,
    chunk_ids: List[str],
) -> None:
    """
    Record that chunks were accessed (returned in search results).
    Updates hit_count and last_hit for each chunk.

    Args:
        kb_id: Knowledge base ID.
        chunk_ids: List of chunk IDs that were hit.
    """
    stats = load_chunk_stats(kb_id)
    now = datetime.now(timezone.utc).isoformat()

    for cid in chunk_ids:
        if cid not in stats:
            stats[cid] = {
                "hit_count": 0,
                "first_hit": now,
                "last_hit": now,
                "created_at": now,
            }
        stats[cid]["hit_count"] += 1
        stats[cid]["last_hit"] = now

    path = _get_stats_path(kb_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def relevance_score(
    hit_count: int,
    last_hit_iso: str,
    half_life_days: float = 30.0,
) -> float:
    """
    Compute relevance score with exponential decay.
    Higher hit_count + more recent access → higher score.

    Args:
        hit_count: Number of times this chunk was accessed.
        last_hit_iso: ISO timestamp of last access.
        half_life_days: Days until relevance halves.

    Returns:
        Decay-weighted score.
    """
    try:
        last_hit_dt = datetime.fromisoformat(last_hit_iso)
        days_since = (datetime.now(timezone.utc) - last_hit_dt).total_seconds() / 86400
    except (ValueError, TypeError):
        days_since = 0

    decay = math.exp(-0.693 * days_since / half_life_days)
    return hit_count * decay


# ─── File I/O ─────────────────────────────────────────────────────────────────


def save_knowledge_graph(graph: Dict[str, Any], output_path: str) -> str:
    """Save knowledge graph to a JSON file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Knowledge graph saved: {output_path}")
    return output_path


def load_knowledge_graph(path: str) -> Optional[Dict[str, Any]]:
    """Load an existing knowledge graph from JSON file."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load knowledge graph from {path}: {e}")
        return None


def _save_chunks(chunks: List[Dict[str, Any]], output_path: str) -> None:
    """Save chunks data to a JSON file in the standard format."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"chunks": chunks}, f, ensure_ascii=False, indent=2)


def _load_chunks(path: str) -> List[Dict[str, Any]]:
    """Load chunks from a stored chunks.json file."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "chunks" in data:
            return data["chunks"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, IOError):
        pass
    return []


def extract_chunks_from_graph(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Reconstruct minimal chunk dicts from graph for incremental matching.
    This is a last-resort fallback when subdirectory chunks.json files are unavailable.
    v2.0: no chunk-level data in graph; returns empty list.
    Legacy: falls back to node_index or nodes array.
    """
    chunks = []
    # v2.0: files dict doesn't store chunk IDs, return empty
    if graph.get("version", "").startswith("2."):
        return chunks
    # Legacy v1.x: handle node_index
    node_index = graph.get("node_index", {})
    if node_index:
        for chunk_id, file_key in node_index.items():
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "path": file_key,
                    "content": "",
                    "metadata": {"keywords": []},
                }
            )
        return chunks
    # Legacy: handle old nodes array
    for node in graph.get("nodes", []):
        chunks.append(
            {
                "chunk_id": node["id"],
                "path": node.get("path", ""),
                "content": node.get("content_preview", ""),
                "metadata": {"keywords": node.get("keywords", [])},
            }
        )
    return chunks


# ─── Chunk ID Dedup ──────────────────────────────────────────────────────────


def _dedup_chunks_by_content(
    new_chunks: List[Dict[str, Any]],
    existing_chunks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Filter new_chunks: discard any whose chunk_id already exists in existing_chunks.

    Since all parsers now generate deterministic know_id (content-hash based),
    identical content always produces the same chunk_id. Simple set comparison
    replaces the old strip+hash pipeline.

    Returns:
        List of new chunks that have no chunk_id duplicate in existing_chunks.
    """
    existing_ids = {
        str(c.get("chunk_id") or c.get("know_id", "")) for c in existing_chunks
    }
    existing_ids.discard("")

    deduped = []
    skipped = 0
    for chunk in new_chunks:
        cid = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        if cid and cid in existing_ids:
            skipped += 1
        else:
            deduped.append(chunk)

    if skipped > 0:
        logger.info(
            f"📊 chunk dedup: {skipped} duplicate chunks skipped "
            f"(by chunk_id), {len(deduped)} chunks to add"
        )
    return deduped


def _load_all_chunks_from_kb(kb_dir: str) -> List[Dict[str, Any]]:
    """
    Load all chunks from per-file chunks.json files under a KB directory.
    Tags each chunk with _source_file = subdirectory name (= source document).
    """
    all_chunks = []
    for entry in os.listdir(kb_dir):
        entry_path = os.path.join(kb_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        chunks_file = os.path.join(entry_path, "chunks.json")
        if os.path.isfile(chunks_file):
            loaded = _load_chunks(chunks_file)
            for chunk in loaded:
                chunk["_source_file"] = entry
            all_chunks.extend(loaded)
    return all_chunks


def _source_files_from_chunks(chunks: List[Dict[str, Any]]) -> set[str]:
    """Return the source-file set represented by loaded KB chunks."""
    return {
        str(chunk.get("_source_file") or "").strip()
        for chunk in chunks
        if str(chunk.get("_source_file") or "").strip()
    }


def _prune_chunk_stats(kb_id: str, chunks: List[Dict[str, Any]]) -> None:
    """Remove chunk_stats entries whose chunks no longer exist on disk."""
    stats_path = _get_stats_path(kb_id)
    if not os.path.exists(stats_path):
        return

    stats = load_chunk_stats(kb_id)
    live_chunk_ids = {
        str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        for chunk in chunks
        if chunk.get("chunk_id") or chunk.get("know_id")
    }
    pruned = {cid: data for cid, data in stats.items() if cid in live_chunk_ids}
    if len(pruned) == len(stats):
        return

    os.makedirs(os.path.dirname(stats_path), exist_ok=True)
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)
    logger.info(
        f"📊 Chunk stats pruned: {len(stats) - len(pruned)} stale chunks removed"
    )


def sync_knowledge_graph_with_local_files(
    kb_id: str,
    connect_config: Optional[Dict[str, Any]] = None,
    summary_use_llm: bool = False,
) -> Dict[str, Any]:
    """Synchronize knowledge_graph.json with current ~/.knowhere/{kb_id} files.

    This is intentionally a no-op when graph files match on-disk document
    directories. If a user manually deletes a local parsed document directory,
    the graph is rebuilt from remaining chunks and stale chunk_stats entries are
    removed.
    """
    from app.services.connect_builder.builder import build_connections
    from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries

    kb_dir = _get_kb_dir(kb_id)
    kg_path = _get_kg_path(kb_id)
    os.makedirs(kb_dir, exist_ok=True)

    existing_graph = load_knowledge_graph(kg_path)
    chunks_on_disk = _load_all_chunks_from_kb(kb_dir)
    disk_files = _source_files_from_chunks(chunks_on_disk)
    graph_files = set((existing_graph or {}).get("files", {}).keys())

    if existing_graph is not None and graph_files == disk_files:
        _prune_chunk_stats(kb_id, chunks_on_disk)
        return existing_graph

    removed_files = sorted(graph_files - disk_files)
    added_files = sorted(disk_files - graph_files)
    logger.info(
        "📊 Syncing Knowledge Graph with local files: "
        f"removed={removed_files}, added={added_files}"
    )

    if not chunks_on_disk:
        graph = _empty_knowledge_graph(kb_id)
        save_knowledge_graph(graph, kg_path)
        _prune_chunk_stats(kb_id, [])
        return graph

    try:
        file_summaries = enrich_doc_nav_summaries(
            kb_dir=kb_dir,
            source_file=None,
            use_llm=summary_use_llm,
        )
    except Exception as e:
        logger.warning(f"sync enrich_doc_nav_summaries failed: {e}")
        file_summaries = {}

    stats = load_chunk_stats(kb_id)
    connections = build_connections(chunks_on_disk, connect_config)
    _merge_related_connections_into_chunks(chunks_on_disk, connections)
    _save_chunks_by_source_file(kb_dir, chunks_on_disk)
    graph = build_knowledge_graph(
        all_chunks=chunks_on_disk,
        connections=connections,
        kb_id=kb_id,
        chunk_stats=stats,
        file_summaries=file_summaries,
    )
    save_knowledge_graph(graph, kg_path)
    _prune_chunk_stats(kb_id, chunks_on_disk)
    return graph


# ─── MCP Auto-Registration ───────────────────────────────────────────────────


def _get_mcp_server_path() -> str:
    """Get the absolute path to the MCP server script.

    Points to the consolidated knowhere-mcp/server.py (unified server
    with both Cloud API and local search tools).
    """
    # Navigate from graph_builder.py → project root → knowhere-mcp/server.py
    # graph_builder.py is at: apps/worker/app/services/connect_builder/
    project_root = os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "..",
            "..",
            "..",
            "..",
        )
    )
    return os.path.join(project_root, "knowhere-mcp", "server.py")


def _auto_register_mcp() -> None:
    """
    Detect installed Agent products and auto-register the knowhere MCP server.
    Only runs on first deploy (when ~/.knowhere/ is freshly created).

    Supported products:
      - Cursor: ~/.cursor/mcp.json
      - Claude Code: ~/.claude.json (project-level) or ~/.claude/claude_code_config.json
    """
    mcp_server_path = os.path.normpath(_get_mcp_server_path())
    home = os.path.expanduser("~")

    knowhere_mcp_entry = {
        "command": "python3",
        "args": [mcp_server_path],
        "env": {
            "KNOWHERE_API_KEY": os.environ.get("KNOWHERE_API_KEY", ""),
        },
    }

    registered = []

    # ── Cursor ────────────────────────────────────────────────────────────
    cursor_mcp = os.path.join(home, ".cursor", "mcp.json")
    if os.path.isdir(os.path.join(home, ".cursor")):
        try:
            existing = {}
            if os.path.exists(cursor_mcp):
                with open(cursor_mcp, "r") as f:
                    existing = json.load(f)

            servers = existing.get("mcpServers", {})
            # Update even if "knowhere" exists (to point to new server)
            if "knowhere" not in servers or "mcp/knowhere_mcp_server" in str(
                servers.get("knowhere", {}).get("args", [])
            ):
                servers["knowhere"] = knowhere_mcp_entry
                existing["mcpServers"] = servers
                with open(cursor_mcp, "w") as f:
                    json.dump(existing, f, indent=2)
                registered.append("Cursor")
        except Exception as e:
            logger.debug(f"Cursor MCP registration skipped: {e}")

    # ── Claude Code ───────────────────────────────────────────────────────
    claude_config = os.path.join(home, ".claude.json")
    if os.path.exists(claude_config) or os.path.isdir(os.path.join(home, ".claude")):
        try:
            existing = {}
            if os.path.exists(claude_config):
                with open(claude_config, "r") as f:
                    existing = json.load(f)

            servers = existing.get("mcpServers", {})
            if "knowhere" not in servers or "mcp/knowhere_mcp_server" in str(
                servers.get("knowhere", {}).get("args", [])
            ):
                servers["knowhere"] = knowhere_mcp_entry
                existing["mcpServers"] = servers
                with open(claude_config, "w") as f:
                    json.dump(existing, f, indent=2)
                registered.append("Claude Code")
        except Exception as e:
            logger.debug(f"Claude Code MCP registration skipped: {e}")

    if registered:
        logger.info(f"🔌 MCP auto-registered for: {', '.join(registered)}")
    else:
        logger.debug("No Agent products detected for MCP auto-registration")


# ─── doc_nav section extraction for GraphNode persistence ────────────────────





# ─── One-Stop API ─────────────────────────────────────────────────────────────


def build_and_deploy(
    chunks: List[Dict[str, Any]],
    kb_id: str,
    parsed_output_dir: Optional[str] = None,
    connect_config: Optional[Dict[str, Any]] = None,
    rebuild_all: bool = True,
    summary_use_llm: bool = False,
) -> Dict[str, Any]:
    """
    One-stop knowledge graph build/update + deploy to ~/.knowhere/ + MCP register.

    This is the main entry point for callers (parse services, debug scripts, etc).
    Callers just provide chunks + kb_id; everything else is automatic.

    Flow:
      1. If parsed_output_dir provided → copy full parsed output to ~/.knowhere/{kb_id}/data/
      2. Check if ~/.knowhere/{kb_id}/knowledge_graph.json exists
         - No  → build_knowledge_graph() (full build)
           - rebuild_all=True  → scan KB dir for existing files, merge with new chunks
           - rebuild_all=False → only use the new chunks (ignore previous files)
         - Yes → update_knowledge_graph() (incremental)
      3. Save knowledge_graph.json to ~/.knowhere/{kb_id}/
      4. On first-ever deploy → _auto_register_mcp()

    Args:
        chunks: Parsed chunks from the current file.
        kb_id: Knowledge base identifier (e.g. dataset name).
        parsed_output_dir: Path to the parsed output directory (add_dir) containing
            images, tables, doc_nav.json etc. If provided, its contents are
            copied to ~/.knowhere/{kb_id}/data/{dirname}/.
        connect_config: Optional config overrides for connect_builder.
        rebuild_all: When knowledge_graph.json is missing, whether to scan the
            KB directory for existing chunk data and include them in the full
            rebuild. Defaults to True. Set to False to only process the new
            chunks (legacy behavior).
        summary_use_llm: If True, use LLM to generate coherent hierarchical
            summaries (slow, costs API tokens). If False (default), use
            lightweight title enumeration (e.g. "This section covers: Section 1,
            Section 2"). Only affects `top_summary` and `summary` fields.

    Returns:
        The knowledge graph dict.
    """
    import shutil

    from app.services.connect_builder.builder import build_connections

    kg_path = _get_kg_path(kb_id)
    kb_dir = _get_kb_dir(kb_id)

    # Detect if this is a first-ever deploy (for MCP registration)
    first_deploy = not os.path.exists(KNOWHERE_HOME)

    # Ensure directory exists
    os.makedirs(kb_dir, exist_ok=True)

    # Load existing state BEFORE deploy (to avoid counting new file's chunks twice)
    # Determine source_file early so we can exclude it from existing_chunks
    source_file = (
        os.path.basename(parsed_output_dir)
        if parsed_output_dir and os.path.isdir(parsed_output_dir)
        else None
    )
    existing_graph = load_knowledge_graph(kg_path)

    # Load chunks once and reuse — avoids the double-load where
    # sync_knowledge_graph_with_local_files internally calls
    # _load_all_chunks_from_kb and then we call it again.
    all_on_disk: List[Dict[str, Any]] = []
    if existing_graph is not None:
        all_on_disk = _load_all_chunks_from_kb(kb_dir)
        disk_files = _source_files_from_chunks(all_on_disk)
        graph_files = set(existing_graph.get("files", {}).keys())

        if graph_files != disk_files:
            # Disk state diverged from graph → full sync rebuild.
            # sync will reload chunks internally (it backfills connect_to
            # metadata), so we reload afterwards to pick up changes.
            existing_graph = sync_knowledge_graph_with_local_files(
                kb_id=kb_id,
                connect_config=connect_config,
                summary_use_llm=summary_use_llm,
            )
            all_on_disk = _load_all_chunks_from_kb(kb_dir)
        else:
            # Files match — fast-path: just prune stale chunk_stats.
            _prune_chunk_stats(kb_id, all_on_disk)

    if existing_graph is not None:
        if not all_on_disk:
            existing_chunks = extract_chunks_from_graph(existing_graph)
        else:
            # Exclude chunks from the current source file — they may already
            # be on disk if parsed_output_dir is inside kb_dir (debug_parse).
            # Without this filter, _dedup_chunks_by_content would treat them
            # as "existing" and skip the incremental update entirely.
            existing_chunks = (
                [c for c in all_on_disk if c.get("_source_file") != source_file]
                if source_file
                else all_on_disk
            )
    else:
        existing_chunks = []

    # ── Deploy parsed output (images, tables, hierarchy, etc.) ──
    if parsed_output_dir and os.path.isdir(parsed_output_dir) and source_file is not None:
        deploy_target = os.path.join(kb_dir, source_file)

        # Skip copy if parsed output is already in the target location
        parsed_abs = os.path.normpath(os.path.abspath(parsed_output_dir))
        target_abs = os.path.normpath(os.path.abspath(deploy_target))
        if parsed_abs == target_abs:
            logger.info(
                f"📂 Parsed output already at target: {deploy_target} (skip copy)"
            )
        else:
            if os.path.exists(deploy_target):
                shutil.rmtree(deploy_target)
            shutil.copytree(parsed_output_dir, deploy_target)
            # Delete ZIP files from deployed directory (no longer needed)
            import glob

            for zip_file in glob.glob(os.path.join(deploy_target, "*.zip")):
                os.remove(zip_file)
            logger.info(f"📂 Parsed output deployed: {deploy_target}")

    # Tag all chunks with source document for correct file-level grouping
    if source_file:
        for chunk in chunks:
            chunk["_source_file"] = source_file

    # ── Generate hierarchical summaries ──
    from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries
    try:
        file_summaries = enrich_doc_nav_summaries(
            kb_dir=kb_dir,
            source_file=source_file,
            use_llm=summary_use_llm,
        )
    except Exception as e:
        logger.warning(f"doc_nav summary enrichment failed: {e}")
        file_summaries = {}

    # Load chunk_stats for importance calculation
    stats = load_chunk_stats(kb_id)

    if existing_graph is None:
        # ── First build: full ──
        if rebuild_all:
            # Scan KB dir for existing chunk data (deploy already happened,
            # so the new file's chunks are on disk if parsed_output_dir was given).
            all_on_disk = _load_all_chunks_from_kb(kb_dir)
            if source_file and all_on_disk:
                # New file already deployed → all_on_disk includes it, no merge needed
                all_chunks = all_on_disk
            else:
                # New file not deployed to disk (no parsed_output_dir),
                # or KB dir was empty → merge in-memory chunks with disk data.
                # Dedup by chunk_id to prevent double-counting.
                seen_ids = {
                    str(c.get("chunk_id") or c.get("know_id", "")) for c in all_on_disk
                }
                extra = [
                    c
                    for c in chunks
                    if str(c.get("chunk_id") or c.get("know_id", "")) not in seen_ids
                ]
                all_chunks = all_on_disk + extra
            # Full rebuild: generate summaries for ALL files, not just source_file
            from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries as _enrich_nav
            try:
                all_nav_summaries = _enrich_nav(
                    kb_dir=kb_dir,
                    source_file=None,
                    use_llm=summary_use_llm,
                )
                file_summaries.update(all_nav_summaries)
            except Exception as e:
                logger.warning(f"Full rebuild enrich_doc_nav_summaries failed: {e}")
            logger.info(
                f"📊 rebuild Knowledge Graph "
                f"(rebuild_all=True, {len(all_chunks)} chunks from KB dir) ..."
            )
        else:
            all_chunks = chunks
            logger.info(
                "📊 rebuild Knowledge Graph (rebuild_all=False, new chunks only) ..."
            )

        connections = build_connections(all_chunks, connect_config)
        _merge_related_connections_into_chunks(all_chunks, connections)
        _save_chunks_by_source_file(kb_dir, all_chunks)
        stats_chunks = all_chunks
        graph = build_knowledge_graph(
            all_chunks=all_chunks,
            connections=connections,
            kb_id=kb_id,
            chunk_stats=stats,
            file_summaries=file_summaries,
        )
    else:
        # ── Incremental update ──
        # Content-hash dedup: discard new chunks identical to existing ones
        # to preserve established graph edges and relationships.
        deduped_new = _dedup_chunks_by_content(chunks, existing_chunks)
        if len(deduped_new) == 0:
            logger.info(
                "📊 All new chunks are duplicates of existing data, "
                "skipping incremental update"
            )
            stats_chunks = existing_chunks
            graph = existing_graph
            # Still inject summaries even if chunks unchanged
            for fk, fdata in graph.get("files", {}).items():
                if file_summaries and fk in file_summaries and not fdata.get("top_summary"):
                    fdata["top_summary"] = file_summaries[fk]
        else:
            logger.info(
                f"📊 incremental update Knowledge Graph "
                f"({len(deduped_new)} new, {len(chunks) - len(deduped_new)} skipped) ..."
            )
            related_connections = _incremental_connections(
                new_chunks=deduped_new,
                existing_chunks=existing_chunks,
                config=connect_config,
            )
            _merge_related_connections_into_chunks(
                existing_chunks + deduped_new, related_connections
            )
            _save_chunks_by_source_file(kb_dir, existing_chunks + deduped_new)
            stats_chunks = existing_chunks + deduped_new
            graph = update_knowledge_graph(
                existing_graph=existing_graph,
                new_chunks=deduped_new,
                existing_chunks=existing_chunks,
                kb_id=kb_id,
                connect_config=connect_config,
                chunk_stats=stats,
                file_summaries=file_summaries,
                new_connections=related_connections,
            )

    # Save graph
    save_knowledge_graph(graph, kg_path)

    # Initialize chunk_stats.json with created_at for all new chunks
    stats_path = _get_stats_path(kb_id)
    now = datetime.now(timezone.utc).isoformat()
    updated = False
    for chunk in stats_chunks:
        cid = str(chunk.get("chunk_id") or chunk.get("know_id", ""))
        if cid and cid not in stats:
            stats[cid] = {
                "hit_count": 0,
                "first_hit": None,
                "last_hit": None,
                "created_at": now,
            }
            updated = True
    if updated:
        os.makedirs(os.path.dirname(stats_path), exist_ok=True)
        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        logger.info(f"📊 Chunk stats initialized: {len(stats)} chunks tracked")

    logger.info(
        f"✅ Knowledge Graph deployed to {kb_dir}: "
        f"{graph['stats']['total_files']} files, "
        f"{graph['stats']['total_chunks']} chunks, "
        f"{graph['stats']['total_cross_file_edges']} edges"
    )

    # Auto-register MCP on first deploy
    if first_deploy:
        try:
            _auto_register_mcp()
        except Exception as e:
            logger.debug(f"MCP auto-registration skipped: {e}")

    return graph
