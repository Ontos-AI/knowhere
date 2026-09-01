from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .knowhere_hybrid import (
    build_content_search_text,
    build_path_search_text,
    build_term_search_text,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    score_persisted_corpus_many,
)
from .persisted_score_load import (
    average_idf_from_unit_dfs,
    build_channel_bm25_stats,
)

_logger = logging.getLogger(__name__)


def _build_legacy_score_corpus(ts: Any, doc_ids: Sequence[str]) -> PersistedScoreCorpus:
    """Build the retired in-memory scorer input when persisted indexes are absent."""
    raw_units: List[dict] = []
    for doc_id in doc_ids:
        raw_units.extend(build_score_units(ts, doc_id))
    frequencies: Dict[Tuple[str, str], Dict[str, int]] = {}
    unit_rows: List[dict] = []
    path_dfs: Dict[str, int] = {}
    content_dfs: Dict[str, int] = {}
    for unit in raw_units:
        unit_id = str(unit.get("chunk_id") or "").strip()
        if not unit_id:
            continue
        path_tokens = str(unit.get("path_search_text") or "").split()
        content_tokens = str(unit.get("content_search_text") or "").split()
        path_freq: Dict[str, int] = {}
        content_freq: Dict[str, int] = {}
        for token in path_tokens:
            path_freq[token] = path_freq.get(token, 0) + 1
        for token in content_tokens:
            content_freq[token] = content_freq.get(token, 0) + 1
        frequencies[(unit_id, "path")] = path_freq
        frequencies[(unit_id, "content")] = content_freq
        for token in path_freq:
            path_dfs[token] = path_dfs.get(token, 0) + 1
        for token in content_freq:
            content_dfs[token] = content_dfs.get(token, 0) + 1
        unit_rows.append(
            {
                "unit_id": unit_id,
                "path_length": len(path_tokens),
                "content_length": len(content_tokens),
            }
        )
    unit_count = len(unit_rows)
    return PersistedScoreCorpus(
        units=[
            PersistedScoreUnit(
                unit_id=str(row["unit_id"]),
                path_length=int(row["path_length"]),
                content_length=int(row["content_length"]),
                path_frequencies=frequencies[(str(row["unit_id"]), "path")],
                content_frequencies=frequencies[(str(row["unit_id"]), "content")],
            )
            for row in unit_rows
        ],
        path_stats=build_channel_bm25_stats(
            unit_rows=unit_rows,
            map_unit_id_field="unit_id",
            length_field="path_length",
            channel="path",
            query_tokens=list(path_dfs),
            frequencies=frequencies,
            average_idf=average_idf_from_unit_dfs(
                unit_count=unit_count, token_document_frequency=path_dfs
            ),
        ),
        content_stats=build_channel_bm25_stats(
            unit_rows=unit_rows,
            map_unit_id_field="unit_id",
            length_field="content_length",
            channel="content",
            query_tokens=list(content_dfs),
            frequencies=frequencies,
            average_idf=average_idf_from_unit_dfs(
                unit_count=unit_count, token_document_frequency=content_dfs
            ),
        ),
    )


def _children_ids(ts: Any, section_id: str, doc_id: str) -> List[str]:
    children_fn = getattr(ts, "_children_for_section_path", None)
    if not callable(children_fn):
        st = ts.get_structure(section_id)
        rows = st.get("children") or []
        return [
            str(r.get("section_id") or "").strip() for r in rows if r.get("section_id")
        ]
    rows = children_fn(section_id, doc_id, limit=100000)
    return [str(r.get("section_id") or "").strip() for r in rows if r.get("section_id")]


def _line_content(ts: Any, section_id: str, doc_id: str) -> str:
    """Raw line text for a section node (no truncation)."""
    idx = getattr(ts, "_idx", None)
    b = getattr(idx, "_bundles", {}).get(doc_id) if idx is not None else None
    if b is None:
        path_fn = getattr(ts, "path_titles", None)
        if callable(path_fn):
            path = str(path_fn(section_id, doc_id) or "").strip()
            return path.rsplit(" / ", 1)[-1] if path else ""
        st = ts.get_structure(section_id)
        return str(st.get("preview") or "").strip()
    loc = getattr(idx, "_node_to_doc_line", {}).get(section_id)
    if not loc:
        return ""
    _doc, line_idx = loc
    if line_idx < 0 or line_idx >= len(b.lines):
        return ""
    return str(b.lines[line_idx].content or "").strip()


def _ancestor_path_titles(ts: Any, section_id: str, doc_id: str) -> str:
    idx = getattr(ts, "_idx", None)
    if idx is None:
        # Provider-backed spaces expose the title chain directly; without this
        # the path channel would score every unit as empty.
        path_fn = getattr(ts, "path_titles", None)
        return str(path_fn(section_id, doc_id) or "") if callable(path_fn) else ""
    try:
        ancestors = list(idx.ancestor_line_node_ids(section_id))
    except Exception:
        ancestors = []
    titles: List[str] = []
    for aid in reversed(ancestors):
        if not str(aid).startswith(f"{doc_id}:"):
            continue
        titles.append(_line_content(ts, aid, doc_id))
    titles.append(_line_content(ts, section_id, doc_id))
    return " / ".join(t for t in titles if t)


