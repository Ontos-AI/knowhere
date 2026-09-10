"""``corpus.read`` — full body content for already-located sections/chunks.

Unlike ``hydration.result_assembly.assemble_retrieval_results`` (which
down-weights ``page`` chunks to their summary — see that module's
``_page_summary``, a deliberate trade-off for the retrieval-answer surface),
``read`` returns the page chunk's full body content, with ``[SAME-AS <owner>
p<N>]`` markers resolved to the owner section's text (§2 of
``CORPUS_SCHEMA.md``) rather than stripped or summarized. ``connect_to``
assets are still inlined via the same placeholder mechanism as retrieval, and
``page_assets``/asset ``file_path`` are converted to URLs via the existing
``enrich_rows_with_retrieval_asset_url``.

SAME-AS resolution is single-level: the owner chunk's full content is
embedded as-is. If that owner chunk itself still contains an unrelated
SAME-AS marker (a different leaf's page), it is not recursively resolved in
this pass — a disclosed scope limit, not a silent gap (the raw marker stays
visible in the embedded text).

``section_path`` refs are resolved via ``agent_tools.section_path_lookup``:
exact match first, then a unique segment-bound suffix match when the agent
omits ancestor segments; ambiguous suffix matches return an error listing
candidate full paths instead of picking one silently.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentSection,
)
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.hydration.assets import (
    enrich_rows_with_retrieval_asset_url,
)
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.result_assembly import (
    _compose_table_content,
    _compose_text_content,
    _image_display_content,
)
from shared.services.retrieval.hydration.row_utils import normalize_chunk_type
from shared.services.retrieval.agent_tools.section_path_lookup import (
    resolve_section_path_anchor,
    section_path_anchor_filter,
    section_path_subtree_filter,
)
from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path

_SAME_AS_MARKER_RE = re.compile(r"\[SAME-AS (.+?) p(\d+)\]")
_BODY_CHUNK_TYPES = ("text", "page")

# ``_compose_text_content`` doesn't branch on chunk_type — it just inlines
# connect_to placeholders — so the ``page`` branch below reuses it directly
# instead of carrying a near-identical copy. The behavioral difference from
# retrieval's own page handling (never downgrading to a summary — see the
# module docstring) comes entirely from *not* calling ``_page_summary``
# first, which this module never did.


async def _resolve_same_as_markers(
    db: Any,
    rows: list[dict[str, Any]],
    *,
    revision_by_doc: dict[str, str],
    source_file_name_by_doc: dict[str, str],
) -> None:
    """Mutate ``page`` rows in place, replacing SAME-AS markers with owner text."""
    matches_by_index: dict[int, list[tuple[str, str]]] = {}
    needed: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        if normalize_chunk_type(row.get("chunk_type")) != "page":
            continue
        content = str(row.get("content") or "")
        found = list(_SAME_AS_MARKER_RE.finditer(content))
        if not found:
            continue
        document_id = str(row.get("document_id") or "")
        source_file_name = source_file_name_by_doc.get(document_id)
        row_matches: list[tuple[str, str]] = []
        for match in found:
            owner_db_path = section_path_from_chunk_path(
                match.group(1), source_file_name=source_file_name
            )
            needed.add((document_id, owner_db_path))
            row_matches.append((match.group(0), owner_db_path))
        matches_by_index[index] = row_matches

    if not needed:
        return

    owner_content: dict[tuple[str, str], str] = {}
    for document_id, owner_path in needed:
        job_result_id = revision_by_doc.get(document_id)
        if not job_result_id:
            continue
        result = await db.execute(
            select(DocumentChunk.content)
            .select_from(DocumentChunk)
            .join(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentSection.section_path == owner_path)
            .where(DocumentChunk.chunk_type == "page")
        )
        content_row = result.first()
        owner_content[(document_id, owner_path)] = (
            str(content_row[0]) if content_row and content_row[0] else ""
        )

    for index, row_matches in matches_by_index.items():
        row = rows[index]
        content = str(row.get("content") or "")
        document_id = str(row.get("document_id") or "")
        for marker_text, owner_path in row_matches:
            resolved = owner_content.get((document_id, owner_path), "")
            if resolved:
                replacement = f"(SAME-AS {owner_path} resolved)\n{resolved}"
            else:
                replacement = f"(SAME-AS {owner_path} — page not found)"
            content = content.replace(marker_text, replacement, 1)
        row["content"] = content


def _normalize_read_refs(args: dict[str, Any]) -> list[Any]:
    """Accept the canonical ``refs`` list, or a flat single-document shorthand.

    Deterministic, not model-guessing: observed live tool calls sometimes
    hoist ``document_id`` to the top level alongside ``section_path(s)`` /
    ``chunk_id(s)`` instead of nesting each pair inside ``refs`` — the exact
    shape the ``json_schema`` above documents. Rather than relying on the
    model to always match the schema, normalize the known equivalent flat
    shape here so a well-formed ``document_id`` isn't discarded over an
    outer-structure mismatch. Does not change behavior when ``refs`` is
    already a non-empty list.
    """
    refs = args.get("refs")
    if isinstance(refs, list) and refs:
        return refs

    document_id = str(args.get("document_id") or "").strip()
    if not document_id:
        return []

    normalized: list[dict[str, Any]] = []
    section_path = args.get("section_path")
    if isinstance(section_path, str) and section_path.strip():
        normalized.append({"document_id": document_id, "section_path": section_path.strip()})
    for path in args.get("section_paths") or []:
        if isinstance(path, str) and path.strip():
            normalized.append({"document_id": document_id, "section_path": path.strip()})
    chunk_id = args.get("chunk_id")
    if isinstance(chunk_id, str) and chunk_id.strip():
        normalized.append({"document_id": document_id, "chunk_id": chunk_id.strip()})
    for cid in args.get("chunk_ids") or []:
        if isinstance(cid, str) and cid.strip():
            normalized.append({"document_id": document_id, "chunk_id": cid.strip()})
    return normalized


@register_tool(
    name="corpus.read",
    description=(
        "Read full body content for already-located sections or chunks. "
        "Resolves page-track SAME-AS pointers to the owner section's text, "
        "inlines connect_to assets, and converts asset/page_assets "
        "references to URLs. Use after outline/node_filter/recall/grep "
        "have located where to look."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "section_path": {"type": "string"},
                        "chunk_id": {"type": "string"},
                    },
                    "required": ["document_id"],
                },
                "minItems": 1,
                "description": "Each ref needs document_id and either section_path or chunk_id.",
            },
            "mode": {
                "type": "string",
                "enum": ["self", "descendants"],
                "default": "self",
                "description": (
                    "'descendants' also reads every section under a "
                    "section_path ref; ignored for chunk_id refs."
                ),
            },
            "include_assets": {"type": "boolean", "default": True},
            "resolve_same_as": {"type": "boolean", "default": True},
        },
        "required": ["refs"],
    },
)
async def read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    refs = _normalize_read_refs(args)
    if not refs:
        return ToolResult(text="", error="read requires refs")
    mode = str(args.get("mode") or "self").strip().lower()
    if mode not in ("self", "descendants"):
        return ToolResult(text="", error=f"unsupported mode: {mode}")
    include_assets = bool(args.get("include_assets", True))
    resolve_same_as_flag = bool(args.get("resolve_same_as", True))

    document_ids = {
        str(ref.get("document_id") or "").strip() for ref in refs if ref.get("document_id")
    }
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
    source_file_name_by_doc = {d.document_id: d.source_file_name or "" for d in documents}
    job_result_ids = sorted(set(revision_by_doc.values()))
    job_id_by_revision: dict[str, str] = {}
    if job_result_ids:
        job_rows = await ctx.db.execute(
            select(JobResult.id, JobResult.job_id).where(JobResult.id.in_(job_result_ids))
        )
        job_id_by_revision = {str(rid): str(jid) for rid, jid in job_rows.all() if rid and jid}

    base_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for ref in refs:
        document_id = str(ref.get("document_id") or "").strip()
        job_result_id = revision_by_doc.get(document_id)
        if not job_result_id:
            errors.append(f"unknown document_id: {document_id}")
            continue
        chunk_id = str(ref.get("chunk_id") or "").strip()
        section_path = str(ref.get("section_path") or "").strip()
        source_file_name = source_file_name_by_doc.get(document_id, "")
        job_id = job_id_by_revision.get(job_result_id)

        if chunk_id:
            row = (
                await ctx.db.execute(
                    select(DocumentChunk, DocumentSection.section_path)
                    .select_from(DocumentChunk)
                    .outerjoin(
                        DocumentSection,
                        DocumentSection.section_id == DocumentChunk.section_id,
                    )
                    .where(DocumentChunk.document_id == document_id)
                    .where(DocumentChunk.job_result_id == job_result_id)
                    .where(DocumentChunk.chunk_id == chunk_id)
                )
            ).first()
            if row is None:
                errors.append(f"unknown chunk_id: {chunk_id} in {document_id}")
                continue
            chunk, resolved_section_path = row
            base_rows.append(
                {
                    "document_id": document_id,
                    "job_result_id": job_result_id,
                    "job_id": job_id,
                    "source_file_name": source_file_name,
                    "chunk_id": chunk.chunk_id,
                    "section_id": chunk.section_id,
                    "section_path": resolved_section_path,
                    "chunk_type": chunk.chunk_type,
                    "content": chunk.content,
                    "chunk_metadata": chunk.chunk_metadata or {},
                    "file_path": chunk.file_path,
                }
            )
            continue

        if not section_path:
            errors.append(f"ref for {document_id} needs section_path or chunk_id")
            continue

        resolved_path, path_error = await resolve_section_path_anchor(
            ctx.db,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=section_path,
        )
        if path_error or not resolved_path:
            errors.append(path_error or f"unknown section_path for {document_id}")
            continue

        path_filter = (
            section_path_subtree_filter(resolved_path)
            if mode == "descendants"
            else section_path_anchor_filter(resolved_path)
        )
        section_rows = (
            (
                await ctx.db.execute(
                    select(DocumentSection)
                    .where(DocumentSection.document_id == document_id)
                    .where(DocumentSection.job_result_id == job_result_id)
                    .where(path_filter)
                    .order_by(DocumentSection.sort_order)
                )
            )
            .scalars()
            .all()
        )
        section_ids = [s.section_id for s in section_rows]
        chunk_rows = (
            await ctx.db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.job_result_id == job_result_id,
                    DocumentChunk.section_id.in_(section_ids),
                    # Body chunks only (text/page). image/table chunks share a
                    # section_id with whichever section happens to store them
                    # in the DB (always Root — CORPUS_SCHEMA.md §3), which is
                    # not the same as "belonging" to that section; their real
                    # association is connect_to on the body chunk, resolved
                    # below via hydrate_connected_target_rows. Without this
                    # filter, reading Root would return every still-unmounted
                    # asset in the document as spurious top-level entries.
                    DocumentChunk.chunk_type.in_(_BODY_CHUNK_TYPES),
                )
            )
        ).scalars().all()
        section_path_by_id = {s.section_id: s.section_path for s in section_rows}
        for chunk in chunk_rows:
            base_rows.append(
                {
                    "document_id": document_id,
                    "job_result_id": job_result_id,
                    "job_id": job_id,
                    "source_file_name": source_file_name,
                    "chunk_id": chunk.chunk_id,
                    "section_id": chunk.section_id,
                    "section_path": (
                        section_path_by_id.get(chunk.section_id)
                        if chunk.section_id
                        else None
                    ),
                    "chunk_type": chunk.chunk_type,
                    "content": chunk.content,
                    "chunk_metadata": chunk.chunk_metadata or {},
                    "file_path": chunk.file_path,
                }
            )

    if not base_rows:
        return ToolResult(
            text="",
            error="no chunks resolved for given refs" + (f" ({'; '.join(errors)})" if errors else ""),
        )

    if resolve_same_as_flag:
        await _resolve_same_as_markers(
            ctx.db,
            base_rows,
            revision_by_doc=revision_by_doc,
            source_file_name_by_doc=source_file_name_by_doc,
        )

    connected_rows: list[dict[str, Any]] = []
    if include_assets:
        connected_rows = await hydrate_connected_target_rows(
            db=ctx.db,
            rows=base_rows,
            exclude_document_ids=[],
            exclude_sections=[],
        )
    rows_by_chunk_id = {
        str(row.get("chunk_id") or ""): row
        for row in [*base_rows, *connected_rows]
        if row.get("chunk_id")
    }

    assembled: list[dict[str, Any]] = []
    for row in base_rows:
        chunk_type = normalize_chunk_type(row.get("chunk_type"))
        composed = dict(row)
        if chunk_type == "text":
            composed["content"] = _compose_text_content(row, rows_by_chunk_id) if include_assets else row.get("content")
        elif chunk_type == "page":
            composed["content"] = _compose_text_content(row, rows_by_chunk_id) if include_assets else row.get("content")
        elif chunk_type == "table":
            composed["content"] = _compose_table_content(row, rows_by_chunk_id)
        elif chunk_type == "image":
            composed["content"] = _image_display_content(row)
        assembled.append(composed)

    if include_assets:
        assembled = await enrich_rows_with_retrieval_asset_url(
            assembled, log_context="agent_tools.read"
        )

    lines = []
    if errors:
        lines.append(f"errors: {'; '.join(errors)}")
    for row in assembled:
        lines.append(
            f"### {row.get('source_file_name')} ({row.get('document_id')}) / "
            f"{row.get('section_path')} [{row.get('chunk_type')}]"
        )
        lines.append(str(row.get("content") or ""))

    return ToolResult(
        text="\n".join(lines),
        payload={"chunks": assembled, "errors": errors},
        refs=[
            {"document_id": row["document_id"], "chunk_id": row["chunk_id"]}
            for row in assembled
        ],
    )
