from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from shared.services.retrieval.scoring.knowhere_hybrid import score_persisted_corpus_many
from shared.services.retrieval.scoring.score_units import _walk_tree

_logger = logging.getLogger(__name__)


def _count_tree_shape(
    tree_by_doc: Dict[
        str,
        Tuple[Dict[str, List[str]], Set[str], Dict[str, str]],
    ],
) -> Tuple[int, int, int]:
    """Return reachable section nodes, parent-child edges, and leaves."""
    section_nodes: int = sum(len(value[0]) for value in tree_by_doc.values())
    section_edges: int = sum(
        sum(len(children) for children in value[0].values())
        for value in tree_by_doc.values()
    )
    leaf_sections: int = sum(len(value[1]) for value in tree_by_doc.values())
    return section_nodes, section_edges, leaf_sections

def _collect_descendant_leaves(
    section_id: str,
    children_map: Dict[str, List[str]],
    leaves: Set[str],
) -> List[str]:
    out: List[str] = []

    def rec(sid: str) -> None:
        kids = children_map.get(sid) or []
        if not kids:
            if sid in leaves:
                out.append(sid)
            return
        for kid in kids:
            rec(kid)

    rec(section_id)
    return out


def _pool_unit_scores_to_tree(
    children_map: Dict[str, List[str]],
    leaves: Set[str],
    unit_scores: Dict[str, float],
) -> Dict[str, float]:
    """MAX-pool globally comparable unit scores onto one document tree."""
    # ``_walk_tree`` inserts every parent before its children. Reversing that
    # order is therefore a postorder traversal without allocating descendant
    # lists or revisiting nodes.
    map_scores: Dict[str, float] = {}
    descendant_leaf_max: Dict[str, float] = {}
    for section_id in reversed(children_map):
        kids = children_map.get(section_id) or []
        if not kids:
            leaf_score = float(unit_scores.get(section_id, 0.0) or 0.0)
            descendant_leaf_max[section_id] = leaf_score
            score = leaf_score
        else:
            child_leaf_scores = [
                descendant_leaf_max.get(kid, float(unit_scores.get(kid, 0.0) or 0.0))
                for kid in kids
            ]
            leaf_score = max(child_leaf_scores, default=0.0)
            descendant_leaf_max[section_id] = leaf_score
            self_score = float(unit_scores.get(f"{section_id}__self", 0.0) or 0.0)
            score = max(leaf_score, self_score)
        map_scores[section_id] = float(score)
    # Preserve the legacy behavior for leaf ids that are not present in the
    # children map (defensive support for sparse providers).
    for leaf_id in leaves:
        map_scores.setdefault(leaf_id, float(unit_scores.get(leaf_id, 0.0) or 0.0))
    return map_scores


def compute_map_scores(
    ts: Any,
    *,
    doc_id: str,
    query: str,
    root_ids: Optional[Sequence[str]] = None,
) -> Dict[str, float]:
    """Leaf path+content scores + parent max-pool (self_only only if interstitial)."""
    map_scores, _unit_scores = compute_map_and_unit_scores(
        ts, doc_id=doc_id, query=query, root_ids=root_ids
    )
    return map_scores


