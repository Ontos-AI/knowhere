"""Map-nav exit bridge: kept_chunks → referenced_chunks (+ scores).

Refs always come from ``NavSnapshot.chunk_ref_index`` (DB-original
section_path / job_id), never from remounted provider paths.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from shared.services.retrieval.nav_snapshot import NavSnapshot


def dedupe_referenced_chunks(refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inline of workflow/reference_projection.WorkflowReferenceProjection.dedupe."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ref in refs:
        document_id = str(ref.get("document_id") or "").strip()
        chunk_id = str(ref.get("chunk_id") or "").strip()
        section_path = str(ref.get("section_path") or "").strip()
        file_path = str(ref.get("file_path") or "").strip()
        key = (
            f"{document_id}:{chunk_id}:{section_path}:{file_path}"
            if document_id and chunk_id
            else str(ref)
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(ref))
    return out


def _lookup_chunk_meta(
    snapshot: NavSnapshot,
    chunk_id: str,
    *,
    document_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    doc = str(document_id or "").strip()
    if doc:
        scoped = snapshot.chunk_ref_index.get(f"{doc}:{chunk_id}")
        if isinstance(scoped, dict):
            return scoped
    meta = snapshot.chunk_ref_index.get(chunk_id)
    return meta if isinstance(meta, dict) else None


def _ref_for(
    chunk_id: str,
    *,
    snapshot: NavSnapshot,
    score: Optional[float],
    document_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    meta = _lookup_chunk_meta(snapshot, chunk_id, document_id=document_id)
    if not meta:
        return None
    ref: dict[str, Any] = {
        "chunk_id": chunk_id,
        "document_id": meta.get("document_id"),
        "chunk_type": meta.get("chunk_type") or "text",
        "section_path": meta.get("section_path"),
        "file_path": meta.get("file_path") or "",
        "job_id": meta.get("job_id"),
    }
    if score is not None:
        ref["score"] = float(score)
    return ref


def _expand_node_to_chunk_ids(
    *,
    node_id: str,
    section_id: Optional[str],
    snapshot: NavSnapshot,
    document_id: Optional[str] = None,
) -> list[str]:
    nid = str(node_id or "").strip()
    if not nid:
        return []
    if _lookup_chunk_meta(snapshot, nid, document_id=document_id) is not None:
        return [nid]

    sid = str(section_id or "").strip() or nid
    if sid.endswith("__self"):
        sid = sid[: -len("__self")]
    elif sid.endswith("__path"):
        sid = sid[: -len("__path")]
    if nid.endswith("__self"):
        sid = nid[: -len("__self")]
    elif nid.endswith("__path"):
        sid = nid[: -len("__path")]

    out: list[str] = []
    for unit in snapshot.provider.self_units(sid) or ():
        cid = str(getattr(unit, "chunk_id", "") or "").strip()
        if cid and _lookup_chunk_meta(snapshot, cid, document_id=document_id) is not None:
            out.append(cid)
    return out


def build_referenced_chunks(
    episode: Any,
    snapshot: NavSnapshot,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Expand ``episode.kept_chunks`` into API refs + score index.

    Returns ``(refs, score_by_chunk_id)``.
    """
    score_by_node: dict[str, float] = {}
    for item in getattr(episode, "scored_chunks", None) or ():
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        chunk, score = item[0], item[1]
        nid = str(getattr(chunk, "node_id", "") or "").strip()
        if not nid:
            continue
        try:
            score_by_node[nid] = float(score)
        except (TypeError, ValueError):
            continue

    refs: list[dict[str, Any]] = []
    score_by_chunk_id: dict[str, float] = {}
    for chunk in getattr(episode, "kept_chunks", None) or ():
        nid = str(getattr(chunk, "node_id", "") or "").strip()
        sid = getattr(chunk, "section_id", None)
        doc_id = str(getattr(chunk, "doc_id", "") or "").strip() or None
        node_score = score_by_node.get(nid)
        for chunk_id in _expand_node_to_chunk_ids(
            node_id=nid,
            section_id=str(sid) if sid else None,
            snapshot=snapshot,
            document_id=doc_id,
        ):
            if node_score is not None:
                prev = score_by_chunk_id.get(chunk_id)
                score_by_chunk_id[chunk_id] = (
                    max(prev, node_score) if prev is not None else node_score
                )
            ref = _ref_for(
                chunk_id,
                snapshot=snapshot,
                score=score_by_chunk_id.get(chunk_id),
                document_id=doc_id,
            )
            if ref is not None:
                refs.append(ref)

    return dedupe_referenced_chunks(refs), score_by_chunk_id
