from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, Text, cast, delete, false, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from shared.services.retrieval.publication_trace_stage import trace_publication_stage

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
from shared.services.retrieval.graph.keywords import (
    KEYWORD_SCORE_WEIGHT,
    MIN_ENTITY_OVERLAP,
    MIN_KEYWORD_OVERLAP,
    MIN_SCORE_THRESHOLD,
    compute_entity_score,
    compute_keyword_score,
    compute_tfidf_keywords,
    extract_document_top_summary,
    get_normalized_entity_set,
    get_normalized_keyword_set,
    normalize_keyword,
)

logger = logging.getLogger(__name__)


@dataclass
class GraphScope:
    user_id: str
    namespace: str


def _parse_stored_entities(stored: object) -> set[tuple[str, str]]:
    """Reconstruct a ``{(type, text)}`` set from a node's stored ``top_entities``.

    Stored form is a list of ``"type:text"`` strings (or bare ``"text"`` when the
    entity was untyped). Splits on the first colon only, since entity text may
    itself contain colons.
    """
    if not isinstance(stored, list):
        return set()
    result: set[tuple[str, str]] = set()
    for item in stored:
        token = str(item).strip()
        if not token:
            continue
        if ":" in token:
            etype, etext = token.split(":", 1)
            result.add((etype.strip(), etext.strip()))
        else:
            result.add(("", token))
    return result


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
        top_summary: str | None = None,
        trace: PublicationTrace | None = None,
    ) -> None:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        with trace_publication_stage(trace, "graph_prepare"):
            document = db.execute(
                select(corpusStorage.Document).where(corpusStorage.Document.document_id == document_id)
            ).scalar_one_or_none()
            if document is None:
                return
            if document.user_id != corpusStorage.resolve_owner(user_id) or document.namespace != namespace:
                raise ValueError(
                    "Graph publication scope does not match the document owner"
                )

            chunk_meta_rows = list(
                db.execute(
                    select(corpusStorage.DocumentChunk.chunk_type, corpusStorage.DocumentChunk.chunk_metadata)
                    .where(corpusStorage.DocumentChunk.document_id == document_id)
                    .where(corpusStorage.DocumentChunk.job_result_id == job_result_id)
                ).all()
            )
        chunk_metadata_list = [row[1] or {} for row in chunk_meta_rows]

        # Compute document-level metadata (aligned with KB knowledge_graph.json files dict)
        with trace_publication_stage(trace, "graph_prepare"):
            top_keywords = compute_tfidf_keywords(chunk_metadata_list)
            new_doc_kws = get_normalized_keyword_set(chunk_metadata_list)
            # Typed entities (§4.4) — higher-precision cross-document link signal.
            new_doc_entities = get_normalized_entity_set(chunk_metadata_list)

            types_breakdown: dict[str, int] = defaultdict(int)
            for chunk_type, _ in chunk_meta_rows:
                types_breakdown[chunk_type or "text"] += 1
            chunks_count = len(chunk_meta_rows)

            resolved_top_summary = str(top_summary or "").strip()
            if not resolved_top_summary:
                resolved_top_summary = extract_document_top_summary(chunk_metadata_list)

        # ── Clean up old graph data for this document ──
        with trace_publication_stage(trace, "graph_persist"):
            if not corpusStorage.is_demo:
                self.remove_document_graph(db, scope=None, document_id=document_id)

        # Serialize typed entities as ["type:text", ...] for storage in node props
        # so peers can reconstruct the set without a separate schema.
        with trace_publication_stage(trace, "graph_prepare"):
            top_entities = sorted(
                f"{etype}:{etext}" if etype else etext
                for etype, etext in new_doc_entities
            )

        # ── Create document-level node (no section nodes — aligned with KB KG) ──
        document_node_id = f"doc:{document_id}:{job_result_id}" if corpusStorage.is_demo else f"doc:{document_id}"
        with trace_publication_stage(trace, "graph_persist"):
            db.add(
                corpusStorage.GraphNode(
                    node_id=document_node_id,
                    user_id=corpusStorage.resolve_owner(user_id),
                    namespace=namespace,
                    node_kind="document",
                    owner_document_id=document_id,
                    job_result_id=job_result_id,
                    ref_document_id=document_id,
                    ref_section_id=None,
                    properties={
                        "source_file_name": document.source_file_name,
                        "top_keywords": top_keywords,
                        "top_entities": top_entities,
                        "chunks_count": chunks_count,
                        "types": dict(types_breakdown),
                        "top_summary": resolved_top_summary,
                    },
                )
            )

        # ── Keyword-overlap-based cross-document edges ──
        # Only create edges where keyword overlap score >= threshold.
        candidate_terms = sorted(
            new_doc_kws
            | {
                f"{entity_type}:{entity_text}" if entity_type else entity_text
                for entity_type, entity_text in new_doc_entities
            }
        )
        with trace_publication_stage(trace, "graph_prepare"):
            peer_statement = (
                select(
                    corpusStorage.GraphNode.node_id,
                    corpusStorage.GraphNode.owner_document_id,
                    corpusStorage.GraphNode.properties,
                )
                .where(corpusStorage.GraphNode.user_id == corpusStorage.resolve_owner(user_id))
                .where(corpusStorage.GraphNode.namespace == namespace)
                .where(corpusStorage.GraphNode.node_kind == "document")
                .where(corpusStorage.GraphNode.owner_document_id != document_id)
            )
            if corpusStorage.is_demo:
                peer_statement = peer_statement.join(
                    corpusStorage.Document,
                    (corpusStorage.Document.document_id == corpusStorage.GraphNode.owner_document_id)
                    & (corpusStorage.Document.current_job_result_id == corpusStorage.GraphNode.job_result_id),
                ).where(corpusStorage.Document.status == "active")
            if candidate_terms:
                term_array = cast(candidate_terms, ARRAY(Text))
                properties_json = cast(corpusStorage.GraphNode.properties, JSONB)
                peer_statement = peer_statement.where(
                    or_(
                        properties_json.op("->")("top_keywords")
                        .op("?|")(term_array),
                        properties_json.op("->")("top_entities")
                        .op("?|")(term_array),
                    )
                )
            else:
                peer_statement = peer_statement.where(false())
            other_doc_nodes = list(db.execute(peer_statement).all())

        graph_edge_count = 0
        with trace_publication_stage(trace, "graph_prepare"):
            for peer_node_id, peer_document_id, peer_properties in other_doc_nodes:
                peer_props = peer_properties or {}

                edge_props = self._build_edge_properties(
                    new_doc_entities=new_doc_entities,
                    new_doc_kws=new_doc_kws,
                    peer_entities=_parse_stored_entities(
                        peer_props.get("top_entities", [])
                    ),
                    peer_keywords=peer_props.get("top_keywords", []),
                )
                if edge_props is None:
                    continue
                score = edge_props.pop("_score")

                edge_pair = tuple(sorted([document_id, peer_document_id]))
                db.add(
                    corpusStorage.GraphEdge(
                        edge_id=f"related:{document_node_id}<->{peer_node_id}" if corpusStorage.is_demo else f"related:{edge_pair[0]}<->{edge_pair[1]}",
                        user_id=corpusStorage.resolve_owner(user_id),
                        namespace=namespace,
                        edge_kind="related",
                        source_node_id=document_node_id,
                        target_node_id=peer_node_id,
                        owner_document_id=document_id,
                        job_result_id=job_result_id,
                        is_directed=False,
                        weight=round(score, 4),
                        properties=edge_props,
                    )
                )
                graph_edge_count += 1

        if trace is not None:
            trace.record_count("graph_edges", graph_edge_count)
        with trace_publication_stage(trace, "graph_persist"):
            db.flush()
        logger.info(
            f"publish_document_graph: doc={document_id} "
            f"keywords={len(top_keywords)} entities={len(top_entities)} "
            f"chunks={chunks_count}"
        )

    @staticmethod
    def _build_edge_properties(
        *,
        new_doc_entities: set[tuple[str, str]],
        new_doc_kws: set[str],
        peer_entities: set[tuple[str, str]],
        peer_keywords: list,
    ) -> dict | None:
        """Decide whether two documents link, preferring typed-entity overlap.

        Returns edge ``properties`` (with a private ``_score`` key) when the pair
        clears a threshold, else ``None``. Typed entities are tried first; when
        either side lacks entities we fall back to free-form keyword overlap so
        documents ingested before §4.4 still link.
        """
        # ── Primary: typed-entity overlap ──
        if new_doc_entities and peer_entities:
            shared_entities = new_doc_entities & peer_entities
            if len(shared_entities) >= MIN_ENTITY_OVERLAP:
                score = compute_entity_score(
                    shared_entities=shared_entities,
                    entities_a=new_doc_entities,
                    entities_b=peer_entities,
                    weight=KEYWORD_SCORE_WEIGHT,
                )
                if score >= MIN_SCORE_THRESHOLD:
                    return {
                        "_score": score,
                        "edge_basis": "entities",
                        "shared_entities": sorted(
                            f"{etype}:{etext}" if etype else etext
                            for etype, etext in shared_entities
                        ),
                        "connection_count": len(shared_entities),
                    }

        # ── Fallback: free-form keyword overlap ──
        # TODO: Once single-doc entity extraction is stable, migrate this to
        # typed-entity-only edges and remove the TF-IDF keyword overlap path.
        # The current keyword overlap rarely produces meaningful cross-doc links
        # now that entities have replaced free-form keywords in the extraction
        # pipeline. This won't crash — just means fewer/no cross-doc links until
        # the graph is upgraded to use entity-based matching exclusively.
        peer_kws: set[str] = set()
        for k in peer_keywords:
            normalized = normalize_keyword(str(k))
            if normalized:
                peer_kws.add(normalized)
        if not peer_kws or not new_doc_kws:
            return None
        shared_kws = new_doc_kws & peer_kws
        if len(shared_kws) < MIN_KEYWORD_OVERLAP:
            return None
        score = compute_keyword_score(
            shared_keywords=shared_kws,
            keywords_a=new_doc_kws,
            keywords_b=peer_kws,
            weight=KEYWORD_SCORE_WEIGHT,
        )
        if score < MIN_SCORE_THRESHOLD:
            return None
        return {
            "_score": score,
            "edge_basis": "keywords",
            "shared_keywords": sorted(shared_kws),
            "connection_count": len(shared_kws),
        }

    def remove_document_graph(
        self, db: Session, *, scope: GraphScope | None, document_id: str
    ) -> None:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace) if scope is not None else CorpusStorage.resolve_document(document_id)
        if corpusStorage.is_demo:
            # Archived revisions remain stored; RLS hides the archived source.
            return
        document_node_id = f"doc:{document_id}"
        edge_delete = delete(corpusStorage.GraphEdge).where(
            or_(
                corpusStorage.GraphEdge.owner_document_id == document_id,
                corpusStorage.GraphEdge.source_node_id == document_node_id,
                corpusStorage.GraphEdge.target_node_id == document_node_id,
            )
        )
        node_delete = delete(corpusStorage.GraphNode).where(
            corpusStorage.GraphNode.owner_document_id == document_id
        )
        if scope is not None:
            edge_delete = edge_delete.where(
                corpusStorage.GraphEdge.user_id == corpusStorage.resolve_owner(scope.user_id),
                corpusStorage.GraphEdge.namespace == scope.namespace,
            )
            node_delete = node_delete.where(
                corpusStorage.GraphNode.user_id == corpusStorage.resolve_owner(scope.user_id),
                corpusStorage.GraphNode.namespace == scope.namespace,
            )
        db.execute(edge_delete)
        db.execute(node_delete)
        db.flush()
