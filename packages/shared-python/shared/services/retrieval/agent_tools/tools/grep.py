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
+ first-match window + tail, ``...``-joined, overlap-merged). Rows render
through the shared ``agent_tools.snippet.format_row`` — the same row shape
``corpus.outline``/``corpus.node_filter``/``corpus.recall``/``corpus.assets``
use, so a model reads one shape regardless of which tool produced it.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_, select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_tools.asset_hosts import host_paths_for_hits
from shared.services.retrieval.agent_tools.explore_mount import mount_explore_hits
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    capped_limit,
    chunk_types_schema,
    register_tool,
)
from shared.services.retrieval.agent_tools.scope import (
    SCOPE_SCHEMA,
    ScopeTarget,
    resolve_scope,
    scope_orm_clause,
)
from shared.services.retrieval.agent_tools.snippet import build_row, build_snippet, format_row
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES

_DEFAULT_LIMIT = 30
_DEFAULT_CONTEXT_CHARS = 80


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
        "Exact string search over body text, image descriptions, table "
        "summaries and keywords, file names and section paths. Table cell "
        "values are not searched. Requires pattern or patterns — do not "
        "call without a term. Returns the total number of matching "
        "chunks plus a capped list of snippet rows (score-free — this is an "
        "exact match, not a ranked search; use corpus.recall for ranking). "
        "Table and image hits show their content; body hits also show the "
        "images and tables they contain — read the hit's document_id + "
        "section_path with corpus.read next. Provide 'pattern' for one "
        "term, or 'patterns' for several candidate terms OR'd together in "
        "this single call (e.g. synonyms) — issue one call with multiple "
        "terms instead of several parallel corpus.grep calls for different "
        "terms in the same turn. Several terms are any-match (OR), and "
        "rows stay in document order — this tool does not rank by "
        "relevance. At least one of pattern/patterns is required."
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
                "description": (
                    "Several search terms OR'd together in this one call. "
                    "A chunk matches if any term hits. Results stay in "
                    "document order, not relevance order."
                ),
            },
            "scope": SCOPE_SCHEMA,
            "chunk_types": chunk_types_schema(
                "Restrict hits to these chunk types. Old type names are rejected."
            ),
            "context_chars": {
                "type": "integer",
                "minimum": 1,
                "default": _DEFAULT_CONTEXT_CHARS,
                "description": "Characters of context around the first match in the snippet.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "default": _DEFAULT_LIMIT,
                "description": "Max rows to return. The total match count is still reported.",
            },
        },
        "anyOf": [
            {"required": ["pattern"]},
            {"required": ["patterns"]},
        ],
        "additionalProperties": False,
    },
)
async def grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    terms = _terms_from_args(args)
    if not terms:
        return ToolResult(text="", error="grep requires pattern or patterns")
    context_chars = int(args.get("context_chars", _DEFAULT_CONTEXT_CHARS))
    requested_limit = int(args.get("limit", _DEFAULT_LIMIT))
    limit = capped_limit(requested_limit, ctx.budget)
    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    }

    scope: list[ScopeTarget] = []
    scope_filter = None
    if args.get("scope") is not None:
        scope, scope_error = await resolve_scope(
            ctx.db,
            user_id=ctx.user_id,
            namespace=ctx.namespace,
            document_scope=ctx.document_scope,
            raw_scope=args.get("scope"),
        )
        if scope_error is not None:
            return ToolResult(text="", error=f"grep: {scope_error}")
        scope_filter = scope_orm_clause(
            scope,
            document_id_col=DocumentChunk.document_id,
            section_path_col=DocumentSection.section_path,
        )

    compiled, term_filter = _term_search(terms)
    # Match the indexed haystack first so PostgreSQL can use
    # idx_document_chunks_term_trgm. Section is joined here (not later) so a
    # scope's section-subtree condition can apply inside this same CTE.
    matched = (
        select(
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
            DocumentSection.section_path,
        )
        .select_from(DocumentChunk)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(
            DocumentChunk.term_search_text.is_not(None),
            term_filter,
            DocumentChunk.user_id == ctx.user_id,
            DocumentChunk.namespace == ctx.namespace,
            ctx.document_scope.predicate(DocumentChunk.document_id),
        )
    )
    if scope_filter is not None:
        matched = matched.where(scope_filter)
    if chunk_types:
        matched = matched.where(
            func.lower(DocumentChunk.chunk_type).in_(sorted(chunk_types))
        )
    matched = matched.cte("matched").prefix_with("MATERIALIZED")

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
            matched.c.section_path,
            Document.source_file_name,
            func.count().over().label("total_matches"),
        )
        .select_from(matched)
        .join(Document, Document.document_id == matched.c.document_id)
        .outerjoin(JobResult, JobResult.id == Document.current_job_result_id)
        .where(
            Document.user_id == ctx.user_id,
            Document.namespace == ctx.namespace,
            Document.status == "active",
            Document.current_job_result_id == matched.c.job_result_id,
        )
        .order_by(matched.c.document_id, matched.c.sort_order)
        .limit(limit)
    )
    matched_rows = (await ctx.db.execute(rows_stmt)).all()
    total_matches = int(matched_rows[0][-1]) if matched_rows else 0

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
    ) in matched_rows:
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
    host_paths = await host_paths_for_hits(ctx, results, scope)

    rows: list[dict[str, Any]] = []
    lines = [f"total_matches={total_matches} returned={len(results)}"]
    if requested_limit > limit:
        lines.append(f"note: capped to budget.max_items={ctx.budget.max_items}")
    for r in results:
        chunk_type = str(r["chunk_type"] or "").strip()
        is_asset = chunk_type in ASSET_CHUNK_TYPES
        key = (str(r["document_id"]), str(r["chunk_id"]))
        section_path, hosted = (
            host_paths[key] if is_asset and key in host_paths else (r["section_path"], None)
        )
        row = build_row(
            kind=chunk_type or "text",
            document_id=r["document_id"],
            section_path=section_path,
            title=r["source_file_name"],
            chunk_id=r["chunk_id"] if is_asset else None,
            snippet=r["snippet"],
            hosted=hosted if is_asset else None,
        )
        row["mounted_chunk_ids"] = r["mounted_chunk_ids"]
        rows.append(row)
        lines.append(format_row(row))
        rendered = str(r.get("rendered") or "").strip()
        if rendered:
            lines.append(rendered)

    return ToolResult(
        text="\n".join(lines),
        payload={
            "rows": rows,
            "details": {
                "total_matches": total_matches,
                "capped": requested_limit > limit,
            },
        },
        refs=[
            {"document_id": r["document_id"], "chunk_id": r["chunk_id"]} for r in results
        ],
        media=media,
    )