def _self_only_text(ts: Any, section_id: str, doc_id: str) -> Tuple[str, bool]:
    """Return (self_text, has_interstitial_body).

    Interstitial means self_only span contains content beyond the heading line
    itself (structural: more than one line/chunk in the self span).
    """
    self_fn = getattr(ts, "materialize_self_only_chunks", None)
    if not callable(self_fn):
        return "", False
    chunks = list(self_fn(section_id, doc_id) or [])
    if not chunks:
        return "", False
    texts = [str(getattr(c, "text", "") or "").strip() for c in chunks]
    texts = [t for t in texts if t]
    if not texts:
        return "", False
    # Structural interstitial: self span covers more than the node heading line.
    has_interstitial = len(chunks) > 1
    return "\n".join(texts), has_interstitial


def _section_body_text(ts: Any, section_id: str, doc_id: str) -> str:
    """Heading + lines until first structural child (leaf body / parent self span)."""
    text, _ = _self_only_text(ts, section_id, doc_id)
    if text:
        return text
    return _line_content(ts, section_id, doc_id)


def _walk_tree(
    ts: Any,
    doc_id: str,
    root_ids: Sequence[str],
) -> Tuple[Dict[str, List[str]], Set[str], Dict[str, str]]:
    """Return children map, leaf ids, and title map for reachable nodes."""
    children_map: Dict[str, List[str]] = {}
    titles: Dict[str, str] = {}
    leaves: Set[str] = set()
    seen: Set[str] = set()

    def walk(sid: str) -> None:
        if not sid or sid in seen:
            return
        seen.add(sid)
        titles[sid] = _line_content(ts, sid, doc_id)
        kids = [c for c in _children_ids(ts, sid, doc_id) if c]
        children_map[sid] = kids
        if not kids:
            leaves.add(sid)
            return
        for kid in kids:
            walk(kid)

    for rid in root_ids:
        walk(rid)
    return children_map, leaves, titles


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
    map_scores = {
        leaf_id: float(unit_scores.get(leaf_id, 0.0) or 0.0) for leaf_id in leaves
    }

    def score_node(section_id: str) -> float:
        if section_id in map_scores:
            return map_scores[section_id]
        kids = children_map.get(section_id) or []
        if not kids:
            score = float(unit_scores.get(section_id, 0.0) or 0.0)
            map_scores[section_id] = score
            return score
        descendant_leaves = _collect_descendant_leaves(section_id, children_map, leaves)
        parts = [
            float(unit_scores.get(leaf_id, 0.0) or 0.0) for leaf_id in descendant_leaves
        ]
        self_key = f"{section_id}__self"
        if self_key in unit_scores:
            parts.append(float(unit_scores[self_key]))
        score = float(max(parts)) if parts else 0.0
        map_scores[section_id] = score
        return score

    for section_id in children_map:
        score_node(section_id)
    return map_scores


def build_score_units(
    ts: Any, doc_id: str, root_ids: Optional[Sequence[str]] = None
) -> List[dict]:
    """Build leaf (+ interstitial self_only) units for hybrid scoring."""
    if root_ids is None:
        root_ids = list(ts.sections_for_doc(doc_id))
    children_map, leaves, titles = _walk_tree(ts, doc_id, root_ids)
    units: List[dict] = []
    seen_unit_ids: Set[str] = set()

    for leaf_id in sorted(leaves):
        content = _section_body_text(ts, leaf_id, doc_id) or (
            titles.get(leaf_id) or _line_content(ts, leaf_id, doc_id)
        )
        path_text = _ancestor_path_titles(ts, leaf_id, doc_id)
        unit_id = leaf_id
        if unit_id in seen_unit_ids:
            continue
        seen_unit_ids.add(unit_id)
        title = titles.get(leaf_id) or _line_content(ts, leaf_id, doc_id)
        units.append(
            {
                "chunk_id": unit_id,
                "section_id": leaf_id,
                "kind": "leaf",
                "content": content,
                "path_text": path_text,
                "path_search_text": build_path_search_text(
                    section_path=path_text, section_title=title or content
                ),
                "content_search_text": build_content_search_text(content),
                "term_search_text": build_term_search_text(
                    content, path_text=path_text
                ),
            }
        )

    # Parents with interstitial self body.
    for sid, kids in children_map.items():
        if not kids:
            continue
        self_text, has_interstitial = _self_only_text(ts, sid, doc_id)
        if not has_interstitial or not self_text:
            continue
        unit_id = f"{sid}__self"
        if unit_id in seen_unit_ids:
            continue
        seen_unit_ids.add(unit_id)
        path_text = _ancestor_path_titles(ts, sid, doc_id)
        units.append(
            {
                "chunk_id": unit_id,
                "section_id": sid,
                "kind": "self_only",
                "content": self_text,
                "path_text": path_text,
                "path_search_text": build_path_search_text(
                    section_path=path_text, section_title=titles.get(sid) or ""
                ),
                "content_search_text": build_content_search_text(self_text),
                "term_search_text": build_term_search_text(
                    self_text, path_text=path_text
                ),
            }
        )
    return units


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
    _logger.info(
        "retrieval mapnav phase=tree_build seconds=%.3f documents=%d sections=%d",
        time.perf_counter() - tree_started,
        len(valid_doc_ids),
        sum(len(value[0]) for value in tree_by_doc.values()),
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
    if persisted_corpus is None:
        _logger.warning(
            "retrieval map index unavailable; using bounded legacy in-memory scorer "
            "documents=%d",
            len(valid_doc_ids),
        )
        persisted_corpus = _build_legacy_score_corpus(ts, valid_doc_ids)
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
        "retrieval mapnav phase=map_pooling seconds=%.3f documents=%d sections=%d",
        time.perf_counter() - pooling_started,
        len(valid_doc_ids),
        sum(len(value[0]) for value in tree_by_doc.values()),
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
