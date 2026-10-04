"""``corpus.outline`` — titles + summaries, no body text.

Reads ``document_sections`` for one or more scope targets' current
revision, each optionally narrowed to a section subtree. This queries the live tables directly rather than the
compressed ``RetrievalNamespaceMapSnapshot``/serving-manifest blob: that
snapshot is namespace-wide and decoding it to read one document's subtree
would cost more than this document-scoped, index-backed query.

A scope target's own row is always included, so calling this on a section
with no descendants (a leaf) would return exactly that one row with no
outline value — rejected up front instead, pointing at ``corpus.read``.

The rendered map (one row per section, same shape ``corpus.node_filter``
uses — see ``agent_tools.snippet``) is never truncated mid-text. Past
``registry.MAP_TOOL_CHAR_BUDGET`` chars it shrinks in
``agent_tools.map_render``: summaries are dropped first, then low-scoring
subtrees become placeholders, then the call fails as too large.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage

from typing import Any

from sqlalchemy import select

from shared.services.retrieval.agent_tools.map_render import render_map
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.agent_tools.scope import (
    SCOPE_SCHEMA,
    resolve_scope,
)
from shared.services.retrieval.agent_tools.snippet import build_row


@register_tool(
    name="corpus.outline",
    description=(
        "Map what a document (or one of its sections) covers: every "
        "section_path beneath a scope target, any depth, with title + "
        "summary, indented by level — no body text. Use this "
        "for 'what does this document/section cover' questions, not for "
        "finding an answer to a specific fact (use corpus.recall/corpus.grep "
        "for that). Accepts several scope targets in one call. Each target "
        "must be a non-leaf section (or the whole document, via a scope "
        "item with no section_path) — calling this on a section with no "
        "children is rejected; read it directly with corpus.read instead. "
        "Use the returned section_paths to corpus.read the ones that matter, "
        "or as the scope of a corpus.grep/corpus.recall/corpus.assets call. "
        "A result over 20,000 chars is shrunk, not cut off: summaries are "
        "omitted first; if it is still too long, the lowest-relevance "
        "subtrees are hidden behind a 'hidden N nodes' placeholder — "
        "re-scope to that section_path to expand it. If even that does not "
        "fit, the call fails: the scope is too large to map, so use "
        "corpus.grep, corpus.recall or corpus.assets on the same scope."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "scope": SCOPE_SCHEMA,
            "depth": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Max levels below each scope target to include. Omit "
                    "for unlimited depth."
                ),
            },
        },
        "required": ["scope"],
        "additionalProperties": False,
    },
)
async def outline(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(ctx.namespace)
    targets, scope_error = await resolve_scope(
        ctx.db,
        user_id=ctx.user_id,
        namespace=ctx.namespace,
        document_scope=ctx.document_scope,
        raw_scope=args.get("scope"),
    )
    if scope_error is not None:
        return ToolResult(text="", error=f"outline: {scope_error}")

    depth_raw = args.get("depth")
    depth = int(depth_raw) if depth_raw is not None else None

    document_ids = sorted({target.document_id for target in targets})
    documents = (
        (
            await ctx.db.execute(
                select(corpusStorage.Document).where(corpusStorage.Document.document_id.in_(document_ids))
            )
        )
        .scalars()
        .all()
    )
    source_file_name_by_doc = {d.document_id: d.source_file_name or "" for d in documents}

    rows_by_id: dict[str, dict[str, Any]] = {}
    order: list[tuple[str, str | None]] = []

    for target in targets:
        section_stmt = (
            select(corpusStorage.DocumentSection)
            .where(corpusStorage.DocumentSection.document_id == target.document_id)
            .where(corpusStorage.DocumentSection.job_result_id == target.job_result_id)
            .order_by(corpusStorage.DocumentSection.sort_order, corpusStorage.DocumentSection.section_id)
        )
        doc_sections = list((await ctx.db.execute(section_stmt)).scalars().all())

        if target.section_path is None:
            base_level = 0
            scoped = doc_sections
        else:
            root_section = next(
                (s for s in doc_sections if s.section_path == target.section_path), None
            )
            if root_section is None:
                return ToolResult(
                    text="",
                    error=(
                        f"outline: unknown section_path for {target.document_id}: "
                        f"{target.section_path}"
                    ),
                )
            has_children = any(
                s.section_path.startswith(f"{target.section_path} / ")
                for s in doc_sections
            )
            if not has_children:
                return ToolResult(
                    text="",
                    error=(
                        f"outline: {target.section_path} in {target.document_id} is a "
                        "leaf section (no child sections) — call corpus.read on it "
                        "directly instead of outline."
                    ),
                )
            base_level = root_section.section_level
            scoped = [
                s
                for s in doc_sections
                if s.section_path == target.section_path
                or s.section_path.startswith(f"{target.section_path} / ")
            ]

        if depth is not None:
            scoped = [s for s in scoped if (s.section_level - base_level) <= depth]

        scoped_ids = {s.section_id for s in scoped}

        for section in scoped:
            parent_id = (
                section.parent_section_id
                if section.parent_section_id in scoped_ids
                else None
            )
            rows_by_id[section.section_id] = build_row(
                kind="section",
                document_id=target.document_id,
                section_path=section.section_path,
                title=section.section_title,
                summary=section.summary or "",
                depth=section.section_level - base_level,
            )
            order.append((section.section_id, parent_id))

    if not order:
        return ToolResult(text="sections=0", payload={"rows": [], "details": {}}, refs=[])

    document_names = ", ".join(
        f"{source_file_name_by_doc.get(doc_id, doc_id)} ({doc_id})"
        for doc_id in document_ids
    )

    def header(visible: int, hidden: int) -> str:
        line = f"documents={document_names} sections={visible}"
        if hidden:
            line += f" (folded — {hidden} nodes hidden)"
        return line

    rendered = await render_map(
        ctx,
        tool="outline",
        order=order,
        rows_by_id=rows_by_id,
        revision_by_document={target.document_id: target.job_result_id for target in targets},
        header=header,
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
            "details": {"hidden_count": len(order) - len(visible_rows)},
        },
        refs=[
            {"document_id": row["document_id"], "section_path": row["section_path"]}
            for row in visible_rows
        ],
    )
