"""``corpus.node_filter`` — deterministic FOR-ALL/EXISTS/ANY/NOT predicate over sections.

Reuses the exact predicate compile/match semantics from
``nav.nav_node_filter`` (path/summary substring|regex, fields AND together,
terms OR together) — see that module's docstring — but walks
``document_sections`` rows for the requested documents' current revision
instead of the in-memory map-nav tree. No top-K: returns the full matched set
and its count, per ``CORPUS_SCHEMA.md`` §6.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.nav.nav_node_filter import (
    FieldPredicate,
    _compile_predicates,
    _node_matches,
    field_predicate,
)


@register_tool(
    name="corpus.node_filter",
    description=(
        "Deterministic FOR-ALL/EXISTS/ANY/NOT filter over section titles "
        "(section_path) and summaries — not body text (use corpus.grep for "
        "that). Predicates AND together across fields; terms within one "
        "field's 'terms' list OR together. Returns the complete matched set "
        "and its count, never a truncated top-K."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "document_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "predicates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": ["path", "summary"]},
                        "terms": {"type": "array", "items": {"type": "string"}},
                        "match": {
                            "type": "string",
                            "enum": ["substring", "regex"],
                            "default": "substring",
                        },
                    },
                    "required": ["field", "terms"],
                },
                "minItems": 1,
            },
            "chunk_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Narrow matched sections to those owning a body chunk of "
                    "one of these chunk_type values (e.g. ['page'] to filter "
                    "to page-track leaves only). Omit for no narrowing."
                ),
            },
        },
        "required": ["document_ids", "predicates"],
    },
)
async def node_filter(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    document_ids = [
        str(did).strip() for did in (args.get("document_ids") or []) if str(did).strip()
    ]
    if not document_ids:
        return ToolResult(text="", error="node_filter requires document_ids")
    raw_predicates = args.get("predicates") or []
    if not raw_predicates:
        return ToolResult(text="", error="node_filter requires predicates")

    predicates: list[FieldPredicate] = []
    for raw in raw_predicates:
        try:
            predicates.append(
                field_predicate(
                    raw.get("field"),
                    raw.get("terms") or [],
                    raw.get("match", "substring"),
                )
            )
        except ValueError as exc:
            return ToolResult(text="", error=str(exc))

    compiled, failed = _compile_predicates(predicates)
    if failed:
        return ToolResult(
            text=f"failed_predicates={failed}",
            payload={"cardinality": 0, "matched_sections": [], "failed_predicates": failed},
            error="one or more predicates failed to compile",
        )

    documents = (
        (
            await ctx.db.execute(
                select(Document)
                .where(Document.document_id.in_(document_ids))
                .where(Document.user_id == ctx.user_id)
                .where(Document.namespace == ctx.namespace)
                .where(Document.status == "active")
            )
        )
        .scalars()
        .all()
    )
    revision_by_doc = {
        d.document_id: d.current_job_result_id for d in documents if d.current_job_result_id
    }
    if not revision_by_doc:
        return ToolResult(text="", error="no active documents found for document_ids")

    revision_pairs = list(revision_by_doc.items())
    sections = (
        (
            await ctx.db.execute(
                select(DocumentSection).where(
                    DocumentSection.document_id.in_([d for d, _ in revision_pairs])
                )
            )
        )
        .scalars()
        .all()
    )
    sections = [
        s for s in sections if revision_by_doc.get(s.document_id) == s.job_result_id
    ]

    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    }
    if chunk_types:
        chunk_rows = await ctx.db.execute(
            select(DocumentChunk.section_id, DocumentChunk.chunk_type).where(
                DocumentChunk.document_id.in_([d for d, _ in revision_pairs]),
                DocumentChunk.job_result_id.in_([r for _, r in revision_pairs]),
            )
        )
        allowed_section_ids = {
            str(section_id)
            for section_id, chunk_type in chunk_rows.all()
            if section_id and str(chunk_type or "").strip().lower() in chunk_types
        }
        sections = [s for s in sections if s.section_id in allowed_section_ids]

    matched_sections: list[dict[str, Any]] = []
    matched_doc_ids: list[str] = []
    seen_docs: set[str] = set()
    for section in sorted(sections, key=lambda s: (s.document_id, s.sort_order)):
        values = {"path": section.section_path, "summary": section.summary or ""}
        if not _node_matches(values, compiled):
            continue
        matched_sections.append(
            {
                "document_id": section.document_id,
                "section_id": section.section_id,
                "section_path": section.section_path,
                "summary": section.summary or "",
            }
        )
        if section.document_id not in seen_docs:
            seen_docs.add(section.document_id)
            matched_doc_ids.append(section.document_id)

    header = f"hits={len(matched_sections)}"
    lines = [header]
    for entry in matched_sections:
        block = [entry["section_path"]]
        if entry["summary"]:
            block.append(f"    summary: {entry['summary']}")
        lines.append("\n".join(block))

    return ToolResult(
        text="\n".join(lines),
        payload={
            "cardinality": len(matched_sections),
            "matched_sections": matched_sections,
            "matched_document_ids": matched_doc_ids,
        },
        refs=[
            {"document_id": entry["document_id"], "section_path": entry["section_path"]}
            for entry in matched_sections
        ],
    )
