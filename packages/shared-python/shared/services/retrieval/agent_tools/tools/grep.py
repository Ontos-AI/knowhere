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

import asyncio
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


def _terms_from_args(args: dict[str, Any]) -> list[str]:
    """Merge ``pattern`` (single) and ``patterns`` (list) into one ordered,
    de-duplicated term list — see ``register_tool`` description above for
    why both exist (collapsing what used to be several parallel
    ``corpus.grep`` calls into one multi-term call)."""
    single = str(args.get("pattern") or "").strip()
    many = [str(p).strip() for p in (args.get("patterns") or []) if str(p).strip()]
    seen: set[str] = set()
    terms: list[str] = []
    for term in ([single] if single else []) + many:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _content_search(
    terms: list[str], *, is_regex: bool
) -> tuple[re.Pattern[str], Any]:
    """Build the Python matcher and the SQL content predicate for ``terms``.

    One literal term stays on ``ILIKE`` (trgm-index path). Several terms, or
    any regex call, become a ``~*`` alternation — ``ILIKE`` has no OR form.
    """
    if is_regex:
        combined = "|".join(f"(?:{term})" for term in terms)
        compiled = re.compile(combined, flags=re.IGNORECASE)
        return compiled, DocumentChunk.content.op("~*")(combined)
    if len(terms) == 1:
        compiled = re.compile(re.escape(terms[0]), flags=re.IGNORECASE)
        return compiled, DocumentChunk.content.ilike(f"%{terms[0]}%")
    combined = "|".join(re.escape(term) for term in terms)
    compiled = re.compile(combined, flags=re.IGNORECASE)
    return compiled, DocumentChunk.content.op("~*")(combined)


@register_tool(
    name="corpus.grep",
    description=(
        "Exact string or regex search against chunk body text (content), "
        "not titles/summaries (use corpus.node_filter for that). Returns the "
        "total number of matching chunks plus a capped list of snippets. "
        "Provide 'pattern' for one term, or 'patterns' for several candidate "
        "terms OR'd together in this single call (e.g. synonyms) — issue one "
        "call with multiple terms instead of several parallel corpus.grep "
        "calls for different terms in the same turn. At least one of "
        "pattern/patterns is required."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "One search term (string or regex per is_regex).",
            },
            "patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Several search terms OR'd together in this one call.",
            },
            "document_ids": {"type": "array", "items": {"type": "string"}},
            "chunk_types": {"type": "array", "items": {"type": "string"}},
            "is_regex": {"type": "boolean", "default": False},
            "context_chars": {"type": "integer", "default": _DEFAULT_CONTEXT_CHARS},
            "max_results": {"type": "integer", "default": _DEFAULT_MAX_RESULTS},
        },
        "anyOf": [
            {"required": ["pattern"]},
            {"required": ["patterns"]},
        ],
    },
)
async def grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    terms = _terms_from_args(args)
    if not terms:
        return ToolResult(text="", error="grep requires pattern or patterns")
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

    try:
        compiled, content_filter = _content_search(terms, is_regex=is_regex)
    except re.error as exc:
        return ToolResult(text="", error=f"invalid regex: {exc}")
    # Match content first so PostgreSQL can use idx_document_chunks_content_trgm.
    # Joining documents first makes the planner filter every in-scope chunk
    # and ignore the trigram index (observed: 31s vs 0.4s for the same count).
    matched = select(
        DocumentChunk.id,
        DocumentChunk.chunk_id,
        DocumentChunk.document_id,
        DocumentChunk.job_result_id,
        DocumentChunk.chunk_type,
        DocumentChunk.content,
        DocumentChunk.section_id,
        DocumentChunk.sort_order,
    ).where(DocumentChunk.content.is_not(None), content_filter)
    if chunk_types:
        matched = matched.where(
            func.lower(DocumentChunk.chunk_type).in_(sorted(chunk_types))
        )
    matched = matched.cte("matched").prefix_with("MATERIALIZED")

    scope_filters = [
        Document.user_id == ctx.user_id,
        Document.namespace == ctx.namespace,
        Document.status == "active",
        Document.current_job_result_id == matched.c.job_result_id,
        ctx.document_scope.predicate(Document.document_id),
    ]
    if document_ids:
        scope_filters.append(Document.document_id.in_(document_ids))

    count_stmt = (
        select(func.count(matched.c.id))
        .select_from(matched)
        .join(Document, Document.document_id == matched.c.document_id)
        .where(*scope_filters)
    )
    rows_stmt = (
        select(
            matched.c.chunk_id,
            matched.c.document_id,
            matched.c.chunk_type,
            matched.c.content,
            DocumentSection.section_path,
            Document.source_file_name,
        )
        .select_from(matched)
        .join(Document, Document.document_id == matched.c.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == matched.c.section_id)
        .where(*scope_filters)
        .order_by(matched.c.document_id, matched.c.sort_order)
        .limit(max_results)
    )
    async with ctx.db_factory() as rows_db:
        count_result, rows_result = await asyncio.gather(
            ctx.db.execute(count_stmt),
            rows_db.execute(rows_stmt),
        )
    total_matches = int(count_result.scalar_one())
    rows = rows_result.all()

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
        lines.append(
            f"- {r['source_file_name']} ({r['document_id']}) / {r['section_path']}: "
            f"{r['snippet']!r}"
        )

    return ToolResult(
        text="\n".join(lines),
        payload={"total_matches": total_matches, "results": results},
        refs=[
            {"document_id": r["document_id"], "chunk_id": r["chunk_id"]} for r in results
        ],
    )
