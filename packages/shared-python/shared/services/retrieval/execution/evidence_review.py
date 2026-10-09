"""Build review inputs from the same scoped, composed evidence returned to callers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_explore.evidence_pool import Candidate
from shared.services.retrieval.agent_explore.evidence_review import (
    EvidenceItem,
    EvidenceSnapshot,
)
from shared.services.retrieval.execution.route_types import RetrievalRouteContext
from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results
from shared.services.retrieval.hydration.row_utils import extract_page_nums


async def assemble_review_rows(
    *,
    context: RetrievalRouteContext,
    db: AsyncSession,
    rows: list[dict[str, Any]],
    queried_tables: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    # Content-derived chunk IDs can occur in different documents. Connected
    # assets must be resolved within their owning document during composition.
    by_document: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_document.setdefault(str(row.get("document_id") or ""), []).append(row)
    assembled: list[dict[str, Any]] = []
    for document_rows in by_document.values():
        assembled.extend(await assemble_retrieval_results(
            db=db,
            rows=document_rows,
            exclude_document_ids=context.exclude_document_ids,
            document_scope=context.document_scope,
            exclude_sections=context.exclude_sections,
            allowed_chunk_types=context.allowed_chunk_types,
            revision_pins=context.revision_pins,
            queried_tables=queried_tables,
            offload_composition=True,
        ))
    return assembled


def build_review_snapshot(
    *,
    query: str,
    pool: list[Candidate],
    rows: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> EvidenceSnapshot:
    """Use delivered text/table parts; summaries and outlines are not proof."""
    selected = {
        (candidate.document_id, chunk_id)
        for candidate in pool if candidate.kind == "read"
        for chunk_id in candidate.chunk_ids
    }
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    limitation = None
    for row in rows:
        document_id = str(row.get("document_id") or "")
        chunk_id = str(row.get("chunk_id") or "")
        if (document_id, chunk_id) not in selected:
            continue
        parts = row.get("composed") or []
        if row.get("chunk_type") in {"page", "image"} or any(
            part.get("type") != "text" for part in parts
        ):
            limitation = "visual_evidence_not_reviewed"
        text = "\n".join(
            str(part.get("text") or "") for part in parts if part.get("type") == "text"
        )
        if not text.strip():
            continue
        revision = str(row.get("job_result_id") or "")
        section_path = str(row.get("section_path") or "")
        identity = json.dumps([document_id, chunk_id, revision, section_path])
        evidence_id = "C" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        items.append(EvidenceItem(
            evidence_id=evidence_id,
            document_id=document_id,
            chunk_id=chunk_id,
            section_path=section_path,
            source_file_name=str(row.get("source_file_name") or ""),
            text=text,
            revision=revision,
            page_nums=tuple(
                page for page in (extract_page_nums(row) or [])
                if isinstance(page, int) and not isinstance(page, bool)
            ),
        ))
    # Hash the complete delivered evidence, including unreviewable media and
    # outline context. A changed final packet must never reuse an old verdict.
    digest = hashlib.sha256()
    payload = {"query": query, "items": [asdict(item) for item in items], "evidence": evidence}
    for segment in json.JSONEncoder(sort_keys=True, ensure_ascii=False).iterencode(payload):
        digest.update(segment.encode("utf-8"))
    return EvidenceSnapshot(items=tuple(items), fingerprint=digest.hexdigest(), limitation=limitation)


def review_sources(snapshot: EvidenceSnapshot) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in asdict(item).items() if key != "text"}
        for item in snapshot.items
    ]


def unreviewed_route(reason: str) -> dict[str, Any]:
    return {
        "status": "unverified", "reason": reason, "coverage": [],
        "attempts": 0, "repairs": 0, "reviewer_tokens": 0,
        "usage_complete": True,
    }
