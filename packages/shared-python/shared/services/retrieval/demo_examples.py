"""Bind curated examples once per publication to unique valid content matches."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.demo_corpus import (
    DemoDocument,
    DemoDocumentChunk,
    DemoDocumentSection,
)
from shared.models.database.job_result import JobResult


def bind_demo_examples(db: Session, *, document_id: str, job_result_id: str) -> None:
    document: DemoDocument = db.execute(
        select(DemoDocument).where(DemoDocument.document_id == document_id)
    ).scalar_one()
    examples: object = (document.catalog_metadata or {}).get("examples", [])
    boundExamples: list[dict[str, Any]] = []
    if isinstance(examples, list) and examples:
        rows = db.execute(
            select(DemoDocumentChunk, DemoDocumentSection.section_path)
            .outerjoin(
                DemoDocumentSection,
                DemoDocumentSection.section_id == DemoDocumentChunk.section_id,
            )
            .where(
                DemoDocumentChunk.document_id == document_id,
                DemoDocumentChunk.job_result_id == job_result_id,
            )
        ).all()
        normalizedRows = [
            (chunk, path, " ".join(str(chunk.content or "").split()).casefold())
            for chunk, path in rows
        ]
        for example in examples:
            if not isinstance(example, dict):
                continue
            citations: list[dict[str, Any]] = []
            for citation in example.get("citations", []):
                needle: str = " ".join(
                    str(citation.get("content") or "").split()
                ).casefold()
                matches = [
                    (chunk, path)
                    for chunk, path, content in normalizedRows
                    if needle and needle in content
                ]
                if len(matches) != 1:
                    citations = []
                    break
                chunk, path = matches[0]
                pageNumbers: object = (chunk.chunk_metadata or {}).get("page_nums", [])
                citations.append(
                    {
                        "description": str(citation.get("description") or ""),
                        "document_id": document_id,
                        "canonical_document_id": document_id,
                        "job_result_id": job_result_id,
                        "demo_source_id": document.demo_source_id,
                        "id": chunk.id,
                        "demo_chunk_id": chunk.id,
                        "chunk_id": chunk.chunk_id,
                        "section_path": path,
                        "content": str(citation.get("content") or ""),
                        "page_number": pageNumbers[0]
                        if isinstance(pageNumbers, list) and pageNumbers
                        else None,
                    }
                )
            if citations:
                boundExamples.append(
                    {
                        "id": example.get("id"),
                        "question": example.get("question"),
                        "answer": example.get("answer"),
                        "citations": citations,
                    }
                )
    revision: JobResult = db.execute(
        select(JobResult).where(JobResult.id == job_result_id)
    ).scalar_one()
    revision.document_metadata = {
        **(revision.document_metadata or {}),
        "bound_demo_examples": boundExamples,
    }
    db.flush()