def compute_map_and_unit_scores(
    ts: Any,
    *,
    doc_id: str,
    query: str,
    root_ids: Optional[Sequence[str]] = None,
    namespace: Optional[str] = None,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Return (section map_scores, unit hybrid scores keyed by chunk_id)."""
    del root_ids
    return compute_corpus_map_and_unit_scores(
        ts, doc_ids=[doc_id], query=query, namespace=namespace
    )


def compute_corpus_map_and_unit_scores(
    ts: Any,
    *,
    doc_ids: Sequence[str],
    query: str,
    namespace: Optional[str] = None,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Globally score every unit across documents, then MAX-pool onto the tree.

    All documents share one BM25 corpus, path/content normalization, channel
    ranking, and RRF pass. Document-level scores are keyed by bare ``document_id``.
    """
    return compute_corpus_map_and_unit_scores_many(
        ts,
        doc_ids=doc_ids,
        queries=[query],
        namespace=namespace,
    ).get(query, ({}, {}))


def compute_corpus_map_and_unit_scores_many(
    ts: Any,
    *,
    doc_ids: Sequence[str],
    queries: Sequence[str],
    namespace: Optional[str] = None,
) -> Dict[str, Tuple[Dict[str, float], Dict[str, float]]]:
    """Globally score several queries with one replay of the corpus units."""
    unique_queries = list(dict.fromkeys(str(query) for query in queries))
    if not unique_queries:
        return {}

    valid_doc_ids: List[str] = []
    seen_doc_ids: Set[str] = set()
    for raw in doc_ids:
        doc_id = str(raw or "").strip()
        if not doc_id or doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)
        valid_doc_ids.append(doc_id)

    del namespace  # Dense scoring is intentionally disabled for the corpus path.

    # Tree shape is query-independent; reuse it across the episode's two
    # scoring passes (user query + per-subgoal retrieval_query) on the same
    # ToolSpace instead of re-walking every document each time.
    tree_cache = getattr(ts, "_mapnav_tree_cache", None)
    if not isinstance(tree_cache, dict):
        tree_cache = {}
        setattr(ts, "_mapnav_tree_cache", tree_cache)
    tree_by_doc: Dict[
        str,
        Tuple[Dict[str, List[str]], Set[str], Dict[str, str]],
    ] = {}
    tree_started = time.perf_counter()
    for doc_id in valid_doc_ids:
        cached = tree_cache.get(doc_id)
        if cached is None:
            root_ids = list(ts.sections_for_doc(doc_id))
            cached = _walk_tree(ts, doc_id, root_ids)
            tree_cache[doc_id] = cached
        tree_by_doc[doc_id] = cached
    section_nodes, section_edges, leaf_sections = _count_tree_shape(tree_by_doc)
    _logger.info(
        "retrieval mapnav phase=tree_build seconds=%.3f documents=%d "
        "section_nodes=%d section_edges=%d leaf_sections=%d",
        time.perf_counter() - tree_started,
        len(valid_doc_ids),
        section_nodes,
        section_edges,
        leaf_sections,
    )

    persisted_loader = getattr(ts, "load_persisted_score_corpus", None)
    loader_started = time.perf_counter()
    persisted_corpus = (
        persisted_loader(valid_doc_ids, unique_queries)
        if callable(persisted_loader)
        else None
    )
    _logger.info(
        "retrieval mapnav phase=index_load seconds=%.3f persisted=%s",
        time.perf_counter() - loader_started,
        persisted_corpus is not None,
    )
    score_started = time.perf_counter()
    unit_scores_by_query = (
        score_persisted_corpus_many(persisted_corpus, unique_queries)
        if persisted_corpus is not None
        else {query: {} for query in unique_queries}
    )
    _logger.info(
        "retrieval mapnav phase=unit_scoring persisted=%s seconds=%.3f units=%d queries=%d",
        persisted_corpus is not None,
        time.perf_counter() - score_started,
        sum(len(scores) for scores in unit_scores_by_query.values()),
        len(unique_queries),
    )
    results: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = {}
    pooling_started = time.perf_counter()
    for query in unique_queries:
        unit_scores = unit_scores_by_query.get(query, {})
        map_scores: Dict[str, float] = {}
        for doc_id in valid_doc_ids:
            children_map, leaves, _titles = tree_by_doc[doc_id]
            doc_map_scores = _pool_unit_scores_to_tree(
                children_map, leaves, unit_scores
            )
            map_scores.update(doc_map_scores)
            doc_max = max(
                (float(value) for value in doc_map_scores.values()),
                default=0.0,
            )
            map_scores[doc_id] = doc_max
        results[query] = (map_scores, unit_scores)
    _logger.info(
        "retrieval mapnav phase=map_pooling seconds=%.3f documents=%d "
        "section_nodes=%d section_edges=%d leaf_sections=%d",
        time.perf_counter() - pooling_started,
        len(valid_doc_ids),
        section_nodes,
        section_edges,
        leaf_sections,
    )
    return results


def unit_id_to_section_id(unit_id: str) -> str:
    """Map scoring unit id (leaf or `{sid}__self`) to the section on the map."""
    uid = str(unit_id or "").strip()
    if uid.endswith("__self"):
        return uid[: -len("__self")]
    return uid


def select_map_highlights(unit_scores: Dict[str, float], k: int = 6) -> List[str]:
    """TOP-K section ids by unit hybrid score (stable tie-break on unit id)."""
    limit = max(0, int(k))
    if limit <= 0 or not unit_scores:
        return []
    ranked = sorted(
        unit_scores.items(),
        key=lambda kv: (-float(kv[1] or 0.0), str(kv[0])),
    )
    out: List[str] = []
    seen: Set[str] = set()
    for uid, _score in ranked:
        sid = unit_id_to_section_id(str(uid))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
        if len(out) >= limit:
            break
    return out


def relight_map_for_query(
    ts: Any,
    *,
    doc_id: str,
    query: str,
    top_k: int = 6,
) -> Tuple[Dict[str, float], Dict[str, float], List[str]]:
    """Re-score the whole shared map against ``query``.

    An empty ``doc_id`` means the corpus root, where document ids are map nodes
    and ``ts.document_ids()`` is already restricted to the episode's corpus.
    """
    doc = str(doc_id or "").strip()
    if doc:
        map_scores, unit_scores = compute_map_and_unit_scores(
            ts, doc_id=doc, query=query
        )
    else:
        doc_ids = [str(d) for d in (ts.document_ids() or ()) if str(d).strip()]
        if not doc_ids:
            return {}, {}, []
        map_scores, unit_scores = compute_corpus_map_and_unit_scores(
            ts, doc_ids=doc_ids, query=query
        )
    return map_scores, unit_scores, select_map_highlights(unit_scores, k=int(top_k))


def relight_maps_for_queries(
    ts: Any,
    *,
    doc_id: str,
    queries: Sequence[str],
    top_k: int = 6,
) -> Dict[str, Tuple[Dict[str, float], Dict[str, float], List[str]]]:
    """Re-score a shared map for several queries with one corpus replay."""
    unique_queries = list(dict.fromkeys(str(query) for query in queries))
    if not unique_queries:
        return {}
    doc = str(doc_id or "").strip()
    if doc:
        return {
            query: relight_map_for_query(
                ts,
                doc_id=doc,
                query=query,
                top_k=top_k,
            )
            for query in unique_queries
        }

    doc_ids = [str(value) for value in (ts.document_ids() or ()) if str(value).strip()]
    if not doc_ids:
        return {}
    scored = compute_corpus_map_and_unit_scores_many(
        ts,
        doc_ids=doc_ids,
        queries=unique_queries,
    )
    return {
        query: (
            map_scores,
            unit_scores,
            select_map_highlights(unit_scores, k=int(top_k)),
        )
        for query, (map_scores, unit_scores) in scored.items()
    }
