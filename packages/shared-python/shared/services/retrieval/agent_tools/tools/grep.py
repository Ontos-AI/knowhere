"""``corpus.grep`` — exact string lookup against published term text.

SQL ``LIKE`` on ``lower(coalesce(term_search_text, ''))``, scoped to the
current revision. That expression is the existing term trigram index.
The field is already published (body + filename + section path; tables
use summary/keywords/caption; images use their description).
Reports a total match count (over the full in-scope corpus, not just the
returned page) alongside capped snippets.

Matching table/image chunks, and body chunks that list ``connect_to``
targets, are mounted through the shared explore mount so the tool result
includes rendered table/image content.

Snippets are built by the shared ``agent_tools.snippet.build_snippet`` (head
+ first-match window + tail, ``...``-joined, overlap-merged) — the same
mechanism ``corpus.recall``'s term channel uses, so the two tools don't carry
duplicate window-slicing logic or drift to different constants.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_, select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_tools.explore_mount import mount_explore_hits
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    capped_limit,
    register_tool,
)
from shared.services.retrieval.agent_tools.snippet import (
    HIT_CONTEXT_CHARS,
    build_snippet,
    format_search_hit_line,
)
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES

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


def _indexed_term_search_text() -> Any:
    """Haystack expression for ``idx_document_chunks_term_trgm``."""
    return func.lower(func.coalesce(DocumentChunk.term_search_text, ""))


def _term_search(terms: list[str]) -> tuple[re.Pattern[str], Any]:
    """Build the Python matcher and the SQL term predicate.

    Every term uses ``LIKE`` on the indexed lowercased haystack. Several
    terms become ``OR`` of those same clauses.
    """
    compiled = re.compile(
        "|".join(re.escape(term) for term in terms),
        flags=re.IGNORECASE,
    )
    return compiled, or_(
        *(
            _indexed_term_search_text().like(f"%{term.lower()}%")
            for term in terms
        )
    )


@register_tool(
    name="corpus.grep",
    description=(
        "Exact string search against published term_search_text "
        "(body text, image descriptions, table summaries/keywords, plus "
        "filename and section path). Returns the total number of matching "
        "chunks plus a capped list of snippets. Table and image hits include "
        "rendered content; body hits include any connected table or image. "
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
                "description": "One search term.",
            },
            "patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Several search terms OR'd together in this one call.",
            },
            "document_ids": {"type": "array", "items": {"type": "string"}},
            "chunk_types": {"type": "array", "items": {"type": "string"}},
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
    context_chars = int(args.get("context_chars") or _DEFAULT_CONTEXT_CHARS)
    requested_max_results = int(args.get("max_results") or _DEFAULT_MAX_RESULTS)
    max_results = capped_limit(requested_max_results, ctx.budget)
    document_ids = [
        str(d).strip() for d in (args.get("document_ids") or []) if str(d).strip()
    ]
    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    }

    compiled, term_filter = _term_search(terms)
    # Match the indexed haystack first so PostgreSQL can use
    # idx_document_chunks_term_trgm. Joining documents first makes the
    # planner filter every in-scope chunk and ignore that index.
    matched = select(
        DocumentChunk.id,
        DocumentChunk.chunk_id,
        DocumentChunk.document_id,
        DocumentChunk.job_result_id,
        DocumentChunk.chunk_type,
        DocumentChunk.term_search_text,
        DocumentChunk.content,
        DocumentChunk.file_path,
        DocumentChunk.chunk_metadata,
        DocumentChunk.section_id,
        DocumentChunk.sort_order,
    ).where(
        DocumentChunk.term_search_text.is_not(None),
        term_filter,
        DocumentChunk.user_id == ctx.user_id,
        DocumentChunk.namespace == ctx.namespace,
        ctx.document_scope.predicate(DocumentChunk.document_id),
    )
    if document_ids:
        matched = matched.where(DocumentChunk.document_id.in_(document_ids))
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

    rows_stmt = (
        select(
            matched.c.chunk_id,
            matched.c.document_id,
            matched.c.chunk_type,
            matched.c.term_search_text,
            matched.c.content,
            matched.c.file_path,
            matched.c.chunk_metadata,
            matched.c.job_result_id,
            JobResult.job_id,
            DocumentSection.section_path,
            Document.source_file_name,
            func.count().over().label("total_matches"),
        )
        .select_from(matched)
        .join(Document, Document.document_id == matched.c.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == matched.c.section_id)
        .outerjoin(JobResult, JobResult.id == Document.current_job_result_id)
        .where(*scope_filters)
        .order_by(matched.c.document_id, matched.c.sort_order)
        .limit(max_results)
    )
    rows = (await ctx.db.execute(rows_stmt)).all()
    total_matches = int(rows[0][-1]) if rows else 0

    results: list[dict[str, Any]] = []
    for (
        chunk_id,
        document_id,
        chunk_type,
        term_search_text,
        content,
        file_path,
        chunk_metadata,
        job_result_id,
        job_id,
        section_path,
        source_file_name,
        _total_matches,
    ) in rows:
        text = str(term_search_text or "")
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
                "content": content,
                "file_path": file_path,
                "chunk_metadata": chunk_metadata or {},
                "job_result_id": job_result_id,
                "job_id": job_id,
            }
        )

    media: list[dict[str, str]] = []
    if results:
        results, media = await mount_explore_hits(
            ctx, results, char_budget=ctx.budget.max_chars
        )

    lines = [f"total_matches={total_matches} returned={len(results)}"]
    if requested_max_results > max_results:
        lines.append(f"note: capped to budget.max_items={ctx.budget.max_items}")
    for r in results:
        chunk_type = str(r["chunk_type"] or "").strip()
        lines.append(
            format_search_hit_line(
                source_file_name=r["source_file_name"],
                document_id=r["document_id"],
                section_path=r["section_path"],
                snippet=r["snippet"],
                chunk_type=chunk_type,
                chunk_id=r["chunk_id"] if chunk_type in ASSET_CHUNK_TYPES else None,
            )
        )
        rendered = str(r.get("rendered") or "").strip()
        if rendered:
            lines.append(rendered)

    return ToolResult(
        text="\n".join(lines),
        payload={"total_matches": total_matches, "results": results},
        refs=[
            {"document_id": r["document_id"], "chunk_id": r["chunk_id"]} for r in results
        ],
        media=media,
    )
