from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from shared.models.database.document import (
    Document,
    DocumentChunk,
    GraphEdge,
    GraphNode,
)
from shared.services.retrieval.graph_keywords import (
    KEYWORD_SCORE_WEIGHT,
    MIN_KEYWORD_OVERLAP,
    MIN_SCORE_THRESHOLD,
    compute_keyword_score,
    compute_tfidf_keywords,
    extract_document_top_summary,
    get_normalized_keyword_set,
    normalize_keyword,
)

logger = logging.getLogger(__name__)


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

    def publish_document_graph(
        self,
        db: Session,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
        job_result_id: str,
    ) -> None:
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
        top_keywords = compute_tfidf_keywords(chunk_metadata_list)
        new_doc_kws = get_normalized_keyword_set(chunk_metadata_list)

        types_breakdown: dict[str, int] = defaultdict(int)
        for chunk_type, _ in chunk_meta_rows:
            types_breakdown[chunk_type or 'text'] += 1
        chunks_count = len(chunk_meta_rows)

        top_summary = extract_document_top_summary(chunk_metadata_list)

        # ── Clean up old graph data for this document ──
        self.remove_document_graph(
            db,
            scope=GraphScope(user_id=user_id, namespace=namespace),
            document_id=document_id,
        )

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
                normalized = normalize_keyword(str(k))
                if normalized:
                    peer_kws.add(normalized)

            if not peer_kws or not new_doc_kws:
                continue

            # Find shared keywords
            shared_kws = new_doc_kws & peer_kws
            if len(shared_kws) < MIN_KEYWORD_OVERLAP:
                continue

            # Compute character-length-weighted score
            score = compute_keyword_score(
                shared_keywords=shared_kws,
                keywords_a=new_doc_kws,
                keywords_b=peer_kws,
                weight=KEYWORD_SCORE_WEIGHT,
            )
            if score < MIN_SCORE_THRESHOLD:
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

    def remove_document_graph(
        self, db: Session, *, scope: GraphScope | None, document_id: str
    ) -> None:
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
