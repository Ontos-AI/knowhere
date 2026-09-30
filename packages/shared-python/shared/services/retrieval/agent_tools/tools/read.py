"""``corpus.read`` — full body content for already-located sections/chunks.

Unlike ``hydration.result_assembly.assemble_retrieval_results`` (which
down-weights ``page`` chunks to their summary — see ``page_summary``, a
deliberate trade-off for the retrieval-answer surface),
``read`` returns the page chunk's full body content, with shared-page
markers resolved to the owner section's text rather than stripped or
summarized. Embedded images and tables are still inlined via the same
placeholder mechanism as retrieval.
Table chunks load the stored HTML: small tables return that HTML; large
tables return row/column headers only and point at ``corpus.query_table``
(no window — GREP/recall never scan table-cell HTML, so there is no real
"hit cell" to center a window on). Image ``file_path`` values are converted
to URLs before display so a vision harness can attach HTTPS images.

SAME-AS resolution is single-level: the owner chunk's full content is
embedded as-is. If that owner chunk itself still contains an unrelated
SAME-AS marker (a different leaf's page), it is not recursively resolved in
this pass — a disclosed scope limit, not a silent gap (the raw marker stays
visible in the embedded text).

``section_path`` refs are resolved via ``agent_tools.section_path_lookup``:
exact match first, then a unique segment-bound suffix match when the agent
omits ancestor segments; ambiguous suffix matches return an error listing
candidate full paths instead of picking one silently.

When ``agent_explore`` sets ``ToolContext.readable``/``decided``, a ref must
name an address from an earlier round's results, and body chunks the episode
already picked or passed on are not read again.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, or_, select

from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentSection,
)
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_tools.registry import (
    REF_ADDRESS_ONE_OF,
    REF_ADDRESS_RULE,
    Decision,
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.hydration.asset_inline import inline_assets_at_placeholders
from shared.services.retrieval.hydration.assets import (
    enrich_rows_with_retrieval_asset_url,
)
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.result_assembly import _image_display_content
from shared.services.retrieval.hydration.row_utils import (
    iter_connected_target_ids,
    normalize_chunk_type,
)
from shared.services.retrieval.hydration.table_grid import render_explore_table
from shared.services.retrieval.agent_tools.section_path_lookup import (
    resolve_section_path_anchor,
    section_path_anchor_filter,
    section_path_received,
    section_path_subtree_filter,
)
from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path

_SAME_AS_MARKER_RE = re.compile(r"\[SAME-AS (.+?) p(\d+)\]")
_BODY_CHUNK_TYPES = ("text", "page")
_LOCATE_HINT = (
    " Copy document_id and section_path or chunk_id exactly from a prior "
    "result row, or locate the section first with corpus.outline, "
    "corpus.grep, corpus.recall, or corpus.assets."
)
_NOT_RECEIVED_REASON = (
    "not in any earlier result you have received; results from calls in "
    "this same turn are not available yet"
)


def _with_locate_hint(reason: str) -> str:
    if _LOCATE_HINT.strip() in reason:
        return reason
    return reason + _LOCATE_HINT


def _ref_received(ctx: ToolContext, document_id: str, ref: dict[str, Any]) -> bool:
    if ctx.readable is None:
        return True
    chunk_id = str(ref.get("chunk_id") or "").strip()
    if chunk_id:
        return (document_id, chunk_id) in ctx.readable
    section_path = str(ref.get("section_path") or "").strip()
    return section_path_received(ctx.readable, document_id, section_path)


def _decided_chunks(
    ctx: ToolContext, document_id: str, chunk_ids: list[str]
) -> dict[str, Decision]:
    if ctx.decided is None:
        return {}
    return {
        chunk_id: ctx.decided[(document_id, chunk_id)]
        for chunk_id in chunk_ids
        if (document_id, chunk_id) in ctx.decided
    }


def _already_read_reason(decisions: list[Decision]) -> str:
    parts: list[str] = []
    for decision in dict.fromkeys(decisions):
        state = (
            f"picked as {decision.picked_handle}"
            if decision.picked_handle
            else "not picked"
        )
        parts.append(f"round {decision.read_round} ({state})")
    return f"already read in {', '.join(parts)}; cannot read again"


def _ref_status_line(entry: dict[str, Any]) -> str:
    if entry["status"] == "ok":
        omitted = entry.get("omitted_chunk_ids")
        tag = (
            f"[ok, {len(omitted)} already-read chunks omitted]" if omitted else "[ok]"
        )
    else:
        tag = f"[failed: {entry['reason']}]"
    return (
        f"{tag} document_id={entry['document_id']} "
        f"section_path={entry['section_path']} chunk_id={entry['chunk_id']}"
    )


def _section_belongs_to_resolved_path(
    section: DocumentSection,
    *,
    document_id: str,
    job_result_id: str,
    resolved_path: str,
    mode: str,
) -> bool:
    if section.document_id != document_id or section.job_result_id != job_result_id:
        return False
    if mode == "descendants":
        return (
            section.section_path == resolved_path
            or section.section_path.startswith(f"{resolved_path} / ")
        )
    return section.section_path == resolved_path

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
                replacement = f"(page text shared with section {owner_path})\n{resolved}"
            else:
                replacement = f"(page text shared with section {owner_path}: not found)"
            content = content.replace(marker_text, replacement, 1)
        row["content"] = content


def _compose_explore_text(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
    *,
    char_budget: int,
) -> str:
    base_content = str(row.get("content") or "")
    display = _explore_display_by_target(
        row,
        rows_by_chunk_id,
        char_budget=char_budget,
    )
    if not display:
        return base_content
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    connections = (
        metadata.get("connect_to") if isinstance(metadata, dict) else None
    ) or []
    content, _embedded = inline_assets_at_placeholders(
        base_content,
        connections=connections if isinstance(connections, list) else [],
        display_by_target=display,
    )
    return content


def _explore_display_by_target(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
    *,
    char_budget: int,
) -> dict[str, str]:
    display: dict[str, str] = {}
    for target_id in iter_connected_target_ids(row):
        target_row = rows_by_chunk_id.get(target_id)
        if not target_row:
            continue
        target_type = normalize_chunk_type(target_row.get("chunk_type"))
        if target_type == "table":
            target_content = render_explore_table(
                target_row, char_budget=char_budget
            )
        elif target_type == "image":
            target_content = _image_display_content(target_row)
        else:
            continue
        if target_content:
            display[target_id] = target_content
    return display


def _https_image_media(row: dict[str, Any]) -> list[dict[str, str]]:
    if normalize_chunk_type(row.get("chunk_type")) != "image":
        return []
    url = str(row.get("asset_url") or "").strip()
    if url.startswith("https://"):
        return [{"type": "image_url", "url": url}]
    return []


def _collect_https_image_media(
    assembled: list[dict[str, Any]],
    *,
    rows_by_chunk_id: dict[str, dict[str, Any]],
    include_assets: bool,
) -> list[dict[str, str]]:
    media: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(row: dict[str, Any]) -> None:
        for item in _https_image_media(row):
            url = item["url"]
            if url in seen:
                continue
            seen.add(url)
            media.append(item)

    for row in assembled:
        _add(row)
        if not include_assets:
            continue
        for target_id in iter_connected_target_ids(row):
            target_row = rows_by_chunk_id.get(target_id)
            if target_row:
                _add(target_row)
    return media


@register_tool(
    name="corpus.read",
    description=(
        "Read full content for sections (document_id + section_path) or "
        "chunks (document_id + chunk_id) that you already located. Copy "
        "both values exactly from a prior result row. Page text shared "
        "with another section is filled in automatically. Images and "
        "tables the section contains are shown inline: small tables in "
        "full, large tables as row and column headers (then use "
        "corpus.query_table). Each ref's outcome (ok, or failed with a "
        "reason) is reported separately."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Document that owns this section or chunk.",
                        },
                        "section_path": {
                            "type": "string",
                            "description": "Section to read. Omit when chunk_id is set.",
                        },
                        "chunk_id": {
                            "type": "string",
                            "description": "Chunk to read. Omit when section_path is set.",
                        },
                    },
                    "required": ["document_id"],
                    "oneOf": REF_ADDRESS_ONE_OF,
                    "additionalProperties": False,
                    "description": REF_ADDRESS_RULE,
                },
                "minItems": 1,
                "description": "Each ref needs document_id and exactly one of section_path or chunk_id.",
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
            "include_assets": {
                "type": "boolean",
                "default": True,
                "description": "Show the images and tables this section contains inline.",
            },
        },
        "required": ["refs"],
        "additionalProperties": False,
    },
)
async def read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    refs = args.get("refs")
    if not isinstance(refs, list) or not refs:
        return ToolResult(text="", error="read requires a non-empty refs list")
    mode = str(args.get("mode") or "self").strip().lower()
    if mode not in ("self", "descendants"):
        return ToolResult(text="", error=f"unsupported mode: {mode}")
    include_assets = bool(args.get("include_assets", True))
    char_budget = ctx.budget.max_chars

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
                .where(ctx.document_scope.predicate(Document.document_id))
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

    # One entry per input ref (by index), in call order — the per-ref
    # ok/failed signal this tool now surfaces instead of a single joined
    # error string. "pending" entries are finalized once section_job refs'
    # matched chunks are known, below.
    ref_status: list[dict[str, Any]] = [
        {
            "document_id": str(ref.get("document_id") or "").strip(),
            "section_path": str(ref.get("section_path") or "").strip() or None,
            "chunk_id": str(ref.get("chunk_id") or "").strip() or None,
            "status": "pending",
            "reason": None,
            "chunk_ids": [],
        }
        for ref in refs
    ]

    emit_items: list[tuple[str, Any]] = []
    for index, ref in enumerate(refs):
        document_id = str(ref.get("document_id") or "").strip()
        if not _ref_received(ctx, document_id, ref):
            ref_status[index]["status"] = "failed"
            ref_status[index]["reason"] = _NOT_RECEIVED_REASON
            continue
        job_result_id = revision_by_doc.get(document_id)
        if not job_result_id:
            ref_status[index]["status"] = "failed"
            ref_status[index]["reason"] = _with_locate_hint(
                f"unknown document_id: {document_id}"
            )
            continue
        chunk_id = str(ref.get("chunk_id") or "").strip()
        section_path = str(ref.get("section_path") or "").strip()
        source_file_name = source_file_name_by_doc.get(document_id, "")
        job_id = job_id_by_revision.get(job_result_id)

        if chunk_id:
            row = (
                await ctx.db.execute(
                    select(
                        DocumentChunk,
                        DocumentSection.section_path,
                        DocumentSection.summary,
                    )
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
                ref_status[index]["status"] = "failed"
                ref_status[index]["reason"] = _with_locate_hint(
                    f"unknown chunk_id: {chunk_id} in {document_id}"
                )
                continue
            chunk, resolved_section_path, section_summary = row
            decided = _decided_chunks(ctx, document_id, [chunk.chunk_id])
            if decided:
                ref_status[index]["status"] = "failed"
                ref_status[index]["reason"] = _already_read_reason(list(decided.values()))
                continue
            ref_status[index]["status"] = "ok"
            ref_status[index]["chunk_ids"] = [chunk.chunk_id]
            emit_items.append(
                (
                    "chunk_rows",
                    [
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
                            "section_summary": str(section_summary or "").strip(),
                        }
                    ],
                )
            )
            continue

        if not section_path:
            ref_status[index]["status"] = "failed"
            ref_status[index]["reason"] = _with_locate_hint(
                f"ref for {document_id} needs section_path or chunk_id"
            )
            continue

        resolved_path, path_error = await resolve_section_path_anchor(
            ctx.db,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=section_path,
        )
        if path_error or not resolved_path:
            ref_status[index]["status"] = "failed"
            ref_status[index]["reason"] = _with_locate_hint(
                path_error or f"unknown section_path for {document_id}"
            )
            continue
        emit_items.append(
            (
                "section_job",
                {
                    "ref_index": index,
                    "document_id": document_id,
                    "job_result_id": job_result_id,
                    "resolved_path": resolved_path,
                    "source_file_name": source_file_name,
                    "job_id": job_id,
                },
            )
        )

    section_jobs = [payload for kind, payload in emit_items if kind == "section_job"]
    section_matches: list[DocumentSection] = []
    batched_chunks: list[DocumentChunk] = []
    if section_jobs:
        path_clauses = []
        for job in section_jobs:
            path_filter = (
                section_path_subtree_filter(job["resolved_path"])
                if mode == "descendants"
                else section_path_anchor_filter(job["resolved_path"])
            )
            path_clauses.append(
                and_(
                    DocumentSection.document_id == job["document_id"],
                    DocumentSection.job_result_id == job["job_result_id"],
                    path_filter,
                )
            )
        section_matches = list(
            (
                await ctx.db.execute(
                    select(DocumentSection).where(or_(*path_clauses))
                )
            )
            .scalars()
            .all()
        )
        section_ids = [section.section_id for section in section_matches]
        if section_ids:
            batched_chunks = list(
                (
                    await ctx.db.execute(
                        select(DocumentChunk).where(
                            DocumentChunk.document_id.in_(
                                {job["document_id"] for job in section_jobs}
                            ),
                            DocumentChunk.job_result_id.in_(
                                {job["job_result_id"] for job in section_jobs}
                            ),
                            DocumentChunk.section_id.in_(section_ids),
                            # Body chunks only (text/page). image/table chunks share a
                            # section_id with whichever section happens to store them
                            # in the DB, which is not the same as belonging to that
                            # section; their real association is on the body chunk,
                            # resolved below via hydrate_connected_target_rows.
                            DocumentChunk.chunk_type.in_(_BODY_CHUNK_TYPES),
                        )
                    )
                )
                .scalars()
                .all()
            )

    base_rows: list[dict[str, Any]] = []
    for kind, payload in emit_items:
        if kind == "chunk_rows":
            base_rows.extend(payload)
            continue
        job = payload
        ref_index = job["ref_index"]
        matched_ids = {
            section.section_id
            for section in section_matches
            if _section_belongs_to_resolved_path(
                section,
                document_id=job["document_id"],
                job_result_id=job["job_result_id"],
                resolved_path=job["resolved_path"],
                mode=mode,
            )
        }
        section_path_by_id = {
            section.section_id: section.section_path
            for section in section_matches
            if section.section_id in matched_ids
        }
        section_summary_by_id = {
            section.section_id: str(section.summary or "").strip()
            for section in section_matches
            if section.section_id in matched_ids
        }
        job_chunks = [
            chunk for chunk in batched_chunks if chunk.section_id in matched_ids
        ]
        decided = _decided_chunks(
            ctx, job["document_id"], [chunk.chunk_id for chunk in job_chunks]
        )
        if job_chunks and all(chunk.chunk_id in decided for chunk in job_chunks):
            ref_status[ref_index]["status"] = "failed"
            ref_status[ref_index]["reason"] = _already_read_reason(list(decided.values()))
            continue
        if decided:
            ref_status[ref_index]["omitted_chunk_ids"] = list(decided)
        job_chunk_ids: list[str] = []
        for chunk in batched_chunks:
            if chunk.section_id not in matched_ids or chunk.chunk_id in decided:
                continue
            job_chunk_ids.append(chunk.chunk_id)
            base_rows.append(
                {
                    "document_id": job["document_id"],
                    "job_result_id": job["job_result_id"],
                    "job_id": job["job_id"],
                    "source_file_name": job["source_file_name"],
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
                    "section_summary": section_summary_by_id.get(chunk.section_id, ""),
                }
            )
        if job_chunk_ids:
            ref_status[ref_index]["status"] = "ok"
            ref_status[ref_index]["chunk_ids"] = job_chunk_ids
        else:
            ref_status[ref_index]["status"] = "failed"
            ref_status[ref_index]["reason"] = _with_locate_hint(
                f"no body chunk found for {job['resolved_path']} in "
                f"{job['document_id']} (image/table-only or empty section)"
            )

    status_lines = [_ref_status_line(entry) for entry in ref_status]
    if not base_rows:
        return ToolResult(
            text="\n".join(status_lines),
            payload={"chunks": [], "refs": ref_status},
            error=(
                "read: every ref failed, nothing was read:\n"
                + "\n".join(status_lines)
            ),
        )

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
            document_scope=ctx.document_scope,
            exclude_sections=[],
        )
    enriched_rows = await enrich_rows_with_retrieval_asset_url(
        [*base_rows, *connected_rows],
        log_context="agent_tools.read",
    )
    rows_by_chunk_id = {
        str(row.get("chunk_id") or ""): row
        for row in enriched_rows
        if row.get("chunk_id")
    }
    base_ids = {str(row.get("chunk_id") or "") for row in base_rows}

    assembled: list[dict[str, Any]] = []
    for row in enriched_rows:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id not in base_ids:
            continue
        chunk_type = normalize_chunk_type(row.get("chunk_type"))
        composed = dict(row)
        if chunk_type in _BODY_CHUNK_TYPES:
            composed["content"] = (
                _compose_explore_text(
                    row,
                    rows_by_chunk_id,
                    char_budget=char_budget,
                )
                if include_assets
                else row.get("content")
            )
        elif chunk_type == "table":
            composed["content"] = render_explore_table(
                row, char_budget=char_budget
            )
        elif chunk_type == "image":
            composed["content"] = _image_display_content(row)
        assembled.append(composed)

    lines = ["refs:", *(f"  {line}" for line in status_lines)]
    for row in assembled:
        lines.append(
            f"### {row.get('source_file_name')} ({row.get('document_id')}) / "
            f"{row.get('section_path')} [{row.get('chunk_type')}]"
        )
        lines.append(str(row.get("content") or ""))

    return ToolResult(
        text="\n".join(lines),
        payload={"chunks": assembled, "refs": ref_status},
        refs=[
            {"document_id": row["document_id"], "chunk_id": row["chunk_id"]}
            for row in assembled
        ],
        media=_collect_https_image_media(
            assembled,
            rows_by_chunk_id=rows_by_chunk_id,
            include_assets=include_assets,
        ),
    )
