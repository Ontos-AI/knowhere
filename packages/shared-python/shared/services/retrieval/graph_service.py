from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentChunk, DocumentSection, GraphEdge, GraphNode
from shared.models.database.job_result import JobResult

logger = logging.getLogger(__name__)

_SECTION_EXCLUSION_PAGE_MULTIPLIER = 2

# ── Keyword overlap config for document-level publication graph ──
_MIN_KEYWORD_OVERLAP = 3
_KEYWORD_SCORE_WEIGHT = 1.0
_MIN_SCORE_THRESHOLD = 0.8
_CROSS_FILE_ONLY = True
_MAX_CONTENT_OVERLAP = 0.8


def _build_lexical_match_predicate(query: str):
    like = f'%{query}%'
    return (
        DocumentChunk.content_lexical_text.ilike(like)
        | DocumentChunk.path_lexical_text.ilike(like)
    )


def is_excluded_section(
    *,
    document_id: str | None,
    section_path: str | None,
    exclude_sections: Iterable[dict[str, str]],
) -> bool:
    document_id = str(document_id or '').strip()
    section_path = str(section_path or '').strip()
    if not document_id or not section_path:
        return False
    for item in exclude_sections:
        if not isinstance(item, dict):
            continue
        exc_doc = str(item.get('document_id') or '').strip()
        exc_path = str(item.get('section_path') or '').strip()
        if document_id == exc_doc and (section_path == exc_path or section_path.startswith(exc_path + ' / ')):
            return True
    return False


# ── Keyword extraction & scoring for document-level publication graph ──

def _normalize_keyword(keyword: str) -> str:
    """Normalize a keyword: lowercase, strip, collapse spaces."""
    kw = keyword.lower().strip()
    return re.sub(r'\s+', ' ', kw)


def _extract_keywords_from_chunk_metadata(meta: dict) -> list[str]:
    """Extract keywords from chunk metadata."""
    if not isinstance(meta, dict):
        return []
    # Try metadata.keywords
    kws = meta.get('keywords', [])
    if isinstance(kws, list) and kws:
        return [str(k) for k in kws if k]
    # Fallback: tokens
    tokens = meta.get('tokens', [])
    if isinstance(tokens, list) and tokens:
        return [str(t) for t in tokens if t and len(str(t)) > 1]
    return []


def _compute_tfidf_keywords(
    chunk_metadata_list: list[dict[str, Any]],
    top_k: int = 10,
) -> list[str]:
    """Compute TF-IDF keywords from chunk metadata."""
    df_count: dict[str, int] = {}
    tf_count: dict[str, int] = {}
    total = len(chunk_metadata_list) or 1
    for meta in chunk_metadata_list:
        kws = _extract_keywords_from_chunk_metadata(meta)
        seen: set[str] = set()
        for k in kws:
            if len(str(k)) <= 1 or re.match(r'^\d+[.,%]*$', str(k)):
                continue
            lower = _normalize_keyword(str(k))
            if not lower:
                continue
            tf_count[lower] = tf_count.get(lower, 0) + 1
            if lower not in seen:
                df_count[lower] = df_count.get(lower, 0) + 1
                seen.add(lower)
    scored = [
        (term, freq * (math.log(total / (df_count.get(term, 1))) + 1))
        for term, freq in tf_count.items()
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:top_k]]


def _compute_keyword_score(
    shared_kws: set[str],
    kws_a: set[str],
    kws_b: set[str],
    weight: float = 1.0,
) -> float:
    """Character-length-weighted keyword overlap score.

    Longer tokens contribute more: '施工现场'(4) has 2x weight of '交底'(2).
    Formula: score = weight * sum(len(kw) for shared) / min(sum(len) for A, sum(len) for B)
    """
    weighted_a = sum(len(k) for k in kws_a)
    weighted_b = sum(len(k) for k in kws_b)
    denominator = min(weighted_a, weighted_b)
    if denominator == 0:
        return 0.0
    weighted_shared = sum(len(k) for k in shared_kws)
    return weight * weighted_shared / denominator


def _get_normalized_keyword_set(chunk_metadata_list: list[dict[str, Any]]) -> set[str]:
    """Collect all normalized keywords from chunk metadata for a document."""
    result: set[str] = set()
    for meta in chunk_metadata_list:
        for k in _extract_keywords_from_chunk_metadata(meta):
            normalized = _normalize_keyword(str(k))
            if normalized and len(normalized) > 1 and not re.match(r'^\d+[.,%]*$', normalized):
                result.add(normalized)
    return result


def _extract_document_top_summary(
    chunk_metadata_list: list[dict[str, Any]],
    section_titles: Sequence[str],
) -> str:
    """Extract document_top_summary from chunk metadata.

    The summary is injected by kb_tasks.py via load_nav_top_summary()
    at parse time, so it should always be present.  If missing, return empty
    string rather than fabricating a low-quality fallback.
    """
    for meta in chunk_metadata_list:
        if not isinstance(meta, dict):
            continue
        summary = str(meta.get('document_top_summary') or '').strip()
        if summary:
            return summary
    return ''


