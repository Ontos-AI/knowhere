"""``corpus.neighbors`` — document-level ``related`` graph edges.

Document-to-document only (§4 of ``CORPUS_SCHEMA.md``): no section- or
entity-level graph nodes exist yet. Edges are undirected and were written by
``DocumentGraphService.publish_document_graph`` with ``shared_entities`` (typed
entity overlap, preferred) or ``shared_keywords`` (TF-IDF fallback).
"""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext

from typing import Any

from sqlalchemy import or_, select

from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)


@register_tool(
    name="corpus.neighbors",
    description=(
        "Return documents related to the given document via the persisted "
        "document graph (typed-entity overlap, falling back to TF-IDF "
        "keyword overlap), along with the shared terms that justify each "
        "edge. Document-level only — there is no section- or entity-level "
        "graph yet."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "Document to find related documents for.",
            }
        },
        "required": ["document_id"],
        "additionalProperties": False,
    },
)
async def neighbors(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(ctx.namespace)
    document_id = str(args.get("document_id") or "").strip()
    if not document_id:
        return ToolResult(text="", error="neighbors requires document_id")

    document = (
        await ctx.db.execute(
            select(corpusStorage.Document)
            .where(corpusStorage.Document.document_id == document_id)
            .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(ctx.user_id))
            .where(corpusStorage.Document.namespace == ctx.namespace)
            .where(corpusStorage.Document.status == "active")
            .where(ctx.document_scope.predicate(corpusStorage.Document.document_id))
        )
    ).scalar_one_or_none()
    if document is None:
        return ToolResult(text="", error=f"unknown document_id: {document_id}")

    revision: str | None = CorpusRevisionContext.resolve_revision(document_id, document.current_job_result_id)
    node_id: str = f"doc:{document_id}:{revision}" if corpusStorage.is_demo else f"doc:{document_id}"
    allowed_nodes = select(corpusStorage.GraphNode.node_id).join(
        corpusStorage.Document,
        (corpusStorage.Document.document_id == corpusStorage.GraphNode.owner_document_id)
        & (corpusStorage.GraphNode.job_result_id == CorpusRevisionContext.build_revision_column(corpusStorage.Document)),
    ).where(
        ctx.document_scope.predicate(corpusStorage.GraphNode.owner_document_id),
        corpusStorage.Document.user_id == corpusStorage.resolve_owner(ctx.user_id),
        corpusStorage.Document.namespace == ctx.namespace,
        corpusStorage.Document.status == "active",
    )
    edges = (
        (
            await ctx.db.execute(
                select(corpusStorage.GraphEdge)
                .where(corpusStorage.GraphEdge.user_id == corpusStorage.resolve_owner(ctx.user_id))
                .where(corpusStorage.GraphEdge.namespace == ctx.namespace)
                .where(corpusStorage.GraphEdge.edge_kind == "related")
                .where(corpusStorage.GraphEdge.source_node_id.in_(allowed_nodes))
                .where(corpusStorage.GraphEdge.target_node_id.in_(allowed_nodes))
                .where(
                    or_(
                        corpusStorage.GraphEdge.source_node_id == node_id,
                        corpusStorage.GraphEdge.target_node_id == node_id,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if not edges:
        return ToolResult(text="neighbors=0", payload={"neighbors": []})

    peer_node_ids = {
        edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
        for edge in edges
    }
    peer_nodes = (
        (
            await ctx.db.execute(
                select(corpusStorage.GraphNode).where(
                    corpusStorage.GraphNode.node_id.in_(peer_node_ids),
                    ctx.document_scope.predicate(corpusStorage.GraphNode.owner_document_id),
                )
            )
        )
        .scalars()
        .all()
    )
    peer_by_id = {n.node_id: n for n in peer_nodes}

    neighbor_list: list[dict[str, Any]] = []
    for edge in sorted(edges, key=lambda e: -(e.weight or 0.0)):
        peer_node_id = (
            edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
        )
        peer = peer_by_id.get(peer_node_id)
        if peer is None:
            continue
        props = edge.properties or {}
        peer_props = peer.properties or {}
        neighbor_list.append(
            {
                "document_id": peer.owner_document_id,
                "source_file_name": peer_props.get("source_file_name"),
                "weight": edge.weight,
                "edge_basis": props.get("edge_basis"),
                "shared_entities": props.get("shared_entities"),
                "shared_keywords": props.get("shared_keywords"),
                "connection_count": props.get("connection_count"),
            }
        )

    lines = [f"neighbors={len(neighbor_list)}"]
    for n in neighbor_list:
        shared = n["shared_entities"] or n["shared_keywords"] or []
        lines.append(
            f"- {n['source_file_name']} ({n['document_id']}) weight={n['weight']} "
            f"basis={n['edge_basis']} shared={shared}"
        )

    return ToolResult(
        text="\n".join(lines),
        payload={"neighbors": neighbor_list},
        refs=[{"document_id": n["document_id"]} for n in neighbor_list],
    )
