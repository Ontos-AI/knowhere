"""``corpus.assets`` — forward asset search and reverse asset -> hosts lookup.

Image/table chunks are parked under their document's synthetic ``Root``
section in the DB; the real association to a
body section lives in ``chunk_metadata.connect_to`` on the *body* chunk.
Both directions resolve hosts through ``agent_tools.asset_hosts`` (keyed by
document + chunk, current revision only), the same resolver ``corpus.grep``
and ``corpus.recall`` use: forward rows show the first in-scope host's
section_path, and a ``scope`` section subtree keeps an asset only when one
of its hosts lies in that subtree. An asset with no host keeps ``Root`` and
is marked as unhosted.

``chunk_metadata`` is a plain ``JSON`` column (not ``JSONB``), so a
containment query (``@>``) is not available here — that operator is
JSONB-only in PostgreSQL.

Forward search on its own is an unfiltered listing with no way to decide
which assets matter for the current question — pair it with a prior
``corpus.recall``/``corpus.grep`` hit (scope to that hit's section, or use
its ``host_of`` reverse lookup) rather than browsing every asset in a
document.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.asset_hosts import (
    hosted_section_path,
    in_scope_hosts,
    load_asset_hosts,
)
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.agent_tools.scope import (
    SCOPE_SCHEMA,
    ScopeTarget,
    resolve_scope,
    scope_document_ids,
)
from shared.services.retrieval.agent_tools.snippet import build_row, format_row
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES


@register_tool(
    name="corpus.assets",
    description=(
        "Find image and table chunks by type or query, or, given asset "
        "chunk_ids (host_of), list the sections that contain them. Rows "
        "show the containing section_path; an asset that no section "
        "contains is marked (not in any section). A plain listing does not "
        "tell you which assets matter: scope it to a section from a prior "
        "corpus.grep/corpus.recall hit, or use host_of on chunk_ids you "
        "already have."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "scope": SCOPE_SCHEMA,
            "type": {
                "type": "string",
                "enum": ["image", "table", "any"],
                "default": "any",
                "description": "Forward-search asset type. Ignored for host_of.",
            },
            "query": {
                "type": "string",
                "description": "Substring match against summary/keywords (forward search only).",
            },
            "host_of": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "description": "Asset chunk_ids to reverse-resolve to hosting sections.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
)
async def assets(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    scope: list[ScopeTarget] = []
    if args.get("scope") is not None:
        scope, scope_error = await resolve_scope(
            ctx.db,
            user_id=ctx.user_id,
            namespace=ctx.namespace,
            document_scope=ctx.document_scope,
            raw_scope=args.get("scope"),
        )
        if scope_error is not None:
            return ToolResult(text="", error=f"assets: {scope_error}")
    host_of = [str(c).strip() for c in (args.get("host_of") or []) if str(c).strip()]

    if host_of:
        return await _reverse_lookup(ctx, scope=scope, target_ids=host_of)
    return await _forward_search(
        ctx,
        scope=scope,
        asset_type=str(args.get("type") or "any").strip().lower(),
        query=str(args.get("query") or "").strip().lower(),
    )


async def _forward_search(
    ctx: ToolContext,
    *,
    scope: list[ScopeTarget],
    asset_type: str,
    query: str,
) -> ToolResult:
    types = {asset_type} if asset_type in ASSET_CHUNK_TYPES else set(ASSET_CHUNK_TYPES)
    stmt = (
        select(DocumentChunk, DocumentSection.section_path, Document.source_file_name)
        .select_from(DocumentChunk)
        .join(Document, Document.document_id == DocumentChunk.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(
            ctx.document_scope.predicate(Document.document_id),
            Document.user_id == ctx.user_id,
            Document.namespace == ctx.namespace,
            Document.status == "active",
            Document.current_job_result_id == DocumentChunk.job_result_id,
            DocumentChunk.chunk_type.in_(sorted(types)),
        )
        .order_by(DocumentChunk.document_id, DocumentChunk.sort_order, DocumentChunk.chunk_id)
    )
    if scope:
        stmt = stmt.where(Document.document_id.in_(scope_document_ids(scope)))
    asset_rows = (await ctx.db.execute(stmt)).all()

    hosts = await load_asset_hosts(
        ctx,
        document_ids=sorted({chunk.document_id for chunk, _path, _name in asset_rows}),
        asset_ids={chunk.chunk_id for chunk, _path, _name in asset_rows},
    )
    section_scoped = any(target.section_path is not None for target in scope)

    rows: list[dict[str, Any]] = []
    for chunk, stored_path, source_file_name in asset_rows:
        key = (chunk.document_id, chunk.chunk_id)
        if section_scoped and not in_scope_hosts(hosts, key, scope):
            continue
        metadata = chunk.chunk_metadata if isinstance(chunk.chunk_metadata, dict) else {}
        summary = str(metadata.get("summary") or "").strip()
        keywords = metadata.get("keywords") or []
        if query:
            haystack = " ".join(
                [summary.lower(), " ".join(str(k).lower() for k in keywords)]
            )
            if query not in haystack:
                continue
        section_path, hosted = hosted_section_path(hosts, key, scope, stored_path=stored_path)
        rows.append(
            build_row(
                kind=str(chunk.chunk_type or ""),
                document_id=chunk.document_id,
                section_path=section_path,
                title=source_file_name,
                chunk_id=chunk.chunk_id,
                summary=summary,
                hosted=hosted,
            )
        )
        if len(rows) >= ctx.budget.max_items:
            break

    lines = [f"assets={len(rows)}", *(format_row(row) for row in rows)]
    return ToolResult(
        text="\n".join(lines),
        payload={"rows": rows, "details": {}},
        refs=[{"document_id": row["document_id"], "chunk_id": row["chunk_id"]} for row in rows],
    )


async def _reverse_lookup(
    ctx: ToolContext,
    *,
    scope: list[ScopeTarget],
    target_ids: list[str],
) -> ToolResult:
    hosts = await load_asset_hosts(
        ctx,
        document_ids=scope_document_ids(scope) if scope else None,
        asset_ids=target_ids,
    )
    asset_stmt = (
        select(DocumentChunk.document_id, DocumentChunk.chunk_id, DocumentChunk.chunk_type)
        .select_from(DocumentChunk)
        .join(Document, Document.document_id == DocumentChunk.document_id)
        .where(
            Document.current_job_result_id == DocumentChunk.job_result_id,
            DocumentChunk.document_id.in_(sorted({key[0] for key in hosts})),
            DocumentChunk.chunk_id.in_(target_ids),
        )
    )
    asset_type_by_key = {
        (str(document_id), str(chunk_id)): str(chunk_type or "")
        for document_id, chunk_id, chunk_type in (
            (await ctx.db.execute(asset_stmt)).all() if hosts else []
        )
    }

    rows: list[dict[str, Any]] = []
    unhosted: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for target_id in target_ids:
        keys = sorted(key for key in hosts if key[1] == target_id)
        found = False
        for key in keys:
            for host in in_scope_hosts(hosts, key, scope):
                found = True
                row_key = (host.document_id, host.section_path, target_id)
                if row_key in seen:
                    continue
                seen.add(row_key)
                rows.append(
                    build_row(
                        kind=asset_type_by_key.get(key, "asset"),
                        document_id=host.document_id,
                        section_path=host.section_path,
                        title=host.source_file_name,
                        chunk_id=target_id,
                        hosted=True,
                    )
                )
        if not found:
            unhosted.append(target_id)

    lines = [format_row(row) for row in rows]
    lines.extend(
        f"- {target_id}: not contained in any section within this scope"
        for target_id in unhosted
    )
    return ToolResult(
        text="\n".join(lines),
        payload={"rows": rows, "details": {"unhosted": unhosted}},
        refs=[
            {"document_id": row["document_id"], "section_path": row["section_path"]}
            for row in rows
        ],
    )