@dataclass
class GraphScope:
    user_id: str
    namespace: str


class DocumentGraphService:
    """Write-side graph publication over persisted graph_nodes/graph_edges.

    Aligned with KB's knowledge_graph.json structure:
    - Only document-level nodes (no section nodes)
    - Document nodes carry rich metadata: top_keywords, chunks_count, types, top_summary
    - Edges are keyword-overlap-based cross-document connections with meaningful scores
    """

    def publish_document_graph(self, db: Session, *, user_id: str, namespace: str, document_id: str, job_result_id: str) -> None:
        document = db.execute(
            select(Document).where(Document.document_id == document_id)
        ).scalar_one_or_none()
        if document is None:
            return

        # ── Gather chunk metadata for keyword extraction ──
        chunk_meta_rows = list(
            db.execute(
                select(DocumentChunk.chunk_type, DocumentChunk.chunk_metadata)
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
            ).all()
        )
        chunk_metadata_list = [row[1] or {} for row in chunk_meta_rows]

        # Compute document-level metadata (aligned with KB knowledge_graph.json files dict)
        top_keywords = _compute_tfidf_keywords(chunk_metadata_list)
        new_doc_kws = _get_normalized_keyword_set(chunk_metadata_list)

        types_breakdown: dict[str, int] = defaultdict(int)
        for chunk_type, _ in chunk_meta_rows:
            types_breakdown[chunk_type or 'text'] += 1
        chunks_count = len(chunk_meta_rows)

        sections = [
            section_title
            for section_title in db.execute(
                select(DocumentSection.section_title)
                .where(DocumentSection.document_id == document_id)
                .where(DocumentSection.job_result_id == job_result_id)
                .where(DocumentSection.section_level <= 2)
                .order_by(DocumentSection.sort_order)
            ).scalars()
            if section_title is not None
        ]
        top_summary = _extract_document_top_summary(chunk_metadata_list, sections)

        # ── Clean up old graph data for this document ──
        self.remove_document_graph(db, scope=GraphScope(user_id=user_id, namespace=namespace), document_id=document_id)

        # ── Create document-level node (no section nodes — aligned with KB KG) ──
        document_node_id = f"doc:{document_id}"
        db.add(
            GraphNode(
                node_id=document_node_id,
                user_id=user_id,
                namespace=namespace,
                node_kind='document',
                owner_document_id=document_id,
                job_result_id=job_result_id,
                ref_document_id=document_id,
                ref_section_id=None,
                properties={
                    'source_file_name': document.source_file_name,
                    'top_keywords': top_keywords,
                    'chunks_count': chunks_count,
                    'types': dict(types_breakdown),
                    'top_summary': top_summary,
                },
            )
        )
        db.flush()

        # ── Keyword-overlap-based cross-document edges ──
        # Only create edges where keyword overlap score >= threshold.
        other_doc_nodes = list(
            db.execute(
                select(GraphNode)
                .where(GraphNode.user_id == user_id)
                .where(GraphNode.namespace == namespace)
                .where(GraphNode.node_kind == 'document')
                .where(GraphNode.owner_document_id != document_id)
            ).scalars()
        )

        for peer_node in other_doc_nodes:
            peer_props = peer_node.properties or {}
            peer_keywords = peer_props.get('top_keywords', [])

            # Build normalized keyword sets for comparison
            peer_kws: set[str] = set()
            for k in peer_keywords:
                normalized = _normalize_keyword(str(k))
                if normalized:
                    peer_kws.add(normalized)

            if not peer_kws or not new_doc_kws:
                continue

            # Find shared keywords
            shared_kws = new_doc_kws & peer_kws
            if len(shared_kws) < _MIN_KEYWORD_OVERLAP:
                continue

            # Compute character-length-weighted score
            score = _compute_keyword_score(
                shared_kws=shared_kws,
                kws_a=new_doc_kws,
                kws_b=peer_kws,
                weight=_KEYWORD_SCORE_WEIGHT,
            )
            if score < _MIN_SCORE_THRESHOLD:
                continue

            # Create edge with meaningful weight and metadata
            peer_doc_id = peer_node.owner_document_id
            edge_pair = tuple(sorted([document_id, peer_doc_id]))
            db.add(
                GraphEdge(
                    edge_id=f"related:{edge_pair[0]}<->{edge_pair[1]}",
                    user_id=user_id,
                    namespace=namespace,
                    edge_kind='related',
                    source_node_id=document_node_id,
                    target_node_id=peer_node.node_id,
                    owner_document_id=document_id,
                    job_result_id=job_result_id,
                    is_directed=False,
                    weight=round(score, 4),
                    properties={
                        'shared_keywords': sorted(shared_kws),
                        'connection_count': len(shared_kws),
                    },
                )
            )

        db.flush()
        logger.info(
            f"publish_document_graph: doc={document_id} "
            f"keywords={len(top_keywords)} chunks={chunks_count}"
        )

    def remove_document_graph(self, db: Session, *, scope: GraphScope | None, document_id: str) -> None:
        document_node_id = f"doc:{document_id}"
        edge_delete = delete(GraphEdge).where(
            or_(
                GraphEdge.owner_document_id == document_id,
                GraphEdge.source_node_id == document_node_id,
                GraphEdge.target_node_id == document_node_id,
            )
        )
        node_delete = delete(GraphNode).where(GraphNode.owner_document_id == document_id)
        if scope is not None:
            edge_delete = edge_delete.where(
                GraphEdge.user_id == scope.user_id,
                GraphEdge.namespace == scope.namespace,
            )
            node_delete = node_delete.where(
                GraphNode.user_id == scope.user_id,
                GraphNode.namespace == scope.namespace,
            )
        db.execute(edge_delete)
        db.execute(node_delete)
        db.flush()


