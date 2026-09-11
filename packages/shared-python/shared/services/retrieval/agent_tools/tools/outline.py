"""``corpus.outline`` — titles + summaries, no body text, no folding.

Reads ``document_sections`` (+ a ``document_chunks`` count aggregate) for one
document's current revision, optionally scoped to a ``section_path`` prefix
and depth-limited by the caller's own argument — never by a token budget
(see ``CORPUS_SCHEMA.md`` §6). This queries the live tables directly rather
than the compressed ``RetrievalNamespaceMapSnapshot``/serving-manifest blob:
that snapshot is namespace-wide and decoding it to read one document's
subtree would cost more than this document-scoped, index-backed query.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.search.lexical_text import normalize_section_path


@register_tool(
    name="corpus.outline",
    description=(
        "Return the section outline (title + summary + chunk_count) for one "
        "document, or the subtree under a section_path prefix, without any "
        "body text. Use for overviews, tables of contents, or picking where "
        "to look before calling read. Depth is limited by the 'depth' "
        "argument only, never truncated by a token budget."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "path_prefix": {
                "type": "string",
                "description": (
                    "DB section_path (' / '-joined, e.g. 'Chapter 1 / Section "
                    "1.1'). Omit or use 'Root' for the whole document."
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    "Max levels below path_prefix (or below the document root "
                    "when path_prefix is omitted) to include. Omit for the "
                    "full subtree."
                ),
            },
        },
        "required": ["document_id"],
    },
)
async def outline(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    document_id = str(args.get("document_id") or "").strip()
    if not document_id:
        return ToolResult(text="", error="outline requires document_id")
    depth_raw = args.get("depth")
    depth = int(depth_raw) if depth_raw is not None else None
    if depth is not None and depth < 0:
        return ToolResult(text="", error="depth must be >= 0")
    raw_prefix = str(args.get("path_prefix") or "").strip()
    prefix = normalize_section_path(raw_prefix) if raw_prefix else "Root"

    document = (
        await ctx.db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .where(Document.user_id == ctx.user_id)
            .where(Document.namespace == ctx.namespace)
            .where(Document.status == "active")
            .where(ctx.document_scope.predicate(Document.document_id))
        )
    ).scalar_one_or_none()
    if document is None or not document.current_job_result_id:
        return ToolResult(text="", error=f"unknown document_id: {document_id}")
    job_result_id = document.current_job_result_id

    section_stmt = (
        select(DocumentSection)
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
        .order_by(DocumentSection.sort_order, DocumentSection.section_id)
    )
    all_sections = list((await ctx.db.execute(section_stmt)).scalars().all())

    base_level: int | None = None
    if prefix == "Root":
        base_level = 0
        scoped = all_sections
    else:
        prefix_section = next(
            (s for s in all_sections if s.section_path == prefix), None
        )
        if prefix_section is None:
            return ToolResult(
                text="", error=f"unknown path_prefix for {document_id}: {prefix}"
            )
        base_level = prefix_section.section_level
        scoped = [
            s
            for s in all_sections
            if s.section_path == prefix or s.section_path.startswith(f"{prefix} / ")
        ]

    if depth is not None:
        scoped = [s for s in scoped if (s.section_level - base_level) <= depth]

    section_ids = [s.section_id for s in scoped]
    chunk_counts: dict[str, int] = {}
    if section_ids:
        count_rows = await ctx.db.execute(
            select(DocumentChunk.section_id, func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.section_id.in_(section_ids))
            .group_by(DocumentChunk.section_id)
        )
        chunk_counts = {str(sid): int(count) for sid, count in count_rows.all()}

    nodes: list[dict[str, Any]] = []
    lines: list[str] = []
    for section in scoped:
        relative_depth = section.section_level - base_level
        node = {
            "section_id": section.section_id,
            "section_path": section.section_path,
            "section_title": section.section_title,
            "section_level": section.section_level,
            "relative_depth": relative_depth,
            "summary": section.summary or "",
            "chunk_count": chunk_counts.get(section.section_id, 0),
        }
        nodes.append(node)
        indent = "  " * max(relative_depth, 0)
        line = f"{indent}- {node['section_title'] or node['section_path']} (chunks={node['chunk_count']})"
        if node["summary"]:
            line += f"\n{indent}  summary: {node['summary']}"
        lines.append(line)

    text = (
        f"document={document.source_file_name} ({document_id}) "
        f"sections={len(nodes)}\n" + "\n".join(lines)
    )
    return ToolResult(
        text=text,
        payload={"document_id": document_id, "sections": nodes},
        refs=[
            {"document_id": document_id, "section_path": node["section_path"]}
            for node in nodes
        ],
    )
