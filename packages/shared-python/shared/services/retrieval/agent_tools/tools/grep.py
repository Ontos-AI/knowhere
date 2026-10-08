"""``corpus.grep`` — exact string lookup against published term text.

SQL ``LIKE`` on ``lower(coalesce(term_search_text, ''))``, scoped to the
current revision. That expression is the existing term trigram index.
The field is already published (body + filename + section path; tables
use summary/keywords/caption; images use their description).

Several terms are matched in one query (OR). Hits are then split back
into one block per term, one row per matching section (first chunk in
document / sort order). Terms with fewer matching sections are listed
first so a rare term is not buried by a generic one. Visible rows stop
at the existing ``limit``; leftover sections become one fold line per
document with a reusable ``scope``.

Snippets are built by the shared ``agent_tools.snippet.build_snippet`` (head
+ first-match window + tail, ``...``-joined, overlap-merged). Rows render
through the shared ``agent_tools.snippet.format_row`` — the same row shape
``corpus.outline``/``corpus.node_filter``/``corpus.recall``/``corpus.assets``
use, so a model reads one shape regardless of which tool produced it.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext

from shared.services.retrieval.corpus_storage import CorpusStorage

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import func, or_, select

from shared.services.retrieval.agent_tools.asset_hosts import host_paths_for_hits
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


def _indexed_term_search_text(namespace: str = "default") -> Any:
    """Haystack expression for ``idx_document_chunks_term_trgm``."""
    return func.lower(func.coalesce(CorpusStorage.resolve_namespace(namespace).DocumentChunk.term_search_text, ""))


def _term_search(terms: list[str], namespace: str = "default") -> tuple[re.Pattern[str], Any]:
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
            _indexed_term_search_text(namespace).like(f"%{term.lower()}%")
            for term in terms
        )
    )


def _term_span(text: str, term: str) -> tuple[int, int] | None:
    match = re.search(re.escape(term), text, flags=re.IGNORECASE)
    return match.span() if match else None


def _sections_for_term(
    hits: list[dict[str, Any]], term: str, context_chars: int
) -> list[dict[str, Any]]:
    """Keep the first hit per ``(document_id, section_path)`` for this term.

    ``hits`` must already be in document / sort order.
    """
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for hit in hits:
        text = str(hit["term_search_text"] or "")
        span = _term_span(text, term)
        if span is None:
            continue
        key = (str(hit["document_id"]), str(hit["section_path"] or ""))
        if key in kept:
            continue
        kept[key] = {**hit, "snippet": build_snippet(text, span, hit_context=context_chars)}
        order.append(key)
    return [kept[key] for key in order]


def _fold_line(source_file_name: str, document_id: str, leftover: int) -> str:
    return (
        f"{source_file_name} 还有 {leftover} 节命中 — "
        f"scope=[{{document_id: {document_id}}}]"
    )


def _build_hit_row(
    hit: dict[str, Any],
    host_paths: dict[tuple[str, str], tuple[str, bool]],
) -> dict[str, Any]:
    chunk_type = str(hit["chunk_type"] or "").strip()
    is_asset = chunk_type in ASSET_CHUNK_TYPES
    key = (str(hit["document_id"]), str(hit["chunk_id"]))
    section_path, hosted = (
        host_paths[key] if is_asset and key in host_paths else (hit["section_path"], None)
    )
    return build_row(
        kind=chunk_type or "text",
        document_id=hit["document_id"],
        section_path=section_path,
        title=hit["source_file_name"],
        chunk_id=hit["chunk_id"] if is_asset else None,
        snippet=hit["snippet"],
        hosted=hosted if is_asset else None,
    )


@register_tool(
    name="corpus.grep",
    description=(
        "Exact string search over body text, image descriptions, table "
        "summaries and keywords, file names and section paths. Table cell "
        "values are not searched. Requires pattern or patterns — do not "
        "call without a term. Each term is a separate block of section "
        "rows (score-free — this is an exact match, not a ranked search; "
        "use corpus.recall for ranking). Terms with fewer matching "
        "sections are listed first. Rows past limit are folded per "
        "document with a reusable scope — copy that scope onto a later "
        "corpus.grep call to search only that document. Table and image "
        "hits are address + snippet rows; read the hit's document_id + "
        "section_path (or chunk_id for image/table) with corpus.read "
        "next. Provide 'pattern' for one term, or 'patterns' for several "
        "terms in this single call instead of several parallel "
        "corpus.grep calls. At least one of pattern/patterns is required."
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
                    "Several search terms in this one call. Each term is "
                    "listed as its own block. Terms with fewer matching "
                    "sections are shown first. This tool does not rank "
                    "by relevance."
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
                "description": (
                    "Max section rows to show. Further matching sections "
                    "are folded per document with a reusable scope."
                ),
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
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(ctx.namespace)
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
            document_id_col=corpusStorage.DocumentChunk.document_id,
            section_path_col=corpusStorage.DocumentSection.section_path,
        )

    _compiled, term_filter = _term_search(terms, ctx.namespace)
    # Match the indexed haystack first so PostgreSQL can use
    # idx_document_chunks_term_trgm. Section is joined here (not later) so a
    # scope's section-subtree condition can apply inside this same CTE.
    matched = (
        select(
            corpusStorage.DocumentChunk.id,
            corpusStorage.DocumentChunk.chunk_id,
            corpusStorage.DocumentChunk.document_id,
            corpusStorage.DocumentChunk.job_result_id,
            corpusStorage.DocumentChunk.chunk_type,
            corpusStorage.DocumentChunk.term_search_text,
            corpusStorage.DocumentChunk.section_id,
            corpusStorage.DocumentChunk.sort_order,
            corpusStorage.DocumentSection.section_path,
        )
        .select_from(corpusStorage.DocumentChunk)
        .outerjoin(corpusStorage.DocumentSection, corpusStorage.DocumentSection.section_id == corpusStorage.DocumentChunk.section_id)
        .where(
            corpusStorage.DocumentChunk.term_search_text.is_not(None),
            term_filter,
            corpusStorage.DocumentChunk.user_id == corpusStorage.resolve_owner(ctx.user_id),
            corpusStorage.DocumentChunk.namespace == ctx.namespace,
            ctx.document_scope.predicate(corpusStorage.DocumentChunk.document_id),
        )
    )
    if scope_filter is not None:
        matched = matched.where(scope_filter)
    if chunk_types:
        matched = matched.where(
            func.lower(corpusStorage.DocumentChunk.chunk_type).in_(sorted(chunk_types))
        )
    matched = matched.cte("matched").prefix_with("MATERIALIZED")

    rows_stmt = (
        select(
            matched.c.chunk_id,
            matched.c.document_id,
            matched.c.chunk_type,
            matched.c.term_search_text,
            matched.c.sort_order,
            matched.c.section_path,
            corpusStorage.Document.source_file_name,
        )
        .select_from(matched)
        .join(corpusStorage.Document, corpusStorage.Document.document_id == matched.c.document_id)
        .where(
            corpusStorage.Document.user_id == corpusStorage.resolve_owner(ctx.user_id),
            corpusStorage.Document.namespace == ctx.namespace,
            corpusStorage.Document.status == "active",
            CorpusRevisionContext.build_revision_column(corpusStorage.Document) == matched.c.job_result_id,
        )
        .order_by(matched.c.document_id, matched.c.sort_order, matched.c.chunk_id)
    )
    matched_rows = (await ctx.db.execute(rows_stmt)).all()

    hits: list[dict[str, Any]] = []
    for (
        chunk_id,
        document_id,
        chunk_type,
        term_search_text,
        _sort_order,
        section_path,
        source_file_name,
    ) in matched_rows:
        hits.append(
            {
                "document_id": document_id,
                "source_file_name": source_file_name,
                "chunk_id": chunk_id,
                "chunk_type": chunk_type,
                "section_path": section_path,
                "term_search_text": term_search_text,
            }
        )

    blocks: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for index, term in enumerate(terms):
        sections = _sections_for_term(hits, term, context_chars)
        if sections:
            blocks.append((len(sections), index, term, sections))
    blocks.sort()

    shown_hits: list[dict[str, Any]] = []
    remaining = limit
    allocated: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    for _count, _index, term, sections in blocks:
        visible = sections[:remaining]
        leftover = sections[remaining:]
        remaining -= len(visible)
        shown_hits.extend(visible)
        allocated.append((term, visible, leftover))

    host_paths = await host_paths_for_hits(ctx, shown_hits, scope)

    lines: list[str] = []
    if requested_limit > limit:
        lines.append(f"note: capped to budget.max_items={ctx.budget.max_items}")
    rows: list[dict[str, Any]] = []
    for term, visible, leftover in allocated:
        lines.append(term)
        for hit in visible:
            row = _build_hit_row(hit, host_paths)
            rows.append(row)
            lines.append(format_row(row))
        leftover_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hit in leftover:
            leftover_by_doc[str(hit["document_id"])].append(hit)
        for document_id in leftover_by_doc:
            group = leftover_by_doc[document_id]
            lines.append(
                _fold_line(str(group[0]["source_file_name"] or ""), document_id, len(group))
            )

    return ToolResult(
        text="\n".join(lines),
        payload={"rows": rows, "details": {}},
        refs=[
            {"document_id": hit["document_id"], "chunk_id": hit["chunk_id"]}
            for hit in shown_hits
        ],
    )