class GraphQueryService:
    """Read-side graph service for document routing before canonical chunk hydration."""

    async def find_entry_documents(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        exclude_document_ids: Iterable[str] = (),
        exclude_sections: Iterable[dict[str, str]] = (),
    ) -> list[str]:
        query_lc = query.lower().strip()
        exclude_document_ids = set(exclude_document_ids)

        if query_lc:
            like = f'%{query_lc}%'
            stmt = (
                select(DocumentSection.document_id)
                .join(Document, (Document.document_id == DocumentSection.document_id) & (Document.current_job_result_id == DocumentSection.job_result_id))
                .where(Document.user_id == user_id)
                .where(Document.namespace == namespace)
                .where(Document.status == 'active')
                .where(
                    DocumentSection.section_title.ilike(like)
                    | DocumentSection.section_path.ilike(like)
                )
                .distinct()
            )
            if exclude_document_ids:
                stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
            for exc in (exclude_sections or ()):
                if not isinstance(exc, dict):
                    continue
                exc_doc = str(exc.get('document_id') or '').strip()
                exc_path = str(exc.get('section_path') or '').strip()
                if exc_doc and exc_path:
                    stmt = stmt.where(
                        ~((DocumentSection.document_id == exc_doc) & (
                            (DocumentSection.section_path == exc_path) |
                            DocumentSection.section_path.like(f'{exc_path} / %')
                        ))
                    )
            result = await db.execute(stmt)
            seen = [row[0] for row in result.all()]
            if seen:
                return seen

        return await self._find_documents_by_content(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query_lc,
            exclude_document_ids=exclude_document_ids,
        )

    async def _find_documents_by_content(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        exclude_document_ids: set[str],
    ) -> list[str]:
        like = f'%{query}%'
        stmt = (
            select(Document.document_id)
            .join(DocumentChunk, (DocumentChunk.document_id == Document.document_id) & (DocumentChunk.job_result_id == Document.current_job_result_id))
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
            .where(DocumentChunk.content_lexical_text.ilike(like))
        )
        if exclude_document_ids:
            stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
        result = await db.execute(stmt)
        seen: list[str] = []
        for (doc_id,) in result.all():
            if doc_id and doc_id not in seen:
                seen.append(doc_id)
        return seen

    async def collect_candidate_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        entry_document_ids: Sequence[str],
        query: str,
        top_k: int,
        exclude_sections: Iterable[dict[str, str]] = (),
    ) -> list[dict[str, Any]]:
        if not entry_document_ids:
            return []
        page_size = top_k
        if exclude_sections:
            page_size = max(top_k, top_k * _SECTION_EXCLUSION_PAGE_MULTIPLIER)
        base_stmt = (
            select(Document, DocumentChunk, DocumentSection, JobResult)
            .join(DocumentChunk, (DocumentChunk.document_id == Document.document_id) & (DocumentChunk.job_result_id == Document.current_job_result_id))
            .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
            .where(Document.document_id.in_(list(entry_document_ids)))
            .where(_build_lexical_match_predicate(query))
            .order_by(DocumentChunk.sort_order)
        )
        rows = []
        offset = 0
        while len(rows) < top_k:
            result = await db.execute(base_stmt.limit(page_size).offset(offset))
            result_rows = result.all()
            if not result_rows:
                break
            for document, chunk, section, job_result in result_rows:
                section_path = section.section_path if section else None
                if is_excluded_section(
                    document_id=document.document_id,
                    section_path=section_path,
                    exclude_sections=exclude_sections,
                ):
                    continue
                rows.append({
                    'document_id': document.document_id,
                    'chunk_id': chunk.chunk_id,
                    'section_id': chunk.section_id,
                    'section_path': section_path,
                    'source_file_name': document.source_file_name,
                    'chunk_type': chunk.chunk_type,
                    'content': chunk.content,
                    'score': 2.0,
                    'file_path': chunk.file_path,
                    'chunk_metadata': chunk.chunk_metadata or {},
                    'job_result_id': chunk.job_result_id,
                    'job_id': job_result.job_id if job_result else None,
                })
                if len(rows) >= top_k:
                    break
            if len(result_rows) < page_size:
                break
            offset += page_size
        return rows
