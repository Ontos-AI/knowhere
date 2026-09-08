"""``corpus.grep`` — exact string/regex lookup against body text.

SQL ``ILIKE`` / ``~*`` on ``document_chunks.content``, scoped to the current
revision. Reports a total match count (over the full in-scope corpus, not
just the returned page) alongside capped snippets, so ANY/ALL logic can close
over body text the same way ``corpus.node_filter`` closes over titles/summaries.

Snippets are built by the shared ``agent_tools.snippet.build_snippet`` (head
+ first-match window + tail, ``...``-joined, overlap-merged) — the same
mechanism ``corpus.recall``'s term channel uses, so the two tools don't carry
duplicate window-slicing logic or drift to different constants.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    capped_limit,
    register_tool,
)
from shared.services.retrieval.agent_tools.snippet import (
    HIT_CONTEXT_CHARS,
    build_snippet,
)

_DEFAULT_MAX_RESULTS = 30
_DEFAULT_CONTEXT_CHARS = HIT_CONTEXT_CHARS


def _build_scope_filters(
    *,
    user_id: str,
    namespace: str,
    document_ids: list[str],
    chunk_types: set[str],
) -> list[Any]:
    filters: list[Any] = [
        Document.user_id == user_id,
        Document.namespace == namespace,
        Document.status == "active",
        Document.current_job_result_id == DocumentChunk.job_result_id,
    ]
    if document_ids:
        filters.append(Document.document_id.in_(document_ids))
    if chunk_types:
        filters.append(func.lower(DocumentChunk.chunk_type).in_(sorted(chunk_types)))
    return filters


@register_tool(
    name="corpus.grep",
    description=(
        "Exact string or regex search against chunk body text (content), "
        "not titles/summaries (use corpus.node_filter for that). Returns the "
        "total number of matching chunks plus a capped list of snippets."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "document_ids": {"type": "array", "items": {"type": "string"}},
            "chunk_types": {"type": "array", "items": {"type": "string"}},
            "is_regex": {"type": "boolean", "default": False},
            "context_chars": {"type": "integer", "default": _DEFAULT_CONTEXT_CHARS},
            "max_results": {"type": "integer", "default": _DEFAULT_MAX_RESULTS},
        },
        "required": ["pattern"],
    },
)
async def grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return ToolResult(text="", error="grep requires pattern")
    is_regex = bool(args.get("is_regex", False))
    context_chars = int(args.get("context_chars") or _DEFAULT_CONTEXT_CHARS)
    requested_max_results = int(args.get("max_results") or _DEFAULT_MAX_RESULTS)
    max_results = capped_limit(requested_max_results, ctx.budget)
    document_ids = [
        str(d).strip() for d in (args.get("document_ids") or []) if str(d).strip()
    ]
    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    }

    if is_regex:
        try:
            compiled = re.compile(pattern, flags=re.IGNORECASE)
        except re.error as exc:
            return ToolResult(text="", error=f"invalid regex: {exc}")
    else:
        compiled = re.compile(re.escape(pattern), flags=re.IGNORECASE)

    filters = _build_scope_filters(
        user_id=ctx.user_id,
        namespace=ctx.namespace,
        document_ids=document_ids,
        chunk_types=chunk_types,
    )
    content_filter = (
        DocumentChunk.content.op("~*")(pattern)
        if is_regex
        else DocumentChunk.content.ilike(f"%{pattern}%")
    )

    count_stmt = (
        select(func.count(DocumentChunk.id))
        .select_from(DocumentChunk)
        .join(Document, Document.document_id == DocumentChunk.document_id)
        .where(*filters, content_filter)
    )
    total_matches = int((await ctx.db.execute(count_stmt)).scalar_one())

    rows_stmt = (
        select(
            DocumentChunk.chunk_id,
            DocumentChunk.document_id,
            DocumentChunk.chunk_type,
            DocumentChunk.content,
            DocumentSection.section_path,
            Document.source_file_name,
        )
        .select_from(DocumentChunk)
        .join(Document, Document.document_id == DocumentChunk.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(*filters, content_filter)
        .order_by(DocumentChunk.document_id, DocumentChunk.sort_order)
        .limit(max_results)
    )
    rows = (await ctx.db.execute(rows_stmt)).all()

    results: list[dict[str, Any]] = []
    for chunk_id, document_id, chunk_type, content, section_path, source_file_name in rows:
        text = str(content or "")
        match = compiled.search(text)
        snippet = build_snippet(
            text, match.span() if match else None, hit_context=context_chars
        )
        results.append(
            {
                "document_id": document_id,
                "source_file_name": source_file_name,
                "chunk_id": chunk_id,
                "chunk_type": chunk_type,
                "section_path": section_path,
                "snippet": snippet,
            }
        )

    lines = [f"total_matches={total_matches} returned={len(results)}"]
    if requested_max_results > max_results:
        lines.append(f"note: capped to budget.max_items={ctx.budget.max_items}")
    for r in results:
        lines.append(f"- {r['source_file_name']} / {r['section_path']}: {r['snippet']!r}")

    return ToolResult(
        text="\n".join(lines),
        payload={"total_matches": total_matches, "results": results},
        refs=[
            {"document_id": r["document_id"], "chunk_id": r["chunk_id"]} for r in results
        ],
    )
