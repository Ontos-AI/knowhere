"""``corpus.neighbors`` — document-level ``related`` graph edges.

Document-to-document only (§4 of ``CORPUS_SCHEMA.md``): no section- or
entity-level graph nodes exist yet. Edges are undirected and were written by
``DocumentGraphService.publish_document_graph`` with ``shared_entities`` (typed
entity overlap, preferred) or ``shared_keywords`` (TF-IDF fallback).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from shared.models.database.document import Document, GraphEdge, GraphNode
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
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
)
async def neighbors(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    document_id = str(args.get("document_id") or "").strip()
    if not document_id:
        return ToolResult(text="", error="neighbors requires document_id")

    document = (
        await ctx.db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .where(Document.user_id == ctx.user_id)
            .where(Document.namespace == ctx.namespace)
            .where(Document.status == "active")
        )
    ).scalar_one_or_none()
    if document is None:
        return ToolResult(text="", error=f"unknown document_id: {document_id}")

    node_id = f"doc:{document_id}"
    edges = (
        (
            await ctx.db.execute(
                select(GraphEdge)
                .where(GraphEdge.user_id == ctx.user_id)
                .where(GraphEdge.namespace == ctx.namespace)
                .where(GraphEdge.edge_kind == "related")
                .where(
                    or_(
                        GraphEdge.source_node_id == node_id,
                        GraphEdge.target_node_id == node_id,
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
                select(GraphNode).where(GraphNode.node_id.in_(peer_node_ids))
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
