"""``corpus.node_filter`` — deterministic FOR-ALL/EXISTS/ANY/NOT predicate over sections.

Reuses the exact predicate compile/match semantics from
``scoring.node_filter_predicates`` (path/summary substring|regex, fields AND
together, terms OR together) — see that module's docstring — but walks
``document_sections`` rows for the requested scope's current revision
instead of the in-memory map-nav tree.

Unlike a plain filter, the rendered result is not just the matched rows: it
is each matched section's full ancestor chain (context) plus its entire
descendant subtree, in the same map-row shape ``corpus.outline`` uses (see
``agent_tools.snippet``) — this is a map-narrowing tool, the same family as
``outline``, not a search hit list. Matched rows are marked ``[Hit]``. Past
``registry.MAP_TOOL_CHAR_BUDGET`` chars the map shrinks in
``agent_tools.map_render``: summaries are dropped first, then low-scoring
context is folded with every hit kept: a hit and its ancestor chain are
never folded away, but low-relevance branches under a hit can be hidden
behind a placeholder.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.map_render import render_map
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    chunk_types_schema,
    register_tool,
)
from shared.services.retrieval.agent_tools.scope import (
    SCOPE_SCHEMA,
    resolve_scope,
    scope_document_ids,
)
from shared.services.retrieval.agent_tools.snippet import build_row
from shared.services.retrieval.scoring.node_filter_predicates import (
    FieldPredicate,
    _compile_predicates,
    _node_matches,
    field_predicate,
)


@register_tool(
    name="corpus.node_filter",
    description=(
        "Deterministic FOR-ALL/EXISTS/ANY/NOT filter over section titles and "
        "summaries — not body text (use corpus.grep for that). Narrows a map "
        "the same way corpus.outline does: the result is each matched "
        "section's full ancestor chain plus its entire descendant subtree "
        "(marked [Hit] where matched), not a bare list of hits. Call with "
        "scope plus a predicates array; each predicate is {field: "
        "'path'|'summary', terms: [...], match: 'substring'|'regex'}. "
        "field=path matches the section path; field=summary matches the "
        "section summary. Predicates AND together across the array; terms "
        "within one predicate's 'terms' list OR together. Use the returned "
        "section_paths to corpus.read the ones that matter, or as the scope "
        "of a corpus.grep/corpus.recall/corpus.assets call. A result over "
        "20,000 chars is shrunk, not cut off: summaries are omitted first; "
        "if it is still too long, every [Hit] row and its parents stay and "
        "low-relevance context subtrees are hidden behind a 'hidden N "
        "nodes' placeholder. If the hits alone do not fit, the call fails: "
        "the scope is too large to map, so use corpus.grep, corpus.recall or "
        "corpus.assets on the same scope."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "scope": SCOPE_SCHEMA,
            "predicates": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "AND across this array. Each item is "
                    "{field, terms, match}: terms OR together."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": ["path", "summary"],
                            "description": "Match the section path or the section summary.",
                        },
                        "terms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Candidate strings; any one may match.",
                        },
                        "match": {
                            "type": "string",
                            "enum": ["substring", "regex"],
                            "default": "substring",
                            "description": "How each term is matched.",
                        },
                    },
                    "required": ["field", "terms"],
                    "additionalProperties": False,
                },
            },
            "chunk_types": chunk_types_schema(
                "Narrow matched sections to those owning a chunk of one of "
                "these types (e.g. ['page'] to filter to page-track leaves "
                "only). Omit for no narrowing."
            ),
        },
        "required": ["scope", "predicates"],
        "additionalProperties": False,
    },
)
async def node_filter(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    targets, scope_error = await resolve_scope(
        ctx.db,
        user_id=ctx.user_id,
        namespace=ctx.namespace,
        document_scope=ctx.document_scope,
        raw_scope=args.get("scope"),
    )
    if scope_error is not None:
        return ToolResult(text="", error=f"node_filter: {scope_error}")

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
            payload={
                "rows": [],
                "details": {"cardinality": 0, "failed_predicates": failed},
            },
            error="one or more predicates failed to compile",
        )

    document_ids = scope_document_ids(targets)
    revision_by_doc = {target.document_id: target.job_result_id for target in targets}

    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    }
    allowed_section_ids: set[str] | None = None
    if chunk_types:
        chunk_rows = await ctx.db.execute(
            select(DocumentChunk.section_id, DocumentChunk.chunk_type).where(
                DocumentChunk.document_id.in_(document_ids),
                DocumentChunk.job_result_id.in_(list(revision_by_doc.values())),
            )
        )
        allowed_section_ids = {
            str(section_id)
            for section_id, chunk_type in chunk_rows.all()
            if section_id and str(chunk_type or "").strip().lower() in chunk_types
        }

    rows_by_id: dict[str, dict[str, Any]] = {}
    order: list[tuple[str, str | None]] = []
    matched_ids: set[str] = set()
    matched_doc_ids: list[str] = []

    for target in targets:
        section_stmt = (
            select(DocumentSection)
            .where(DocumentSection.document_id == target.document_id)
            .where(DocumentSection.job_result_id == target.job_result_id)
            .order_by(DocumentSection.sort_order, DocumentSection.section_id)
        )
        doc_sections = list((await ctx.db.execute(section_stmt)).scalars().all())
        if target.section_path is not None:
            doc_sections = [
                s
                for s in doc_sections
                if s.section_path == target.section_path
                or s.section_path.startswith(f"{target.section_path} / ")
            ]
        by_id = {s.section_id: s for s in doc_sections}

        hit_ids: set[str] = set()
        for section in doc_sections:
            if allowed_section_ids is not None and section.section_id not in allowed_section_ids:
                continue
            values = {"path": section.section_path, "summary": section.summary or ""}
            if _node_matches(values, compiled):
                hit_ids.add(section.section_id)

        if not hit_ids:
            continue
        matched_ids.update(hit_ids)
        matched_doc_ids.append(target.document_id)

        visible_ids: set[str] = set()
        for hit_id in hit_ids:
            node = by_id.get(hit_id)
            while node is not None:
                visible_ids.add(node.section_id)
                node = by_id.get(node.parent_section_id) if node.parent_section_id else None
            hit_path = by_id[hit_id].section_path
            for section in doc_sections:
                if section.section_path == hit_path or section.section_path.startswith(
                    f"{hit_path} / "
                ):
                    visible_ids.add(section.section_id)

        base_level = min(s.section_level for s in doc_sections)

        for section in doc_sections:
            if section.section_id not in visible_ids:
                continue
            rows_by_id[section.section_id] = build_row(
                kind="section",
                document_id=target.document_id,
                section_path=section.section_path,
                title=section.section_title,
                summary=section.summary or "",
                depth=section.section_level - base_level,
                is_hit=section.section_id in hit_ids,
            )
            parent_id = (
                section.parent_section_id
                if section.parent_section_id in visible_ids
                else None
            )
            order.append((section.section_id, parent_id))

    if not matched_ids:
        return ToolResult(
            text="hits=0",
            payload={"rows": [], "details": {"cardinality": 0, "matched_document_ids": []}},
            refs=[],
        )

    def header(visible: int, hidden: int) -> str:
        line = f"hits={len(matched_ids)}"
        if hidden:
            line += f" (folded — {hidden} context nodes hidden; every hit is shown)"
        return line

    rendered = await render_map(
        ctx,
        tool="node_filter",
        order=order,
        rows_by_id=rows_by_id,
        revision_by_document=revision_by_doc,
        header=header,
        keep_ids=matched_ids,
    )
    if rendered.error is not None:
        return ToolResult(text="", error=rendered.error)

    visible_rows = [
        rows_by_id[section_id] for section_id, _ in order if section_id in rendered.visible_ids
    ]
    return ToolResult(
        text=rendered.text,
        payload={
            "rows": visible_rows,
            "details": {
                "cardinality": len(matched_ids),
                "matched_document_ids": matched_doc_ids,
            },
        },
        refs=[
            {"document_id": row["document_id"], "section_path": row["section_path"]}
            for row in visible_rows
            if row["is_hit"]
        ],
    )
