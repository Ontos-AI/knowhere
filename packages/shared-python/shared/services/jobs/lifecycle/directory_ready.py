"""Capture navigation without transferring bodies or starting Fine work."""

from datetime import datetime, timezone
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.schemas.document_directory import (
    DocumentDirectoryResponse,
    DocumentDirectorySection,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope


def capture_initial_directory(
    db: Session, *, scope: DocumentPublicationScope, job_id: str
) -> DocumentDirectoryResponse:
    """Freeze the coarse hierarchy while its publication locks are held.

    Fine replaces live section rows later. Keeping this small outline with the
    revision makes a delayed notification read the same coherent directory.
    Page numbers come from chunk metadata and remain physical, 1-based pages.
    """
    storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    sections: list[DocumentDirectorySection] = [
        DocumentDirectorySection.model_validate(dict(row))
        for row in db.execute(
            select(
                storage.DocumentSection.section_id,
                storage.DocumentSection.parent_section_id,
                storage.DocumentSection.section_path,
                storage.DocumentSection.section_title,
                storage.DocumentSection.section_level,
                storage.DocumentSection.sort_order,
            )
            .where(
                storage.DocumentSection.document_id == scope.document_id,
                storage.DocumentSection.job_result_id == scope.job_result_id,
            )
            .order_by(
                storage.DocumentSection.sort_order, storage.DocumentSection.section_id
            )
        ).mappings()
    ]
    parents: dict[str, str | None] = {
        section.section_id: section.parent_section_id for section in sections
    }
    pages_by_section: dict[str, set[int]] = {
        section.section_id: set() for section in sections
    }
    page_rows = db.execute(
        select(
            storage.DocumentChunk.section_id,
            storage.DocumentChunk.chunk_metadata["page_nums"],
        ).where(
            storage.DocumentChunk.document_id == scope.document_id,
            storage.DocumentChunk.job_result_id == scope.job_result_id,
        )
    ).all()
    for row in page_rows:
        section_id: str | None = row[0]
        raw_pages: object = row[1]
        pages: set[int] = (
            {
                page
                for page in cast(list[object], raw_pages)
                if isinstance(page, int) and not isinstance(page, bool) and page > 0
            }
            if isinstance(raw_pages, list)
            else set()
        )
        visited: set[str] = set()
        while section_id in parents and section_id not in visited:
            assert section_id is not None
            visited.add(section_id)
            pages_by_section[section_id].update(pages)
            section_id = parents[section_id]
    return DocumentDirectoryResponse(
        document_id=scope.document_id,
        job_id=job_id,
        job_result_id=scope.job_result_id,
        namespace=scope.namespace,
        directory_version=1,
        directory_committed_at=datetime.now(timezone.utc).isoformat(),
        sections=[
            section.model_copy(
                update={"page_nums": sorted(pages_by_section[section.section_id])}
            )
            for section in sections
        ],
    )
